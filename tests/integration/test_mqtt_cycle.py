"""Full cycle against a real Mosquitto broker in Docker; OKX is mocked with respx."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import aiomqtt
import pandas as pd
import pytest
import respx
from testcontainers.core.container import DockerContainer
from testcontainers.core.waiting_utils import wait_for_logs

from ha_trading_helper.config import Settings
from ha_trading_helper.scheduler import Runner
from tests.unit.test_service import frames as frames_fixture
from tests.unit.test_service import mock_okx

pytestmark = pytest.mark.integration
frames = frames_fixture


@pytest.fixture(scope="module")
def broker() -> object:
    conf = "listener 1883 0.0.0.0\nallow_anonymous true\n"
    container = (
        DockerContainer("eclipse-mosquitto:2")
        .with_exposed_ports(1883)
        .with_command(f"sh -c 'printf \"{conf}\" > /tmp/m.conf && mosquitto -c /tmp/m.conf'")
    )
    with container:
        wait_for_logs(container, "running", timeout=30)
        yield container


def _text(payload: object) -> str | bytes:
    """Text payloads as str; binary ones (the PNG chart) stay bytes."""
    if not isinstance(payload, bytes):
        return ""
    try:
        return payload.decode()
    except UnicodeDecodeError:
        return payload


async def retained(host: str, port: int, topic: str, count: int) -> dict[str, str | bytes]:
    got: dict[str, str | bytes] = {}
    async with aiomqtt.Client(host, port, identifier="test-reader") as client:
        await client.subscribe(topic)
        async with asyncio.timeout(10):
            async for msg in client.messages:
                got[str(msg.topic)] = _text(msg.payload)
                if len(got) >= count:
                    break
    return got


@respx.mock
async def test_cycle_publishes_discovery_and_states(
    broker: DockerContainer, tmp_path: Path, frames: dict[str, pd.DataFrame]
) -> None:
    respx.route(host="localhost").pass_through()
    mock_okx(frames)
    host, port = broker.get_container_host_ip(), int(broker.get_exposed_port(1883))
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, data_dir=tmp_path, mqtt_host=host, mqtt_port=port, min_volume_24h_usdt=1,
        base_topic="hth_test", quote_currency="USDT",
    )  # fmt: skip
    runner = Runner(settings)
    await runner.cycle()

    configs = await retained(host, port, "homeassistant/+/hth_test/+/config", 3 + 2 * 3)
    score_cfg = json.loads(configs["homeassistant/sensor/hth_test/sol_usdt_puntuacion/config"])
    assert score_cfg["state_topic"] == "hth_test/sol_usdt/state"
    assert "homeassistant/image/hth_test/mejor_oportunidad_grafico/config" in configs

    # status, system and best (state + attributes), the chart, 3 topics per pair.
    states = await retained(host, port, "hth_test/#", 1 + 2 * 2 + 1 + 3 * 3)
    assert states["hth_test/status"] == "online"
    assert states["hth_test/system/state"] == "ok"
    assert states["hth_test/best/state"] == "SOL-USDT"
    assert states["hth_test/sol_usdt/entry"] == "ON"
    attrs = json.loads(states["hth_test/sol_usdt/attributes"])
    assert attrs["accion"] == "ENTRAR" and attrs["motivos"] and attrs["explicacion"]
    chart = states["hth_test/best/chart"]
    assert isinstance(chart, bytes) and chart.startswith(b"\x89PNG")

    # A pair leaving the universe gets its entities removed.
    runner.settings = settings.model_copy(update={"pairs": ["SOL-USDT", "BTC-USDT"]})
    await runner.cycle()
    await runner.publisher.close()
    after = await retained(host, port, "homeassistant/+/hth_test/+/config", 3 + 2 * 2)
    assert "homeassistant/sensor/hth_test/xrp_usdt_puntuacion/config" not in after
    status = await retained(host, port, "hth_test/status", 1)
    assert status["hth_test/status"] == "offline"
    assert (tmp_path / "signals.jsonl").is_file()


async def test_retained_pairs_lists_earlier_discovery_configs(
    broker: DockerContainer, tmp_path: Path
) -> None:
    from ha_trading_helper.publish.mqtt import MqttPublisher
    from ha_trading_helper.publish.payloads import discovery_messages

    host, port = broker.get_container_host_ip(), int(broker.get_exposed_port(1883))
    settings = Settings(_env_file=None, mqtt_host=host, mqtt_port=port, base_topic="hth_rp")  # type: ignore[call-arg]
    async with MqttPublisher(settings) as pub:
        await pub.send(discovery_messages(["SOL-USDT", "ADA-USDC"], "hth_rp", "homeassistant"))
    async with MqttPublisher(settings) as pub:
        assert await pub.retained_pairs(wait=1.0) == {"sol_usdt", "ada_usdc"}


async def test_publisher_reconnects_after_broker_drop(
    broker: DockerContainer, tmp_path: Path
) -> None:
    from ha_trading_helper.publish.mqtt import MqttPublisher
    from ha_trading_helper.publish.payloads import Message

    host, port = broker.get_container_host_ip(), int(broker.get_exposed_port(1883))
    settings = Settings(_env_file=None, mqtt_host=host, mqtt_port=port, base_topic="hth_rc")  # type: ignore[call-arg]
    async with MqttPublisher(settings) as pub:
        assert pub._client is not None
        await pub._client.__aexit__(None, None, None)  # simulate a dropped connection
        await pub.send([Message("hth_rc/x", "1")])
    got = await retained(host, port, "hth_rc/x", 1)
    assert got["hth_rc/x"] == "1"


async def test_telegram_bridge_answers_over_mqtt(broker: DockerContainer, tmp_path: Path) -> None:
    from ha_trading_helper.publish.mqtt import serve_requests

    host, port = broker.get_container_host_ip(), int(broker.get_exposed_port(1883))
    settings = Settings(_env_file=None, mqtt_host=host, mqtt_port=port, base_topic="hth_tg")  # type: ignore[call-arg]
    runner = Runner(settings.model_copy(update={"data_dir": tmp_path}))
    bridge = asyncio.create_task(serve_requests(settings, runner.handle_telegram))
    try:
        async with aiomqtt.Client(host, port, identifier="test-telegram") as client:
            await client.subscribe("hth_tg/telegram/out")
            await asyncio.sleep(0.5)  # let the bridge subscribe
            await client.publish("hth_tg/telegram/in", "not json")  # ignored, bridge survives
            await client.publish(
                "hth_tg/telegram/in", json.dumps({"type": "callback", "data": "/hth buy SOL-USDC"})
            )
            async with asyncio.timeout(10):
                async for msg in client.messages:
                    reply = json.loads(msg.payload.decode())  # type: ignore[union-attr]
                    break
    finally:
        bridge.cancel()
    assert reply["text"].startswith("La operativa está desactivada")
    assert reply["keyboard"] == []
