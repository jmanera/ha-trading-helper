"""Long-lived MQTT connection with a last-will so HA marks entities unavailable if we die."""

from __future__ import annotations

from types import TracebackType
from typing import Self

import aiomqtt
import structlog

from ha_trading_helper.config import Settings
from ha_trading_helper.publish.payloads import Message

log = structlog.get_logger(__name__)


class MqttPublisher:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client: aiomqtt.Client | None = None

    @property
    def status_topic(self) -> str:
        return f"{self.settings.base_topic}/status"

    def _new_client(self) -> aiomqtt.Client:
        return aiomqtt.Client(
            hostname=self.settings.mqtt_host,
            port=self.settings.mqtt_port,
            username=self.settings.mqtt_username,
            password=self.settings.mqtt_password,
            identifier=f"{self.settings.base_topic}-publisher",
            will=aiomqtt.Will(self.status_topic, "offline", qos=1, retain=True),
        )

    async def connect(self) -> aiomqtt.Client:
        client = self._new_client()
        await client.__aenter__()
        self._client = client
        await client.publish(self.status_topic, "online", qos=1, retain=True)
        return client

    async def close(self) -> None:
        if self._client is None:
            return
        try:
            await self._client.publish(self.status_topic, "offline", qos=1, retain=True)
        finally:
            await self._client.__aexit__(None, None, None)
            self._client = None

    async def __aenter__(self) -> Self:
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def send(self, messages: list[Message]) -> None:
        """Publish all messages; reconnect once if the broker dropped us."""
        for attempt in (1, 2):
            try:
                client = self._client or await self.connect()
                await client.publish(self.status_topic, "online", qos=1, retain=True)
                for msg in messages:
                    await client.publish(msg.topic, msg.payload, qos=1, retain=msg.retain)
                return
            except aiomqtt.MqttError as exc:
                log.warning("mqtt_publish_failed", attempt=attempt, error=str(exc))
                if self._client is not None:
                    try:
                        await self._client.__aexit__(None, None, None)
                    except aiomqtt.MqttError:
                        pass
                self._client = None
                if attempt == 2:
                    raise
