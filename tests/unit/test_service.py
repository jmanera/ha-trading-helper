from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import numpy as np
import pandas as pd
import pytest
import respx
from pydantic import ValidationError

from ha_trading_helper.config import HaOptionsSource, Position, Settings
from ha_trading_helper.data.store import CandleStore, candles_to_frame
from ha_trading_helper.data.universe import historical_membership, is_candidate
from ha_trading_helper.okx.client import BASE_URL, OkxClient
from ha_trading_helper.okx.models import Candle
from ha_trading_helper.service import SignalLog, load_validation_history, scan, validation_path
from ha_trading_helper.strategy.signal import Action
from tests.conftest import make_candles, uptrend_with_pullback


async def no_sleep(_: float) -> None:
    return None


def okx_rows(frame: pd.DataFrame, unconfirmed_last: bool = True) -> list[list[str]]:
    """Newest first, as OKX returns them."""
    rows = []
    for n, (ts, r) in enumerate(frame.iterrows()):
        last = n == len(frame) - 1
        rows.append([
            str(int(ts.timestamp() * 1000)), str(r.open), str(r.high), str(r.low), str(r.close),
            str(r.volume), str(r.volume_quote), str(r.volume_quote),
            "0" if (last and unconfirmed_last) else "1",
        ])  # fmt: skip
    return rows[::-1]


def mock_okx(frames: dict[str, pd.DataFrame], fail: set[str] = frozenset()) -> None:  # type: ignore[assignment]
    def candles(request: httpx.Request) -> httpx.Response:
        inst = request.url.params["instId"]
        if inst in fail:
            return httpx.Response(200, json={"code": "51001", "msg": "bad"})
        rows = okx_rows(frames[inst])[: int(request.url.params["limit"])]
        return httpx.Response(200, json={"code": "0", "data": rows})

    def history(request: httpx.Request) -> httpx.Response:
        inst, after = request.url.params["instId"], int(request.url.params["after"])
        rows = [r for r in okx_rows(frames[inst]) if int(r[0]) < after][:100]
        return httpx.Response(200, json={"code": "0", "data": rows})

    respx.get(f"{BASE_URL}/api/v5/market/candles").mock(side_effect=candles)
    respx.get(f"{BASE_URL}/api/v5/market/history-candles").mock(side_effect=history)
    tickers = [{"instId": k, "last": "1", "bidPx": "0.999", "askPx": "1", "volCcy24h": "1e9"}
               for k in frames]  # fmt: skip
    respx.get(f"{BASE_URL}/api/v5/market/tickers").mock(
        return_value=httpx.Response(200, json={"code": "0", "data": tickers})
    )
    respx.get(f"{BASE_URL}/api/v5/public/instruments").mock(
        return_value=httpx.Response(200, json={"code": "0", "data": [
            {"instId": k, "instCategory": "1", "state": "live"} for k in frames]})
    )  # fmt: skip


@pytest.fixture
def frames() -> dict[str, pd.DataFrame]:
    vol = np.full(401, 1000.0)
    vol[-2] = 5000.0
    rng = np.random.default_rng(2)
    up = np.concatenate([uptrend_with_pullback(400), [1.0]])
    up[-1] = up[-2]
    return {
        "BTC-USDT": make_candles(100 * np.exp(np.linspace(0, 0.5, 401))),
        "SOL-USDT": make_candles(up, volume=vol),
        "XRP-USDT": make_candles(100 + rng.normal(0, 0.3, 401)),
    }


def settings(tmp_path: Path, **kw: object) -> Settings:
    kw.setdefault("quote_currency", "USDT")
    return Settings(_env_file=None, data_dir=tmp_path, min_volume_24h_usdt=1, **kw)  # type: ignore[call-arg]


@respx.mock
async def test_scan_full_cycle_uses_closed_candles_only(
    tmp_path: Path, frames: dict[str, pd.DataFrame]
) -> None:
    mock_okx(frames)
    async with OkxClient(sleep=no_sleep) as client:
        result = await scan(settings(tmp_path), client)
    assert not result.errors
    assert set(result.universe) == set(frames)
    by_pair = {s.pair: s for s in result.signals}
    assert by_pair["SOL-USDT"].action is Action.ENTER
    assert by_pair["XRP-USDT"].action is Action.WAIT
    # The in-progress candle (last row) is never analysed.
    assert by_pair["SOL-USDT"].candle_open == frames["SOL-USDT"].index[-2].to_pydatetime()
    assert result.ranked()[0].pair == "SOL-USDT"
    cached = CandleStore(tmp_path).load("SOL-USDT", "4H")
    assert len(cached) == 300

    # Second run is incremental: only the recent endpoint, no history pages.
    history = respx.routes[1]
    before = history.call_count
    async with OkxClient(sleep=no_sleep) as client:
        again = await scan(settings(tmp_path), client)
    assert history.call_count == before
    assert len(again.signals) == 3


@respx.mock
async def test_scan_with_fixed_pairs_and_failures(
    tmp_path: Path, frames: dict[str, pd.DataFrame]
) -> None:
    mock_okx(frames, fail={"XRP-USDT"})
    s = settings(tmp_path, pairs=["sol-usdt", "xrp-usdt"])
    async with OkxClient(sleep=no_sleep) as client:
        result = await scan(s, client)
    assert result.universe == ["SOL-USDT", "XRP-USDT"]
    assert [x.pair for x in result.signals] == ["SOL-USDT"]
    assert result.errors and "XRP-USDT" in result.errors[0]


@respx.mock
async def test_scan_universe_failure(tmp_path: Path) -> None:
    respx.get(f"{BASE_URL}/api/v5/market/tickers").mock(
        return_value=httpx.Response(200, json={"code": "50001", "msg": "down"})
    )
    async with OkxClient(sleep=no_sleep) as client:
        result = await scan(settings(tmp_path), client)
    assert result.signals == [] and "universo" in result.errors[0]


@respx.mock
async def test_positions_are_always_in_universe(
    tmp_path: Path, frames: dict[str, pd.DataFrame]
) -> None:
    mock_okx(
        {k: v for k, v in frames.items() if k != "XRP-USDT"} | {"XRP-USDT": frames["XRP-USDT"]}
    )
    pos = Position(pair="XRP-USDT", entry_price=1.0, entry_time=datetime(2024, 1, 1, tzinfo=UTC))
    s = settings(tmp_path, max_pairs=1, positions=[pos])
    async with OkxClient(sleep=no_sleep) as client:
        result = await scan(s, client)
    assert "XRP-USDT" in result.universe


def test_store_merge_dedup_and_incremental(tmp_path: Path) -> None:
    store = CandleStore(tmp_path)
    a = make_candles([1.0, 2.0, 3.0])
    b = make_candles([3.5, 4.0], start="2024-01-01 08:00")
    store.merge("A-USDT", "4H", a)
    merged = store.merge("A-USDT", "4H", b)
    assert merged["close"].tolist() == [1.0, 2.0, 3.5, 4.0]
    assert store.load("B-USDT", "4H").empty
    c = Candle(ts=datetime(2024, 1, 1, tzinfo=UTC), open=1, high=1, low=1, close=1, volume=1,
               volume_quote=1, confirmed=False)  # fmt: skip
    assert candles_to_frame([c]).empty


def test_signal_log_records_changes(tmp_path: Path, frames: dict[str, pd.DataFrame]) -> None:
    from ha_trading_helper.strategy.signal import build_signal

    s = settings(tmp_path)
    enter = build_signal("SOL-USDT", frames["SOL-USDT"].iloc[:-1], None, s)
    wait = build_signal("XRP-USDT", frames["XRP-USDT"], None, s)
    assert enter is not None and wait is not None
    log = SignalLog(tmp_path)
    changed = log.record([enter, wait])
    assert {c.pair for c in changed} == {"SOL-USDT", "XRP-USDT"}
    rows = [json.loads(x) for x in log.path.read_text().splitlines()]
    assert [r["pair"] for r in rows] == ["SOL-USDT"]  # first quiet sighting not logged
    assert log.record([enter, wait]) == []
    exit_sig = enter.model_copy(update={"action": Action.EXIT})
    assert log.record([exit_sig]) == [exit_sig]
    rows = [json.loads(x) for x in log.path.read_text().splitlines()]
    assert rows[-1]["previous"] == "ENTRAR" and rows[-1]["action"] == "SALIR"


def test_settings_from_ha_options(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    options = tmp_path / "options.json"
    options.write_text(json.dumps({
        "pairs": ["BTC-USDT"], "enter_threshold": 75, "mqtt_username": "", "quote_currency": "EUR",
        "weights": {"trend": 30, "momentum": 15, "rsi": 15, "breakout": 15, "volume": 10,
                    "relative_strength": 15},
    }))  # fmt: skip
    monkeypatch.setattr(HaOptionsSource, "path", options)
    monkeypatch.setenv("HTH_MAX_PAIRS", "7")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.pairs == ["BTC-USDT"]
    assert s.enter_threshold == 75
    assert s.mqtt_username is None
    assert s.weights.trend == 30 and s.weights.total() == 100
    assert s.max_pairs == 7
    assert s.quote_currency == "EUR" and s.benchmark == "BTC-EUR"
    monkeypatch.setattr(HaOptionsSource, "path", tmp_path / "missing.json")
    assert Settings(_env_file=None).pairs == []  # type: ignore[call-arg]


def test_quote_currency_drives_benchmark_and_validation_file(tmp_path: Path) -> None:
    s = Settings(_env_file=None, data_dir=tmp_path)  # type: ignore[call-arg]
    assert s.quote_currency == "USDC" and s.benchmark == "BTC-USDC"
    assert validation_path(s) == tmp_path / "validation-usdc-4h.json"
    eur = Settings(_env_file=None, data_dir=tmp_path, quote_currency="EUR", bar="1D")  # type: ignore[call-arg]
    assert eur.benchmark == "BTC-EUR" and validation_path(eur).name == "validation-eur-1d.json"
    custom = Settings(_env_file=None, quote_currency="EUR", benchmark="ETH-EUR")  # type: ignore[call-arg]
    assert custom.benchmark == "ETH-EUR"
    with pytest.raises(ValidationError):
        Settings(_env_file=None, quote_currency="DOGE")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        Settings(_env_file=None, bar="3H")  # type: ignore[call-arg]


@respx.mock
async def test_automatic_universe_uses_only_the_quote_currency(
    tmp_path: Path, frames: dict[str, pd.DataFrame]
) -> None:
    frames = frames | {"BTC-USDC": frames["BTC-USDT"], "SOL-USDC": frames["SOL-USDT"]}
    mock_okx(frames)
    async with OkxClient(sleep=no_sleep) as client:
        result = await scan(settings(tmp_path, quote_currency="USDC"), client)
    assert set(result.universe) == {"SOL-USDC", "BTC-USDC"}
    assert not result.errors


def test_is_candidate() -> None:
    assert is_candidate("SOL-USDT", "USDT")
    assert not is_candidate("SOL-USDC", "USDT")
    assert not is_candidate("USDC-USDT", "USDT")


def test_historical_membership_uses_only_past_volume() -> None:
    idx = pd.date_range("2025-01-01", periods=6, freq="4h", tz="UTC")
    vols = {
        "A-USDT": [10, 10, 10, 1, 1, 1],
        "B-USDT": [5, 5, 5, 20, 20, 20],
        "C-USDT": [1, 1, 1, 1, 1, 1],
    }
    frames = {k: pd.DataFrame({"volume_quote": v}, index=idx, dtype=float) for k, v in vols.items()}
    members = historical_membership(
        frames, max_pairs=1, min_daily_volume=2, window_bars=2, bars_per_day=1
    )
    assert not members.iloc[0].any()  # window not full yet
    assert members["A-USDT"].tolist() == [False, True, True, False, False, False]
    # B overtakes A once its last two bars average more: never earlier.
    assert members["B-USDT"].tolist() == [False, False, False, True, True, True]
    assert not members["C-USDT"].any()  # below the volume floor
    cut = historical_membership(
        {k: f.iloc[:4] for k, f in frames.items()},
        max_pairs=1, min_daily_volume=2, window_bars=2, bars_per_day=1,
    )  # fmt: skip
    assert cut.equals(members.iloc[:4])
    assert historical_membership(
        {}, max_pairs=1, min_daily_volume=0, window_bars=1, bars_per_day=1
    ).empty


@respx.mock
async def test_validation_history_rebuilds_the_universe(
    tmp_path: Path, frames: dict[str, pd.DataFrame]
) -> None:
    frames = frames | {"USDC-USDT": frames["XRP-USDT"], "SOL-USDC": frames["SOL-USDT"]}
    mock_okx(frames)
    async with OkxClient(sleep=no_sleep) as client:
        hist = await load_validation_history(settings(tmp_path, max_pairs=2), client, 0.1)
        fixed = await load_validation_history(settings(tmp_path), client, 0.1, pairs=["SOL-USDT"])
    assert hist.members is not None
    assert set(hist.data) <= {"BTC-USDT", "SOL-USDT", "XRP-USDT"}
    assert set(hist.members.columns) == set(hist.data)
    assert (hist.members.sum(axis=1) <= 2).all()
    assert "histórico" in hist.universe_text
    assert fixed.members is None and list(fixed.data) == ["SOL-USDT"]
    assert fixed.universe_text == "1 pares fijos"


@respx.mock
async def test_validation_history_in_eur_converts_volume(
    tmp_path: Path, frames: dict[str, pd.DataFrame]
) -> None:
    eur = {
        "BTC-EUR": frames["BTC-USDT"],
        "SOL-EUR": frames["SOL-USDT"],
        "EUR-USDT": frames["XRP-USDT"],
    }
    mock_okx(eur)
    s = Settings(_env_file=None, data_dir=tmp_path, quote_currency="EUR", min_volume_24h_usdt=1e12)  # type: ignore[call-arg]
    async with OkxClient(sleep=no_sleep) as client:
        hist = await load_validation_history(s, client, 0.1)
    assert hist.data == {}  # nothing reaches the volume floor
