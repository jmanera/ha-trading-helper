"""Long-running journal: web panel, a sync every few minutes and a few MQTT sensors."""

from __future__ import annotations

import asyncio
import contextlib

import structlog
import uvicorn

from ha_trading_helper.journal import payloads
from ha_trading_helper.journal.config import JournalSettings
from ha_trading_helper.journal.okx import OkxReadClient
from ha_trading_helper.journal.report import Data
from ha_trading_helper.journal.store import Store
from ha_trading_helper.journal.sync import Syncer, SyncResult
from ha_trading_helper.journal.web import create_app
from ha_trading_helper.publish.mqtt import MqttPublisher

log = structlog.get_logger(__name__)


class JournalService:
    def __init__(self, settings: JournalSettings, store: Store | None = None) -> None:
        self.settings = settings
        self.store = store or Store(settings.db_url())
        self.publisher = MqttPublisher(settings) if settings.mqtt_enabled else None
        self._lock = asyncio.Lock()
        self._discovery_sent = False

    def client(self) -> OkxReadClient:
        s = self.settings
        return OkxReadClient(
            s.okx_api_key or "",
            s.okx_api_secret or "",
            s.okx_passphrase or "",
            base_url=s.okx_base_url,
        )

    async def sync(self) -> SyncResult | None:
        if not self.settings.okx_ready:
            log.warning("journal_no_okx_keys")
            self.store.set_json("last_sync", {"ts": None, "errors": ["faltan las claves de OKX"]})
            return None
        async with self._lock:
            async with self.client() as client:
                result = await Syncer(self.store, client).run()
            await self.publish()
            return result

    async def publish(self) -> None:
        if self.publisher is None:
            return
        s = self.settings
        try:
            data = await asyncio.to_thread(Data.load, self.store)
            msgs = payloads.state_messages(data, s.base_topic)
            if not self._discovery_sent:
                msgs = payloads.discovery_messages(s.base_topic, s.discovery_prefix) + msgs
            await self.publisher.send(msgs)
            self._discovery_sent = True
        except Exception as exc:
            # MQTT is optional: without a broker the panel keeps working.
            log.warning("journal_publish_failed", error=str(exc))

    async def sync_loop(self) -> None:
        while True:
            try:
                await self.sync()
            except Exception:
                log.exception("journal_sync_failed")
            await asyncio.sleep(self.settings.sync_minutes * 60)

    async def run_forever(self) -> None:
        s = self.settings
        app = create_app(self.store, timezone=s.timezone, sync_now=self.sync)
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host=s.web_host,
                port=s.web_port,
                log_level="warning",
                proxy_headers=False,
                access_log=False,
            )
        )
        loop_task = asyncio.create_task(self.sync_loop())
        log.info(
            "journal_started",
            port=s.web_port,
            okx=s.okx_domain,
            db="mariadb" if s.db_host else "sqlite",
        )
        try:
            await server.serve()
        finally:
            loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await loop_task
            if self.publisher is not None:
                with contextlib.suppress(Exception):
                    await self.publisher.close()
            self.store.close()
