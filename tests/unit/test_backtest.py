"""Backtest engine on hand-built bars whose results are easy to compute by hand."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from ha_trading_helper.backtest.engine import (
    CostModel,
    EngineParams,
    Trade,
    backtest_pair,
    limit_concurrent,
    simulate,
)
from ha_trading_helper.backtest.metrics import compute_metrics
from ha_trading_helper.backtest.walkforward import load_validation, save_report, walk_forward
from tests.conftest import make_candles, uptrend_with_pullback

NO_COSTS = CostModel(0.0, 0.0)
PARAMS = EngineParams(stop_atr_mult=2.0, trailing_atr_mult=100.0)


def frame(rows: list[tuple[float, float, float, float]], *, atr: float = 1.0) -> pd.DataFrame:
    """rows = (open, high, low, close); EMA 50 sits far below so it never triggers."""
    idx = pd.date_range("2025-01-01", periods=len(rows), freq="4h", tz="UTC")
    f = pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)
    f["atr"] = atr
    f["ema50"] = 0.0
    return f


def run(f: pd.DataFrame, entries: list[int], params: EngineParams = PARAMS,
        costs: CostModel = NO_COSTS) -> list[Trade]:  # fmt: skip
    flags = pd.Series(False, index=f.index)
    flags.iloc[entries] = True
    return simulate("T-USDT", f, flags, pd.Series(80, index=f.index), params, costs)


def test_stop_loss_is_minus_one_r() -> None:
    f = frame([(100, 100, 100, 100), (100, 101, 99, 100), (100, 100, 97, 97.5)])
    (t,) = run(f, [0])
    assert t.entry == 100 and t.stop == 98
    assert t.r_multiple == pytest.approx(-1.0)
    assert t.exit_reason == "stop"
    assert t.bars_held == 2


def test_gap_through_stop_fills_at_open() -> None:
    f = frame([(100, 100, 100, 100), (100, 101, 99, 100), (95, 96, 94, 95)])
    (t,) = run(f, [0])
    assert t.r_multiple == pytest.approx(-2.5)


def test_partial_at_1_5r_then_3r() -> None:
    f = frame([(100, 100, 100, 100), (100, 103.5, 99.5, 103), (103, 106.5, 102, 106)])
    (t,) = run(f, [0])
    # half at 103 (+1.5R), half at 106 (+3R)
    assert t.r_multiple == pytest.approx(2.25)
    assert t.exit_reason == "objetivo 3R"


def test_partial_then_stop() -> None:
    f = frame([(100, 100, 100, 100), (100, 103.5, 99.5, 103), (103, 103, 97, 97)])
    (t,) = run(f, [0])
    assert t.r_multiple == pytest.approx(0.5 * 1.5 + 0.5 * -1.0)


def test_ema_exit_fills_next_open() -> None:
    f = frame([(100, 100, 100, 100), (100, 101, 99.5, 101), (101, 101.5, 100, 100.5),
               (100.4, 100.8, 100, 100.6)])  # fmt: skip
    f.loc[f.index[2], "ema50"] = 200.0  # close < EMA 50 on bar 2
    (t,) = run(f, [0])
    assert t.exit_reason == "pierde EMA50"
    assert t.exit_price == pytest.approx(100.4)
    assert t.r_multiple == pytest.approx(0.2)


def test_trailing_stop_exit() -> None:
    f = frame([(100, 100, 100, 100), (100, 101.5, 99.5, 101), (101, 101.2, 99.5, 99.6),
               (99.5, 100, 99, 99.8)])  # fmt: skip
    (t,) = run(f, [0], EngineParams(stop_atr_mult=2.0, trailing_atr_mult=1.0))
    assert t.exit_reason == "trailing stop"
    assert t.r_multiple == pytest.approx(-0.25)


def test_costs_are_charged_in_r() -> None:
    f = frame([(100, 100, 100, 100), (100, 101, 99, 100), (100, 100, 97, 97.5)])
    (t,) = run(f, [0], costs=CostModel(fee_pct=0.1, slippage_pct=0.0))
    # -2 price, fees 0.1 % * (100 + 98) = 0.198 -> -2.198 / 2
    assert t.r_multiple == pytest.approx(-1.099)
    (s,) = run(f, [0], costs=CostModel(fee_pct=0.0, slippage_pct=1.0))
    assert s.entry == pytest.approx(101.0)


def test_open_trade_at_end_is_not_counted() -> None:
    f = frame([(100, 100, 100, 100), (100, 101, 99, 100), (100, 101, 99.5, 100)])
    assert run(f, [0]) == []
    f.loc[f.index[2], "ema50"] = 200.0  # exit wanted but no next bar to fill it
    assert run(f, [0]) == []


def test_signal_on_last_bar_and_bad_atr_are_skipped() -> None:
    f = frame([(100, 100, 100, 100), (100, 101, 99, 100)])
    assert run(f, [1]) == []
    g = frame([(100, 100, 100, 100), (100, 101, 99, 100)], atr=float("nan"))
    assert run(g, [0]) == []
    h = frame([(100, 100, 100, 100), (100, 101, 99, 100)], atr=60.0)  # stop below zero
    assert run(h, [0]) == []


def _trade(pair: str, start: int, end: int, r: float = 1.0) -> Trade:
    t0 = datetime(2025, 1, 1, tzinfo=UTC)
    return Trade(pair, t0, t0 + timedelta(hours=start), 1, 0.9, t0 + timedelta(hours=end), 1, r,
                 "x", end - start, 80)  # fmt: skip


def test_limit_concurrent_positions() -> None:
    trades = [_trade("A", 0, 3), _trade("B", 1, 10), _trade("C", 2, 5), _trade("D", 4, 6)]
    kept = limit_concurrent(trades, 2)
    assert [t.pair for t in kept] == ["A", "B", "D"]


def test_metrics() -> None:
    m = compute_metrics([_trade("A", 0, 1, 2.0), _trade("B", 0, 2, -1.0), _trade("C", 0, 3, -1.0)])
    assert m.trades == 3
    assert m.win_rate == pytest.approx(1 / 3)
    assert m.expectancy_r == pytest.approx(0.0)
    assert m.profit_factor == pytest.approx(1.0)
    assert m.max_drawdown_r == pytest.approx(2.0)
    empty = compute_metrics([])
    assert empty.trades == 0 and empty.profit_factor is None
    assert compute_metrics([_trade("A", 0, 1, 1.0)]).profit_factor is None


def cyclical_market(n: int = 1600, seed: int = 3) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    t = np.arange(n)
    trend = np.exp(0.0004 * t + 0.25 * np.sin(t / 60))
    noise = np.exp(np.cumsum(rng.normal(0, 0.004, n)))
    vol = 1000 * (1 + rng.exponential(0.6, n))
    return make_candles(100 * trend * noise, volume=vol)


def test_backtest_pair_and_walk_forward(tmp_path: Path) -> None:
    data = {"A-USDT": cyclical_market(seed=3), "B-USDT": cyclical_market(seed=4)}
    trades = backtest_pair("A-USDT", data["A-USDT"], None, EngineParams(), CostModel())
    assert trades, "the synthetic market should produce trades"
    assert all(t.entry_time > t.signal_time for t in trades)
    assert all(t.exit_time >= t.entry_time for t in trades)

    result = walk_forward(data, None, EngineParams(), CostModel(), folds=3)
    assert len(result.folds) == 3
    assert result.oos.trades == sum(f.test.trades for f in result.folds)
    assert set(result.full_sample) == {60, 70, 80}
    first_test_start = result.folds[0].train_end
    assert all(t.entry_time >= first_test_start for t in result.oos_trades)
    path = tmp_path / "validation.json"
    save_report(result, path)
    loaded = load_validation(path)
    assert loaded is not None and loaded.trades == result.oos.trades
    assert load_validation(tmp_path / "missing.json") is None


def test_walk_forward_without_data() -> None:
    result = walk_forward({}, None, EngineParams(), CostModel())
    assert result.oos.trades == 0 and result.folds == []
    assert result.to_validation().threshold == 0


def test_bull_market_backtest_runs() -> None:
    candles = make_candles(uptrend_with_pullback(400))
    trades = backtest_pair("S-USDT", candles, None, EngineParams(), CostModel())
    assert isinstance(trades, list)
