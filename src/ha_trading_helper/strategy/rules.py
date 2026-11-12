"""The evidences that add points, and the filters that gate them.

Each rule is a vectorised boolean over the feature frame, so live signals and the
backtest evaluate exactly the same code.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import pandas as pd

MOMENTUM_LOOKBACK = 3
RSI_PULLBACK_LOOKBACK = 5
RSI_ZONE = (30.0, 45.0)
VOLUME_SPIKE = 1.5


def _trend(f: pd.DataFrame) -> pd.Series:
    return (f["close"] > f["ema50"]) & (f["ema50"] > f["ema200"])


def _momentum(f: pd.DataFrame) -> pd.Series:
    """MACD histogram crossed from <= 0 to > 0 within the last bars and is still positive."""
    hist = f["macd_hist"]
    crossed = (hist > 0) & (hist.shift(1) <= 0)
    recent = crossed.astype(float).rolling(MOMENTUM_LOOKBACK, min_periods=1).max() > 0
    return recent & (hist > 0)


def _rsi(f: pd.DataFrame) -> pd.Series:
    """RSI leaves the 30-45 pullback zone upwards."""
    low, high = RSI_ZONE
    prev_min = f["rsi"].shift(1).rolling(RSI_PULLBACK_LOOKBACK, min_periods=1).min()
    return (f["rsi"] > high) & (prev_min >= low) & (prev_min <= high)


def _breakout(f: pd.DataFrame) -> pd.Series:
    return f["close"] > f["donchian_high"]


def _volume(f: pd.DataFrame) -> pd.Series:
    return f["rel_volume"] >= VOLUME_SPIKE


def _relative_strength(f: pd.DataFrame) -> pd.Series:
    return f["ret30"] > f["btc_ret30"]


@dataclass(frozen=True)
class Evidence:
    key: str
    label: str
    condition: Callable[[pd.DataFrame], pd.Series]


EVIDENCES: tuple[Evidence, ...] = (
    Evidence("trend", "Tendencia", _trend),
    Evidence("momentum", "MACD", _momentum),
    Evidence("rsi", "RSI(14)", _rsi),
    Evidence("breakout", "Ruptura", _breakout),
    Evidence("volume", "Volumen", _volume),
    Evidence("relative_strength", "Fuerza relativa", _relative_strength),
)


def history_ok(f: pd.DataFrame, min_history: int) -> pd.Series:
    return (f["bars"] >= min_history) & f["ema200"].notna()


def trend_ok(f: pd.DataFrame) -> pd.Series:
    """Entry filter: the trend evidence is required, not just a source of points.

    Without it the other evidences alone can reach the threshold (75 of 100 with
    the default weights) and enter below the EMA 200.
    """
    return _trend(f)


def market_ok(f: pd.DataFrame) -> pd.Series:
    """Market filter: the benchmark (BTC) closes above its EMA 200."""
    return f["btc_close"] > f["btc_ema200"]


def exit_signal(f: pd.DataFrame) -> pd.Series:
    """Rule-based exit independent of the entry price: losing the EMA 50."""
    return f["close"] < f["ema50"]
