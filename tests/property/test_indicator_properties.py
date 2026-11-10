from __future__ import annotations

import numpy as np
import pandas as pd
from hypothesis import given, settings
from hypothesis import strategies as st
from hypothesis.extra.numpy import arrays

from ha_trading_helper import indicators as ind

price_arrays = arrays(
    np.float64,
    st.integers(min_value=30, max_value=300),
    elements=st.floats(min_value=0.01, max_value=1e6, allow_nan=False, allow_infinity=False),
)


@settings(max_examples=60, deadline=None)
@given(price_arrays)
def test_rsi_bounded_and_no_nan_after_warmup(values: np.ndarray) -> None:
    out = ind.rsi(pd.Series(values), 14)
    warm = out.iloc[14:]
    assert not warm.isna().any()
    assert ((warm >= 0) & (warm <= 100)).all()


@settings(max_examples=60, deadline=None)
@given(price_arrays, st.integers(min_value=2, max_value=30))
def test_ema_within_window_extremes(values: np.ndarray, period: int) -> None:
    series = pd.Series(values)
    out = ind.ema(series, period)
    valid = out.dropna()
    assert len(valid) == max(len(values) - period + 1, 0)
    # An EMA is a weighted mean of past values, so it stays within their range.
    for i in valid.index:
        seen = series.iloc[: i + 1]
        assert seen.min() - 1e-6 * seen.max() <= valid[i] <= seen.max() * (1 + 1e-9)


@settings(max_examples=60, deadline=None)
@given(price_arrays, st.floats(min_value=0.0, max_value=0.2))
def test_atr_non_negative(values: np.ndarray, width: float) -> None:
    close = pd.Series(values)
    out = ind.atr(close * (1 + width), close * (1 - width), close, 14)
    warm = out.iloc[14:]
    assert not warm.isna().any()
    assert (warm >= 0).all()


@settings(max_examples=40, deadline=None)
@given(price_arrays, st.integers(min_value=1, max_value=20))
def test_indicators_do_not_look_ahead(values: np.ndarray, cut: int) -> None:
    """Truncating the future never changes past values."""
    full = pd.Series(values)
    part = full.iloc[: max(len(full) - cut, 1)]
    for fn in (lambda s: ind.ema(s, 10), lambda s: ind.rsi(s, 14), lambda s: ind.macd(s)["hist"]):
        a = fn(full).iloc[: len(part)].to_numpy()
        b = fn(part).to_numpy()
        np.testing.assert_allclose(a, b, rtol=1e-9, equal_nan=True)
