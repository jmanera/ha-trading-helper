"""Technical indicators as pure functions over pandas Series.

Every function only looks backwards: the value at ``t`` depends on data up to ``t``.
"""

from __future__ import annotations

import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential moving average seeded with the SMA of the first ``period`` values."""
    if period < 1:
        raise ValueError("period must be >= 1")
    values = series.astype(float)
    out = pd.Series(float("nan"), index=values.index, dtype=float)
    if len(values) < period:
        return out
    alpha = 2.0 / (period + 1)
    seed = values.iloc[:period].mean()
    rest = values.iloc[period:]
    seeded = pd.concat([pd.Series([seed], index=[values.index[period - 1]]), rest])
    out.loc[seeded.index] = seeded.ewm(alpha=alpha, adjust=False).mean()
    return out


def wilder(series: pd.Series, period: int) -> pd.Series:
    """Wilder smoothing (RMA): SMA seed, then alpha = 1/period."""
    values = series.astype(float)
    out = pd.Series(float("nan"), index=values.index, dtype=float)
    valid = values.dropna()
    if len(valid) < period:
        return out
    seed_idx = valid.index[period - 1]
    seed = valid.iloc[:period].mean()
    rest = valid.iloc[period:]
    seeded = pd.concat([pd.Series([seed], index=[seed_idx]), rest])
    out.loc[seeded.index] = seeded.ewm(alpha=1.0 / period, adjust=False).mean()
    return out


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.astype(float).diff()
    gain = wilder(delta.clip(lower=0), period)
    loss = wilder(-delta.clip(upper=0), period)
    out = 100 - 100 / (1 + gain / loss)
    # Only gains -> 100, only losses -> 0, flat -> 50.
    out = out.where(loss != 0, 100.0)
    out = out.where(~((gain == 0) & (loss == 0)), 50.0)
    return out.where(gain.notna())


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    line = ema(close, fast) - ema(close, slow)
    sig = ema(line.dropna(), signal).reindex(close.index)
    return pd.DataFrame({"macd": line, "signal": sig, "hist": line - sig}, index=close.index)


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev = close.shift(1)
    ranges = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1)
    return ranges.max(axis=1, skipna=True)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    tr = true_range(high, low, close)
    tr.iloc[0] = float("nan")  # first bar has no previous close
    return wilder(tr, period)


def donchian_high(high: pd.Series, period: int = 20) -> pd.Series:
    """Highest high of the previous ``period`` bars (current bar excluded)."""
    return high.shift(1).rolling(period, min_periods=period).max()


def relative_volume(volume: pd.Series, period: int = 20) -> pd.Series:
    """Volume of the bar divided by the mean of the previous ``period`` bars."""
    mean = volume.shift(1).rolling(period, min_periods=period).mean()
    return volume / mean.where(mean > 0)


def pct_return(close: pd.Series, period: int) -> pd.Series:
    return close / close.shift(period) - 1


def bollinger(close: pd.Series, period: int = 20, width: float = 2.0) -> pd.DataFrame:
    """Middle band (SMA) and the bands ``width`` population standard deviations away."""
    mid = close.rolling(period, min_periods=period).mean()
    std = close.rolling(period, min_periods=period).std(ddof=0)
    return pd.DataFrame({"mid": mid, "lower": mid - width * std, "upper": mid + width * std})
