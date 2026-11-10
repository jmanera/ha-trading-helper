"""Indicators against straightforward loop implementations of the textbook formulas."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from ha_trading_helper import indicators as ind


def ref_ema(values: list[float], period: int) -> list[float]:
    out = [math.nan] * len(values)
    if len(values) < period:
        return out
    k = 2 / (period + 1)
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def ref_rsi(values: list[float], period: int = 14) -> list[float]:
    out = [math.nan] * len(values)
    gains = [max(values[i] - values[i - 1], 0) for i in range(1, len(values))]
    losses = [max(values[i - 1] - values[i], 0) for i in range(1, len(values))]
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period

    def rsi(g: float, loss: float) -> float:
        return 100.0 if loss == 0 else 100 - 100 / (1 + g / loss)

    out[period] = rsi(avg_g, avg_l)
    for i in range(period + 1, len(values)):
        avg_g = (avg_g * (period - 1) + gains[i - 1]) / period
        avg_l = (avg_l * (period - 1) + losses[i - 1]) / period
        out[i] = rsi(avg_g, avg_l)
    return out


def ref_atr(h: list[float], lo: list[float], c: list[float], period: int = 14) -> list[float]:
    trs = [math.nan] + [
        max(h[i] - lo[i], abs(h[i] - c[i - 1]), abs(lo[i] - c[i - 1])) for i in range(1, len(c))
    ]
    out = [math.nan] * len(c)
    prev = sum(trs[1 : period + 1]) / period
    out[period] = prev
    for i in range(period + 1, len(c)):
        prev = (prev * (period - 1) + trs[i]) / period
        out[i] = prev
    return out


@pytest.fixture
def prices() -> pd.Series:
    rng = np.random.default_rng(42)
    return pd.Series(100 + np.cumsum(rng.normal(0, 1, 300)))


def assert_close(actual: pd.Series, expected: list[float]) -> None:
    np.testing.assert_allclose(actual.to_numpy(), np.array(expected), rtol=1e-10, equal_nan=True)


def test_ema_matches_reference(prices: pd.Series) -> None:
    for period in (5, 50, 200):
        assert_close(ind.ema(prices, period), ref_ema(prices.tolist(), period))


def test_ema_short_series_is_nan() -> None:
    assert ind.ema(pd.Series([1.0, 2.0]), 5).isna().all()


def test_ema_rejects_bad_period() -> None:
    with pytest.raises(ValueError):
        ind.ema(pd.Series([1.0]), 0)


def test_rsi_matches_reference(prices: pd.Series) -> None:
    assert_close(ind.rsi(prices, 14), ref_rsi(prices.tolist(), 14))


def test_rsi_edge_cases() -> None:
    up = pd.Series(np.arange(1, 40, dtype=float))
    assert ind.rsi(up).dropna().eq(100).all()
    down = pd.Series(np.arange(40, 1, -1, dtype=float))
    assert ind.rsi(down).dropna().eq(0).all()
    flat = pd.Series(np.full(40, 5.0))
    assert ind.rsi(flat).dropna().eq(50).all()


def test_rsi_short_series() -> None:
    assert ind.rsi(pd.Series([1.0, 2.0, 3.0])).isna().all()


def test_macd_is_difference_of_emas(prices: pd.Series) -> None:
    m = ind.macd(prices)
    line = pd.Series(ref_ema(prices.tolist(), 12)) - pd.Series(ref_ema(prices.tolist(), 26))
    assert_close(m["macd"], line.tolist())
    valid = line.dropna()
    sig = pd.Series(ref_ema(valid.tolist(), 9), index=valid.index).reindex(prices.index)
    assert_close(m["signal"], sig.tolist())
    assert_close(m["hist"], (line - sig).tolist())


def test_atr_matches_reference(prices: pd.Series) -> None:
    high = prices + 1.5
    low = prices - 1.2
    expected = ref_atr(high.tolist(), low.tolist(), prices.tolist())
    assert_close(ind.atr(high, low, prices), expected)


def test_donchian_excludes_current_bar() -> None:
    high = pd.Series([1.0, 5.0, 3.0, 2.0, 9.0])
    out = ind.donchian_high(high, 3)
    assert out.tolist()[3:] == [5.0, 5.0]
    assert out.iloc[:3].isna().all()


def test_relative_volume() -> None:
    vol = pd.Series([10.0, 10.0, 10.0, 30.0])
    out = ind.relative_volume(vol, 3)
    assert out.iloc[3] == pytest.approx(3.0)
    zero = ind.relative_volume(pd.Series([0.0, 0.0, 5.0]), 2)
    assert math.isnan(zero.iloc[2])


def test_pct_return() -> None:
    out = ind.pct_return(pd.Series([100.0, 110.0, 121.0]), 2)
    assert out.iloc[2] == pytest.approx(0.21)
