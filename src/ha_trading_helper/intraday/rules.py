"""The intraday rules under measurement. FROZEN: do not change them while they are measured.

They were fixed before the first live candle (see docs/research/intraday.md). Changing
a rule after seeing its results would fit it to the noise of the measurement itself; a
new idea is a new rule with a new name, measured from its own start date.

Every function is vectorised over closed candles and only looks backwards, so a
signal at bar t uses data up to the close of t and is traded at the open of t+1.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from ha_trading_helper import indicators as ind

RULES_VERSION = "v1 · 2026-11-12"

BAR_MINUTES = {"5m": 5, "15m": 15, "1H": 60}

ATR_PERIOD = 14
TREND_EMA = 50  # on the 1H candles of the pair (and of BTC)


@dataclass(frozen=True)
class Exits:
    """How a trade of a rule ends; all checked on closed candles."""

    stop_atr: float  # initial stop, x ATR at the signal candle
    trailing_atr: float | None  # chandelier stop from the highest high, None = fixed
    max_bars: int  # time stop: leave at the open after this many candles
    exit_on_mid_band: bool = False  # leave when the close is back on the middle band


@dataclass(frozen=True)
class Rule:
    key: str
    name: str
    bar: str
    exits: Exits
    description: str
    control_of: str | None = None  # random-entry control of this rule


RULE_A = Rule(
    key="a",
    name="A · Ruptura con impulso",
    bar="15m",
    exits=Exits(stop_atr=3.0, trailing_atr=3.0, max_bars=24),
    description=(
        "Velas de 15 min: cierra por encima del máximo de 20 velas, con 1,5 veces el volumen "
        "normal, precio sobre su media de 50 velas y BTC sobre su media de 50 horas. Sale con "
        "un stop móvil de 3 ATR o a las 6 h."
    ),
)
RULE_B = Rule(
    key="b",
    name="B · Rebote tras caída",
    bar="5m",
    exits=Exits(stop_atr=1.5, trailing_atr=None, max_bars=12, exit_on_mid_band=True),
    description=(
        "Velas de 5 min: RSI por debajo de 25 y cierre bajo la banda de Bollinger inferior, "
        "con el par sobre su media de 50 horas. Sale al volver a la banda media, con un stop "
        "de 1,5 ATR o a la hora."
    ),
)
CONTROL_A = Rule(
    key="a_azar",
    name="A · Control al azar",
    bar="15m",
    exits=RULE_A.exits,
    description="Cada vez que A entra, entra en un par elegido al azar, con las salidas de A.",
    control_of="a",
)
CONTROL_B = Rule(
    key="b_azar",
    name="B · Control al azar",
    bar="5m",
    exits=RULE_B.exits,
    description="Cada vez que B entra, entra en un par elegido al azar, con las salidas de B.",
    control_of="b",
)
RULES: tuple[Rule, ...] = (RULE_A, CONTROL_A, RULE_B, CONTROL_B)
RULES_BY_KEY = {r.key: r for r in RULES}

# Seed of the control draws, fixed with the rules so the controls are reproducible.
CONTROL_SEED = 20261112

BREAKOUT_BARS = 20
VOLUME_BARS = 20
VOLUME_SPIKE_A = 1.5
RSI_PERIOD = 14
RSI_OVERSOLD = 25.0
BB_PERIOD, BB_WIDTH = 20, 2.0


def duration(bar: str) -> pd.Timedelta:
    return pd.Timedelta(minutes=BAR_MINUTES[bar])


def hourly_trend(hourly: pd.DataFrame) -> pd.Series:
    """True when the 1H close is above its EMA 50, indexed by the time the hour CLOSES."""
    close = hourly["close"].astype(float)
    up = close > ind.ema(close, TREND_EMA)
    up.index = pd.DatetimeIndex(up.index) + duration("1H")
    return up


def align(trend: pd.Series, frame: pd.DataFrame, bar: str) -> pd.Series:
    """Latest hourly value known when each candle of ``frame`` closes (no look-ahead)."""
    if trend.empty or frame.empty:
        return pd.Series(False, index=frame.index)
    closes = pd.DatetimeIndex(frame.index) + duration(bar)
    known = trend.sort_index().reindex(closes, method="ffill")
    return pd.Series(known.to_numpy(), index=frame.index).astype("boolean").fillna(False)


def features(frame: pd.DataFrame) -> pd.DataFrame:
    """Indicator columns of one pair and timeframe."""
    close = frame["close"].astype(float)
    high, low = frame["high"].astype(float), frame["low"].astype(float)
    f = pd.DataFrame(index=frame.index)
    f["open"], f["high"], f["low"], f["close"] = frame["open"].astype(float), high, low, close
    f["atr"] = ind.atr(high, low, close, ATR_PERIOD)
    f["ema50"] = ind.ema(close, TREND_EMA)
    f["donchian_high"] = ind.donchian_high(high, BREAKOUT_BARS)
    f["rel_volume"] = ind.relative_volume(frame["volume"].astype(float), VOLUME_BARS)
    f["rsi"] = ind.rsi(close, RSI_PERIOD)
    bb = ind.bollinger(close, BB_PERIOD, BB_WIDTH)
    f["bb_mid"], f["bb_lower"] = bb["mid"], bb["lower"]
    return f


def signal_a(f: pd.DataFrame, btc_up: pd.Series) -> pd.Series:
    """Rule A on 15m features; ``btc_up`` is the aligned BTC hourly trend."""
    return (
        (f["close"] > f["donchian_high"])
        & (f["rel_volume"] >= VOLUME_SPIKE_A)
        & (f["close"] > f["ema50"])
        & btc_up.astype(bool)
    ).fillna(False)


def signal_b(f: pd.DataFrame, pair_up: pd.Series) -> pd.Series:
    """Rule B on 5m features; ``pair_up`` is the aligned hourly trend of the pair itself."""
    return ((f["rsi"] < RSI_OVERSOLD) & (f["close"] < f["bb_lower"]) & pair_up.astype(bool)).fillna(
        False
    )
