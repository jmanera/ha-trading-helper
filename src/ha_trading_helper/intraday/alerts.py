"""Monitor alerts: things worth a look, never predictions. Their thresholds are options.

Unlike the measured rules, these can be tuned at any time: they only decide when to
tell the user that something unusual happened on a closed candle.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

import pandas as pd

from ha_trading_helper import indicators as ind
from ha_trading_helper.strategy.explain import fmt

KINDS = ("volumen", "movimiento", "maximo_24h", "minimo_24h", "btc_regimen")
DAY_5M = 288  # 5m candles in 24 h


@dataclass(frozen=True)
class AlertSettings:
    volume_spike: float = 3.0  # 5m volume vs the mean of the previous 20 candles
    move_atr: float = 2.0  # 15m candle body vs ATR(14)
    breakouts: bool = True  # 24 h high / low on 5m closes
    btc_regime: bool = True  # BTC crossing its 1H EMA 50
    cooldown: timedelta = timedelta(minutes=60)  # per pair and kind


@dataclass(frozen=True)
class Alert:
    time: datetime  # open time of the candle that triggered it
    pair: str
    kind: str
    text: str
    price: float


def _pct(value: float) -> str:
    return ("+" if value >= 0 else "") + fmt(value * 100, 2) + " %"


def five_minute(pair: str, frame: pd.DataFrame, s: AlertSettings) -> list[Alert]:
    """Alerts of the last closed 5m candle of ``frame``."""
    if len(frame) < 22:
        return []
    when, row = frame.index[-1], frame.iloc[-1]
    close = float(row["close"])
    out: list[Alert] = []
    rel = ind.relative_volume(frame["volume"].astype(float), 20).iloc[-1]
    if math.isfinite(rel) and rel >= s.volume_spike:
        change = close / float(row["open"]) - 1
        out.append(Alert(when, pair, "volumen", (
            f"{pair}: volumen {fmt(rel, 1)} veces lo normal en 5 min ({_pct(change)})."
        ), close))  # fmt: skip
    if s.breakouts and len(frame) > DAY_5M:
        prev = frame.iloc[-DAY_5M - 1 : -1]
        high, low = float(prev["high"].max()), float(prev["low"].min())
        if close > high:
            out.append(Alert(when, pair, "maximo_24h", (
                f"{pair}: cierra en {fmt(close)}, por encima del máximo de 24 h ({fmt(high)})."
            ), close))  # fmt: skip
        elif close < low:
            out.append(Alert(when, pair, "minimo_24h", (
                f"{pair}: cierra en {fmt(close)}, por debajo del mínimo de 24 h ({fmt(low)})."
            ), close))  # fmt: skip
    return out


def fifteen_minute(pair: str, frame: pd.DataFrame, s: AlertSettings) -> list[Alert]:
    """A 15m candle whose body is several times the usual range."""
    if len(frame) < 16:
        return []
    when, row = frame.index[-1], frame.iloc[-1]
    atr = ind.atr(frame["high"], frame["low"], frame["close"], 14).iloc[-2]
    body = float(row["close"]) - float(row["open"])
    if not math.isfinite(atr) or atr <= 0 or abs(body) < s.move_atr * atr:
        return []
    word = "sube" if body > 0 else "cae"
    change = float(row["close"]) / float(row["open"]) - 1
    return [Alert(when, pair, "movimiento", (
        f"{pair}: {word} {_pct(change)} en 15 min, {fmt(abs(body) / atr, 1)} veces su "
        "movimiento habitual."
    ), float(row["close"]))]  # fmt: skip


def btc_regime(pair: str, hourly: pd.DataFrame, s: AlertSettings) -> list[Alert]:
    """BTC's last 1H close crossed its EMA 50 (market turning)."""
    if not s.btc_regime or len(hourly) < 52:
        return []
    close = hourly["close"].astype(float)
    ema = ind.ema(close, 50)
    before, now = close.iloc[-2] > ema.iloc[-2], close.iloc[-1] > ema.iloc[-1]
    if before == now:
        return []
    text = (
        f"{pair} cruza al alza su media de 50 horas ({fmt(ema.iloc[-1])}): el mercado mejora."
        if now
        else f"{pair} pierde su media de 50 horas ({fmt(ema.iloc[-1])}): el mercado se debilita."
    )
    return [Alert(hourly.index[-1], pair, "btc_regimen", text, float(close.iloc[-1]))]


class Cooldown:
    """At most one alert per pair and kind every ``window``."""

    def __init__(self, window: timedelta) -> None:
        self.window = window
        self.last: dict[tuple[str, str], datetime] = {}

    def allow(self, alert: Alert) -> bool:
        key = (alert.pair, alert.kind)
        prev = self.last.get(key)
        if prev is not None and alert.time - prev < self.window:
            return False
        self.last[key] = alert.time
        return True
