"""Intraday monitor: frozen rules, paper trades, verdict, alerts, store and the cycle."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import respx

from ha_trading_helper.intraday import payloads
from ha_trading_helper.intraday.alerts import (
    Alert,
    AlertSettings,
    Cooldown,
    btc_regime,
    fifteen_minute,
    five_minute,
)
from ha_trading_helper.intraday.config import IntradaySettings
from ha_trading_helper.intraday.monitor import (
    Monitor,
    control_pick,
    floor_time,
    last_closed_open,
)
from ha_trading_helper.intraday.paper import FEE, SLIPPAGE, Stats, Trade, stats, verdict
from ha_trading_helper.intraday.rules import (
    CONTROL_A,
    RULE_A,
    RULE_B,
    RULES_VERSION,
    align,
    features,
    hourly_trend,
    signal_a,
    signal_b,
)
from ha_trading_helper.intraday.store import MeasurementStore
from tests.unit.test_service import mock_okx, no_sleep  # noqa: F401

T0 = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)


def candles(closes: list[float] | np.ndarray, *, freq: str = "15min", start: datetime = T0,
            volume: float | np.ndarray = 100.0, spread: float = 0.002) -> pd.DataFrame:  # fmt: skip
    close = np.asarray(closes, dtype=float)
    open_ = np.concatenate([[close[0]], close[:-1]])
    idx = pd.date_range(start, periods=len(close), freq=freq, tz="UTC", name="ts")
    vol = np.full(len(close), volume, dtype=float) if np.isscalar(volume) else volume
    return pd.DataFrame({
        "open": open_, "high": np.maximum(open_, close) * (1 + spread),
        "low": np.minimum(open_, close) * (1 - spread), "close": close, "volume": vol,
        "volume_quote": vol * close,
    }, index=idx)  # fmt: skip


def settings(tmp_path: Path, **kw: object) -> IntradaySettings:
    kw.setdefault("min_volume_24h_usdt", 1)
    return IntradaySettings(_env_file=None, data_dir=tmp_path, **kw)  # type: ignore[call-arg]


# rules ------------------------------------------------------------------------------
def test_hourly_trend_is_known_only_after_the_hour_closes() -> None:
    hourly = candles(np.linspace(100, 160, 60), freq="1h")
    trend = hourly_trend(hourly)
    assert trend.index[0] == hourly.index[0] + pd.Timedelta(hours=1)
    assert bool(trend.iloc[-1])
    fifteen = candles([100.0] * 8, start=hourly.index[-1].to_pydatetime())
    aligned = align(trend, fifteen, "15m")
    # The 15m candles inside the last hour close before it does: they see the previous one.
    assert aligned.tolist() == [bool(trend.iloc[-2])] * 3 + [bool(trend.iloc[-1])] * 5
    assert not align(pd.Series(dtype=bool), fifteen, "15m").any()


def test_signal_a_needs_breakout_volume_trend_and_btc() -> None:
    closes = np.concatenate([np.linspace(100, 110, 60), [110.0] * 20, [113.0]])
    vol = np.full(len(closes), 100.0)
    vol[-1] = 200.0
    f = features(candles(closes, volume=vol))
    up = pd.Series(True, index=f.index)
    assert bool(signal_a(f, up).iloc[-1])
    assert not bool(signal_a(f, ~up).iloc[-1])  # BTC against
    quiet = features(candles(closes))
    assert not bool(signal_a(quiet, up).iloc[-1])  # no volume


def test_signal_b_needs_oversold_below_band_and_pair_trend() -> None:
    closes = np.concatenate([[100.0] * 40, np.linspace(100, 96, 6)])
    f = features(candles(closes, freq="5min"))
    up = pd.Series(True, index=f.index)
    assert f["rsi"].iloc[-1] < 25 and f["close"].iloc[-1] < f["bb_lower"].iloc[-1]
    assert bool(signal_b(f, up).iloc[-1])
    assert not bool(signal_b(f, ~up).iloc[-1])


# paper trades -----------------------------------------------------------------------
def bar(o: float, h: float, lo: float, c: float, atr: float = 1.0, mid: float = 1e9) -> dict:
    return {"open": o, "high": h, "low": lo, "close": c, "atr": atr, "bb_mid": mid}


def test_entry_at_next_open_and_stop_on_the_entry_candle() -> None:
    t = Trade("b", "X", T0, atr=2.0)
    t.step(RULE_B, T0, bar(100, 100.5, 96, 97))  # stop = entry - 1.5 x 2
    entry = 100 * (1 + SLIPPAGE)
    assert t.entry == pytest.approx(entry) and t.closed and t.exit_reason == "stop"
    stop = entry - 3.0
    exit_price = stop * (1 - SLIPPAGE)
    net = exit_price - entry - FEE * (entry + exit_price)
    assert t.ret_pct == pytest.approx(net / entry * 100)
    assert t.r == pytest.approx(net / 3.0)
    t.step(RULE_B, T0, bar(1, 1, 1, 1))  # closed trades ignore candles
    assert t.exit_price == pytest.approx(exit_price)


def test_gap_through_stop_fills_at_open_and_time_stop() -> None:
    t = Trade("b", "X", T0, atr=1.0)
    t.step(RULE_B, T0, bar(100, 101, 99.5, 100))
    t.step(RULE_B, T0, bar(95, 96, 94, 95))  # gap below the stop
    assert t.exit_price == pytest.approx(95 * (1 - SLIPPAGE)) and t.exit_reason == "stop"

    t = Trade("b", "X", T0, atr=1.0)
    for _ in range(12):
        t.step(RULE_B, T0, bar(100, 100.2, 99.9, 100))
    assert t.status == "exiting" and t.exit_reason == "tiempo"
    t.step(RULE_B, T0, bar(101, 101, 101, 101))
    assert t.closed and t.exit_price == pytest.approx(101 * (1 - SLIPPAGE))


def test_middle_band_exit_and_trailing_stop() -> None:
    t = Trade("b", "X", T0, atr=1.0)
    t.step(RULE_B, T0, bar(100, 100.5, 99.8, 100.4, mid=100.3))
    assert t.status == "exiting" and t.exit_reason == "banda media"

    a = Trade("a", "X", T0, atr=1.0)
    a.step(RULE_A, T0, bar(100, 101, 99.5, 100.8))  # stop 97, trails to 101 - 3
    assert a.stop == pytest.approx(98.0)
    a.step(RULE_A, T0, bar(101, 105, 100.5, 104))
    assert a.stop == pytest.approx(102.0)
    a.step(RULE_A, T0, bar(103, 103.5, 101.5, 102))
    assert a.closed and a.exit_price == pytest.approx(102 * (1 - SLIPPAGE)) and a.ret_pct > 0


def test_stats_and_verdict() -> None:
    def closed(ret: float) -> Trade:
        t = Trade("a", "X", T0, atr=1.0, status="closed")
        t.ret_pct, t.r = ret, ret
        return t

    good = [closed(x) for x in [0.6, 0.4] * 60]
    rand = [closed(x) for x in [0.1, -0.3] * 60]
    s, c = stats([*good, Trade("a", "X", T0, atr=1.0)]), stats(rand)
    assert (s.closed, s.open, s.wins) == (120, 1, 120)
    assert s.mean_pct == pytest.approx(0.5) and s.ci_pct[0] < 0.5 < s.ci_pct[1]
    assert stats([]).win_rate == 0.0 and stats([closed(1.0)]).ci_pct == (1.0, 1.0)

    early = verdict(s, c, T0, T0 + timedelta(days=10))
    assert early[0] == "midiendo" and "faltan 18 días" in early[1]
    passed = verdict(s, c, T0, T0 + timedelta(days=30))
    assert passed[0] == "cumple" and "supera al azar" in passed[1]
    failed = verdict(c, s, T0, T0 + timedelta(days=30))
    assert failed[0] == "no_cumple" and "Sin ventaja" in failed[1]
    few = Stats(closed=50, open=0, wins=25, mean_pct=1, mean_r=1, ci_pct=(0, 2), total_pct=50)
    assert verdict(few, c, T0, T0 + timedelta(days=60))[0] == "midiendo"


# alerts -----------------------------------------------------------------------------
def test_five_minute_alerts() -> None:
    s = AlertSettings()
    closes = np.full(300, 100.0)
    closes[-1] = 104.0
    vol = np.full(300, 10.0)
    vol[-1] = 50.0
    found = five_minute("SOL-USDT", candles(closes, freq="5min", volume=vol), s)
    assert {a.kind for a in found} == {"volumen", "maximo_24h"}
    assert "5,0 veces lo normal" in found[0].text and "+4,00 %" in found[0].text
    closes[-1] = 96.0
    low = five_minute("SOL-USDT", candles(closes, freq="5min"), s)
    assert [a.kind for a in low] == ["minimo_24h"]
    off = AlertSettings(breakouts=False)
    assert five_minute("SOL-USDT", candles(closes, freq="5min"), off) == []
    assert five_minute("SOL-USDT", candles(closes[:10], freq="5min"), s) == []


def test_fifteen_minute_and_btc_alerts() -> None:
    s = AlertSettings()
    closes = 100 + np.sin(np.arange(40))  # steady range
    jump = np.concatenate([closes, [closes[-1] + 10]])
    [move] = fifteen_minute("ETH-USDT", candles(jump), s)
    assert move.kind == "movimiento" and "sube" in move.text
    drop = np.concatenate([closes, [closes[-1] - 10]])
    assert "cae" in fifteen_minute("ETH-USDT", candles(drop), s)[0].text
    assert fifteen_minute("ETH-USDT", candles(closes), s) == []
    assert fifteen_minute("ETH-USDT", candles(closes[:5]), s) == []

    down = np.concatenate([np.linspace(90, 100, 55), [80.0]])  # above its EMA, then below
    [cross] = btc_regime("BTC-USDT", candles(down, freq="1h"), s)
    assert "pierde" in cross.text
    up = np.concatenate([np.linspace(100, 90, 55), [110.0]])  # below its EMA, then above
    assert "al alza" in btc_regime("BTC-USDT", candles(up, freq="1h"), s)[0].text
    assert btc_regime("BTC-USDT", candles(np.full(60, 100.0), freq="1h"), s) == []
    assert btc_regime("BTC-USDT", candles(down, freq="1h"), AlertSettings(btc_regime=False)) == []


def test_cooldown() -> None:
    c = Cooldown(timedelta(minutes=60))
    a = Alert(T0, "X", "volumen", "t", 1.0)
    assert c.allow(a) and not c.allow(Alert(T0 + timedelta(minutes=30), "X", "volumen", "t", 1))
    assert c.allow(Alert(T0 + timedelta(minutes=30), "X", "movimiento", "t", 1))
    assert c.allow(Alert(T0 + timedelta(minutes=61), "X", "volumen", "t", 1))


# store ------------------------------------------------------------------------------
def test_store_roundtrip(tmp_path: Path) -> None:
    st = MeasurementStore(tmp_path / "m.db")
    assert st.started_at(T0) == T0 and st.started_at(T0 + timedelta(days=1)) == T0
    assert st.last_open("X", "5m") is None
    st.set_last_open("X", "5m", T0)
    t = Trade("a", "X", T0, atr=1.5)
    st.save_trade(t)
    t.step(RULE_A, T0, bar(100, 100, 90, 91))
    st.save_trade(t)
    st.save_trade(Trade("b", "Y", T0, atr=1.0))
    st.add_alert(Alert(T0, "X", "volumen", "hola", 1.0))
    st.add_gap(T0, T0 + timedelta(hours=1), "corte")
    st.commit()
    again = MeasurementStore(tmp_path / "m.db")
    assert again.last_open("X", "5m") == T0
    [closed] = again.trades("a")
    assert closed.closed and closed.exit_reason == "stop" and closed.entry_time == T0
    assert [x.pair for x in again.trades(open_only=True)] == ["Y"]
    assert again.count_alerts(T0) == 1 and again.alerts(T0)[0].text == "hola"
    assert again.gaps()[0][2] == "corte"
    again.close()


# monitor ----------------------------------------------------------------------------
def test_time_helpers_and_control_pick() -> None:
    now = datetime(2026, 11, 12, 10, 7, 30, tzinfo=UTC)
    assert floor_time(now, "5m") == datetime(2026, 11, 12, 10, 5, tzinfo=UTC)
    assert last_closed_open(now, "15m") == datetime(2026, 11, 12, 9, 45, tzinfo=UTC)
    assert last_closed_open(now, "1H") == datetime(2026, 11, 12, 9, 0, tzinfo=UTC)
    pick = control_pick(RULE_A, "SOL", T0, ["B", "A", "C"])
    assert pick == control_pick(RULE_A, "SOL", T0, ["C", "B", "A"])  # reproducible
    assert control_pick(RULE_A, "SOL", T0, []) is None


def test_step_series_opens_rule_and_control_and_resumes(tmp_path: Path) -> None:
    m = Monitor(settings(tmp_path))
    idx = pd.date_range(T0, periods=4, freq="15min", tz="UTC")
    f = pd.DataFrame({"open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0, "atr": 1.0,
                      "bb_mid": 1e9}, index=idx)  # fmt: skip
    feats = {"A-USDT": f, "B-USDT": f.copy()}
    fired = pd.Series([False, True, False, False], index=idx)
    signals = {"A-USDT": fired, "B-USDT": pd.Series(False, index=idx)}
    m._step_series(RULE_A, {p: x.iloc[:1] for p, x in feats.items()}, signals)
    assert not m.open  # first sight only sets the starting point
    m._step_series(RULE_A, feats, signals)
    assert ("a", "A-USDT") in m.open
    [control] = [t for (k, _), t in m.open.items() if k == CONTROL_A.key]
    assert control.status == "open" and control.bars == 2
    # Restart: open trades come back from the store and candles are not reprocessed.
    m.store.commit()
    again = Monitor(settings(tmp_path))
    assert set(again.open) == set(m.open)
    again._step_series(RULE_A, feats, signals)
    assert again.open[("a", "A-USDT")].bars == 2


@respx.mock
async def test_cycle_publishes_and_records_gaps(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    start = floor_time(now, "5m") - 4000 * timedelta(minutes=5)  # ends at the open candle
    rng = np.random.default_rng(3)
    walk = 100 * np.exp(np.cumsum(rng.normal(0, 0.003, 4000)))
    frames = {p: candles(walk, freq="5min", start=start) for p in ("BTC-USDT", "SOL-USDT")}
    mock_okx(frames)

    class Capture:
        def __init__(self) -> None:
            self.sent: list[payloads.Message] = []

        async def send(self, msgs: list[payloads.Message]) -> None:
            self.sent += msgs

    m = Monitor(settings(tmp_path, max_pairs=5))
    cap = Capture()
    m.publisher = cap  # type: ignore[assignment]
    await m.cycle(now)
    topics = {x.topic: x for x in cap.sent}
    assert "homeassistant/event/intraday_monitor/alerta/config" in topics
    system = json.loads(topics["intraday_monitor/system/attributes"].payload)
    assert system["reglas_version"] == RULES_VERSION and set(system["pares"]) == set(frames)
    assert m.hourly and m.store.last_open("SOL-USDT", "5m") is not None
    rule_a = json.loads(topics["intraday_monitor/rule_a/attributes"].payload)
    assert rule_a["veredicto"] == "midiendo" and rule_a["cerradas"] == 0
    assert json.loads(topics["intraday_monitor/rule_a_azar/attributes"].payload)["veredicto"] == (
        "control"
    )
    # Second cycle an hour later: the hour is recorded as a cut and discovery is not resent.
    cap.sent.clear()
    m.last_cycle = now - timedelta(hours=1)
    await m.cycle(now)
    assert m.store.gaps() and not any(x.topic.endswith("/config") for x in cap.sent)


def test_alert_payloads() -> None:
    a = Alert(T0, "SOL-USDT", "volumen", "texto", 1.5)
    msg = payloads.alert_event(a, "im")
    assert not msg.retain and json.loads(msg.payload)["event_type"] == "volumen"
    [state, attrs] = payloads.alerts_messages([a], 1, "im")
    assert state.payload == "1" and json.loads(attrs.payload)["alertas"][0]["par"] == "SOL-USDT"
    config = {m.topic: json.loads(m.payload) for m in payloads.discovery_messages("im", "ha")}
    assert config["ha/event/im/alerta/config"]["event_types"][0] == "volumen"


@respx.mock
async def test_catch_up_after_an_outage_and_okx_failures(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    start = floor_time(now, "5m") - 4000 * timedelta(minutes=5)
    rng = np.random.default_rng(5)
    walk = 100 * np.exp(np.cumsum(rng.normal(0, 0.004, 4000)))
    vol = rng.uniform(50, 400, 4000)
    frames = {p: candles(walk * k, freq="5min", start=start, volume=vol)
              for p, k in (("BTC-USDT", 1.0), ("SOL-USDT", 0.5), ("XRP-USDT", 0.01))}  # fmt: skip
    mock_okx(frames, fail={"XRP-USDT"})
    m = Monitor(settings(tmp_path, max_pairs=5))

    class Quiet:
        async def send(self, msgs: list[payloads.Message]) -> None:
            raise RuntimeError("broker down")  # logged, never raised

    m.publisher = Quiet()  # type: ignore[assignment]
    await m.cycle(now)
    assert any("XRP-USDT" in e for e in m.errors)
    # Two days off: the next cycle downloads the missed candles and trades through them.
    for pair in ("BTC-USDT", "SOL-USDT"):
        for bar in ("5m", "15m"):
            last = m.store.last_open(pair, bar)
            assert last is not None
            m.store.set_last_open(pair, bar, last - timedelta(days=2))
    m.hourly.clear()
    await m.cycle(now)
    assert m.store.trades(), "two days of candles should produce paper trades"
    assert all(t.pair != "XRP-USDT" for t in m.store.trades())
    await m.safe_cycle()  # same candles again: nothing is processed twice


def test_app_options_match_settings() -> None:
    import yaml

    root = Path(__file__).parents[2]
    config = yaml.safe_load((root / "intraday_monitor/config.yaml").read_text())
    fields = set(IntradaySettings.model_fields)
    assert set(config["options"]) <= fields
    assert {k.rstrip("?") for k in config["schema"]} <= fields
    assert set(config["options"]) <= set(config["schema"])
    s = IntradaySettings(_env_file=None, **config["options"])  # type: ignore[call-arg]
    assert s.benchmark == "BTC-USDT" and s.alert_settings().cooldown == timedelta(hours=1)
    es = yaml.safe_load((root / "intraday_monitor/translations/es.yaml").read_text())
    assert set(es["configuration"]) == {k.rstrip("?") for k in config["schema"]}


def test_status_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    from ha_trading_helper import cli

    st = MeasurementStore(tmp_path / "intraday.db")
    st.started_at(T0)
    st.add_gap(T0, T0 + timedelta(hours=1), "corte de prueba")
    st.commit()
    monkeypatch.setenv("HTH_DATA_DIR", str(tmp_path))
    result = CliRunner().invoke(cli.app, ["intraday", "status"])
    assert result.exit_code == 0, result.output
    assert RULES_VERSION in result.output and "A · Ruptura con impulso" in result.output
    assert "Midiendo" in result.output and "corte de prueba" in result.output
