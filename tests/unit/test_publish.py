from __future__ import annotations

import json
from datetime import UTC, datetime

import numpy as np
import pytest

from ha_trading_helper.config import Settings
from ha_trading_helper.publish import payloads
from ha_trading_helper.strategy.signal import Action, Signal, Validation, build_signal
from tests.conftest import make_candles, uptrend_with_pullback

NOW = datetime(2026, 11, 10, 12, 1, tzinfo=UTC)
VALIDATION = Validation(trades=87, win_rate=0.41, expectancy_r=0.22, profit_factor=1.3,
                        max_drawdown_r=6, threshold=70, generated_at=NOW)  # fmt: skip


@pytest.fixture
def signals() -> list[Signal]:
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    vol = np.full(400, 1000.0)
    vol[-1] = 5000.0
    enter = build_signal("SOL-USDT", make_candles(uptrend_with_pullback(400), volume=vol), None,
                         s, validation=VALIDATION)  # fmt: skip
    rng = np.random.default_rng(1)
    wait = build_signal("XRP-USDT", make_candles(100 + rng.normal(0, 0.3, 400)), None, s)
    assert enter is not None and wait is not None
    return [wait, enter]


def by_topic(msgs: list[payloads.Message]) -> dict[str, str]:
    return {m.topic: m.payload for m in msgs}


def test_discovery_follows_ha_schema() -> None:
    msgs = payloads.discovery_messages(["SOL-USDT"], "hth", "homeassistant")
    topics = by_topic(msgs)
    assert set(topics) == {
        "homeassistant/sensor/hth/estado/config",
        "homeassistant/sensor/hth/mejor_oportunidad/config",
        "homeassistant/sensor/hth/sol_usdt_puntuacion/config",
        "homeassistant/binary_sensor/hth/sol_usdt_entrada/config",
        "homeassistant/image/hth/mejor_oportunidad_grafico/config",
    }
    for payload in topics.values():
        body = json.loads(payload)
        for key in ("name", "unique_id", "availability_topic", "device"):
            assert key in body
        assert "state_topic" in body or "image_topic" in body
        assert body["device"]["identifiers"] == ["hth"]
        assert body["unique_id"].startswith("hth_")
    score = json.loads(topics["homeassistant/sensor/hth/sol_usdt_puntuacion/config"])
    assert score["json_attributes_topic"] == "hth/sol_usdt/attributes"
    assert score["default_entity_id"] == "sensor.hth_sol_usdt_puntuacion"
    entry = json.loads(topics["homeassistant/binary_sensor/hth/sol_usdt_entrada/config"])
    assert (entry["payload_on"], entry["payload_off"]) == ("ON", "OFF")
    assert all(m.retain for m in msgs)
    chart = json.loads(topics["homeassistant/image/hth/mejor_oportunidad_grafico/config"])
    assert chart["image_topic"] == "hth/best/chart"
    assert chart["content_type"] == "image/png"
    assert payloads.chart_message(b"png", "hth") == payloads.Message("hth/best/chart", b"png")


def test_removal_clears_configs() -> None:
    msgs = payloads.removal_messages(["ADA-USDT"], "hth", "homeassistant")
    assert {m.payload for m in msgs} == {""}
    assert len(msgs) == 2


def test_state_messages(signals: list[Signal]) -> None:
    topics = by_topic(
        payloads.state_messages(signals, "hth", now=NOW, errors=[], universe=["SOL-USDT"])
    )
    assert topics["hth/best/state"] == "SOL-USDT"
    assert topics["hth/sol_usdt/entry"] == "ON"
    assert topics["hth/xrp_usdt/entry"] == "OFF"
    assert topics["hth/system/state"] == "ok"
    attrs = json.loads(topics["hth/sol_usdt/attributes"])
    assert attrs["accion"] == "ENTRAR"
    assert attrs["alerta"] is True
    assert sum(m["p"] for m in attrs["motivos"]) == attrs["puntuacion"]
    assert attrs["explicacion"][0]["t"] == "Las reglas ven motivos para comprar"
    assert attrs["explicacion"][-1]["t"] == "Importante"
    assert {"i", "t", "d"} <= set(attrs["explicacion"][1])
    assert "esperanza +0,22R" in attrs["validacion"]
    assert set(attrs["niveles"]) >= {"entrada", "stop", "objetivo1", "objetivo2"}
    best = json.loads(topics["hth/best/attributes"])
    assert [r["par"] for r in best["ranking"]] == ["SOL-USDT", "XRP-USDT"]
    system = json.loads(topics["hth/system/attributes"])
    assert system["validacion_positiva"] is True
    assert system["ultima_vela"] is not None


def test_attribute_size_budget(signals: list[Signal]) -> None:
    for msg in payloads.state_messages(signals, "hth", now=NOW, errors=[], universe=[]):
        assert len(msg.payload.encode()) < payloads.MAX_ATTRIBUTES_BYTES, msg.topic


def test_health_states(signals: list[Signal]) -> None:
    deg = by_topic(payloads.state_messages(signals, "b", now=NOW, errors=["x"], universe=[]))
    assert deg["b/system/state"] == "degradada"
    err = by_topic(payloads.state_messages([], "b", now=NOW, errors=["x"], universe=[]))
    assert err["b/system/state"] == "error"
    assert err["b/best/state"] == "ninguna"
    assert json.loads(err["b/system/attributes"])["validacion"] == "Sin backtest aún"


def test_exit_signal_attributes(signals: list[Signal]) -> None:
    exit_sig = signals[0].model_copy(
        update={"action": Action.EXIT, "exit_reason": "pierde EMA50", "levels": None}
    )
    attrs = payloads.signal_attributes(exit_sig)
    assert attrs["salida"] == "pierde EMA50"
    assert "niveles" not in attrs
    ranked = payloads.ranking([signals[0], exit_sig, signals[1]])
    assert [s.action for s in ranked] == [Action.ENTER, Action.EXIT, Action.WAIT]
