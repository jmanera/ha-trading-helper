"""Long-running mode: run one cycle at start-up and after every candle close.

The walk-forward validation is refreshed weekly (and at start-up when it is
missing or stale) so live signals always show how their rules did out of sample.
"""

from __future__ import annotations

import asyncio
import signal
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from ha_trading_helper.backtest.engine import CostModel, EngineParams
from ha_trading_helper.backtest.walkforward import load_validation, save_report, walk_forward
from ha_trading_helper.config import Settings
from ha_trading_helper.data.store import CandleStore
from ha_trading_helper.okx.client import OkxClient
from ha_trading_helper.okx.trade import OkxTradeClient
from ha_trading_helper.publish import payloads
from ha_trading_helper.publish.mqtt import MqttPublisher, serve_requests
from ha_trading_helper.service import SignalLog, load_history, scan, validation_path
from ha_trading_helper.strategy.signal import Signal
from ha_trading_helper.timeframe import close_hours
from ha_trading_helper.trading.executor import Executor
from ha_trading_helper.trading.positions import PositionStore
from ha_trading_helper.trading.telegram import Conversation

log = structlog.get_logger(__name__)

VALIDATION_MAX_AGE = timedelta(days=7)
VALIDATION_YEARS = 2.0


class Runner:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.store = CandleStore(settings.data_dir)
        self.signal_log = SignalLog(settings.data_dir)
        self.publisher = MqttPublisher(settings)
        self._published_pairs: set[str] = set()
        self._swept_retained = False
        self._lock = asyncio.Lock()
        self.positions = PositionStore(settings.data_dir)
        self.latest: dict[str, Signal] = {}
        self.trade_client: OkxTradeClient | None = None
        executor = None
        if settings.trading_ready:
            self.trade_client = OkxTradeClient(
                settings.okx_api_key or "",
                settings.okx_api_secret or "",
                settings.okx_passphrase or "",
                demo=settings.okx_demo,
            )
            executor = Executor(
                self.trade_client,
                self.positions,
                stop_on_exchange=settings.stop_loss_on_exchange,
            )
        self.conversation = Conversation(settings, executor, self.positions, self.latest_signal)

    def latest_signal(self, pair: str) -> Signal | None:
        return self.latest.get(pair)

    def scan_settings(self) -> Settings:
        """Settings plus the positions opened from Telegram, so SALIR can fire for them."""
        opened = {p.pair: p.as_signal_position() for p in self.positions.load().values()}
        manual = [p for p in self.settings.positions if p.pair not in opened]
        return self.settings.model_copy(update={"positions": [*manual, *opened.values()]})

    async def handle_telegram(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        replies = await self.conversation.handle(event)
        return [r.model_dump() for r in replies]

    async def cycle(self) -> None:
        async with self._lock:
            started = datetime.now(UTC)
            async with OkxClient() as client:
                result = await scan(self.scan_settings(), client, self.store)
            self.latest = {sig.pair: sig for sig in result.signals}
            s = self.settings
            pairs = [sig.pair for sig in result.signals]
            messages = payloads.discovery_messages(pairs, s.base_topic, s.discovery_prefix)
            gone = sorted(self._published_pairs - set(pairs))
            messages += payloads.removal_messages(gone, s.base_topic, s.discovery_prefix)
            messages += payloads.state_messages(
                result.signals,
                s.base_topic,
                now=result.finished_at,
                errors=result.errors,
                universe=result.universe,
                wait_threshold=s.wait_threshold,
            )
            try:
                if not self._swept_retained:
                    # Once per run: drop entities left on the broker by earlier runs.
                    keep = {payloads.slug(p) for p in [*pairs, *gone]}
                    orphans = sorted(await self.publisher.retained_pairs() - keep)
                    messages += payloads.removal_messages(orphans, s.base_topic, s.discovery_prefix)
                await self.publisher.send(messages)
                self._published_pairs = set(pairs)
                self._swept_retained = True
            except Exception:
                log.exception("publish_failed")
            changed = self.signal_log.record(result.signals)
            best = result.ranked()[0] if result.signals else None
            log.info(
                "cycle_done",
                pairs=len(result.signals),
                errors=len(result.errors),
                changed=[f"{c.pair}:{c.action.value}" for c in changed],
                best=f"{best.pair} {best.action.value} {best.score}" if best else None,
                seconds=round((datetime.now(UTC) - started).total_seconds(), 1),
            )

    def validation_is_stale(self) -> bool:
        current = load_validation(validation_path(self.settings))
        if current is None:
            return True
        return datetime.now(UTC) - current.generated_at > VALIDATION_MAX_AGE

    async def revalidate(self) -> None:
        s = self.settings
        log.info("validation_started", years=VALIDATION_YEARS)
        async with OkxClient() as client:
            data, benchmark = await load_history(s, client, list(s.pairs), VALIDATION_YEARS)
        params = EngineParams(
            enter_threshold=s.enter_threshold,
            min_history=s.min_history,
            stop_atr_mult=s.stop_atr_mult,
            trailing_atr_mult=s.trailing_atr_mult,
            weights=s.weights,
        )
        result = await asyncio.to_thread(
            walk_forward,
            data,
            benchmark,
            params,
            CostModel(s.fee_pct, s.slippage_pct),
            max_positions=s.max_positions,
        )
        save_report(result, validation_path(s))
        v = result.to_validation()
        log.info("validation_done", text=v.text(), positive=v.positive)

    async def safe_revalidate(self, *, only_if_stale: bool = False) -> None:
        try:
            if only_if_stale and not self.validation_is_stale():
                return
            await self.revalidate()
            await self.cycle()  # republish with the new validation
        except Exception:
            log.exception("validation_failed")

    async def safe_cycle(self) -> None:
        try:
            await self.cycle()
        except Exception:
            log.exception("cycle_failed")

    async def run_forever(self) -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)

        scheduler = AsyncIOScheduler(timezone=UTC)
        delay = self.settings.run_delay_seconds
        scheduler.add_job(
            self.safe_cycle,
            CronTrigger(
                hour=close_hours(self.settings.bar),
                minute=delay // 60,
                second=delay % 60,
                timezone=UTC,
            ),
            max_instances=1,
            coalesce=True,
            misfire_grace_time=900,
        )
        scheduler.add_job(
            self.safe_revalidate,
            CronTrigger(day_of_week="sun", hour=2, minute=30, timezone=UTC),
            max_instances=1,
            coalesce=True,
        )
        scheduler.start()
        log.info("scheduler_started", bar=self.settings.bar)
        await self.safe_cycle()
        background = asyncio.create_task(self.safe_revalidate(only_if_stale=True))
        bridge = asyncio.create_task(serve_requests(self.settings, self.handle_telegram))
        await stop.wait()
        background.cancel()
        bridge.cancel()
        scheduler.shutdown(wait=False)
        await self.publisher.close()
        if self.trade_client is not None:
            await self.trade_client.__aexit__(None, None, None)
