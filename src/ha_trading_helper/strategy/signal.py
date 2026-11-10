"""Build the explainable signal for the last closed candle of a pair."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

import pandas as pd
from pydantic import BaseModel

from ha_trading_helper.config import Position, Settings
from ha_trading_helper.strategy.explain import describe, fmt, market_text
from ha_trading_helper.strategy.features import compute_features
from ha_trading_helper.strategy.risk import Levels, compute_levels
from ha_trading_helper.strategy.rules import EVIDENCES, exit_signal
from ha_trading_helper.strategy.scoring import score_frame
from ha_trading_helper.timeframe import duration

MIN_VALIDATION_TRADES = 30
MIN_PROFIT_FACTOR = 1.1


class Action(StrEnum):
    ENTER = "ENTRAR"
    WAIT = "ESPERAR"
    EXIT = "SALIR"


class Reason(BaseModel):
    key: str
    label: str
    points: int
    max_points: int
    detail: str


class Validation(BaseModel):
    """Out-of-sample result of the same rules, net of fees and slippage."""

    trades: int
    win_rate: float
    expectancy_r: float
    profit_factor: float | None
    max_drawdown_r: float
    threshold: int
    generated_at: datetime

    @property
    def positive(self) -> bool:
        """Good enough to turn ENTRAR into an alert: enough trades and a real margin."""
        return (
            self.trades >= MIN_VALIDATION_TRADES
            and self.expectancy_r > 0
            and (self.profit_factor or 0.0) >= MIN_PROFIT_FACTOR
        )

    def text(self) -> str:
        sign = "+" if self.expectancy_r >= 0 else ""
        return (
            f"Fuera de muestra: {fmt(self.win_rate * 100, 0)} % aciertos, "
            f"esperanza {sign}{fmt(self.expectancy_r, 2)}R tras costes (n={self.trades})"
        )


class Signal(BaseModel):
    pair: str
    action: Action
    score: int
    candle_open: datetime
    candle_close: datetime
    close: float
    reasons: list[Reason]
    market_ok: bool
    market: str
    levels: Levels | None
    exit_reason: str | None = None
    stop_atr_mult: float = 2.0
    validation: Validation | None = None
    alert: bool = False

    def top_reasons(self, n: int = 3) -> list[Reason]:
        scored = [r for r in self.reasons if r.points > 0]
        return sorted(scored, key=lambda r: r.points, reverse=True)[:n]

    def render(self) -> str:
        """Plain-text block like the one in the plan, used by the CLI and notifications."""
        stamp = self.candle_close.strftime("%Y-%m-%d %H:%M UTC")
        lines = [f"{self.pair} · {self.action.value} · puntuación {self.score}/100 · vela {stamp}"]
        lines.append("Motivos:")
        for r in self.reasons:
            lines.append(f"  +{r.points:>2}  {r.label}: {r.detail}")
        lines.append(f"Mercado: {self.market} (filtro {'OK' if self.market_ok else 'EN CONTRA'})")
        if self.exit_reason:
            lines.append(f"Salida: {self.exit_reason}")
        if self.levels:
            lv = self.levels
            lines.append(
                f"Niveles: entrada {fmt(lv.entry)} · stop {fmt(lv.stop)} "
                f"({fmt(self.stop_atr_mult, 1)}×ATR) · "
                f"objetivos {fmt(lv.target1)} / {fmt(lv.target2)}"
            )
        lines.append(
            "Validación: " + (self.validation.text() if self.validation else "sin backtest aún")
        )
        return "\n".join(lines)


def trailing_stop(features: pd.DataFrame, position: Position, mult: float) -> float | None:
    since = features[features.index >= pd.Timestamp(position.entry_time)]
    if since.empty:
        return None
    highest = max(float(since["close"].max()), position.entry_price)
    return highest - mult * float(since["atr"].iloc[-1])


def build_signal(
    pair: str,
    candles: pd.DataFrame,
    benchmark: pd.DataFrame | None,
    settings: Settings,
    *,
    validation: Validation | None = None,
) -> Signal | None:
    """Signal for the last closed candle, or None when there is not enough history."""
    if candles.empty:
        return None
    features = compute_features(candles, benchmark)
    scores = score_frame(
        features,
        settings.weights,
        min_history=settings.min_history,
        enter_threshold=settings.enter_threshold,
    )
    i = len(features) - 1
    last = scores.iloc[i]
    if not bool(last["history_ok"]):
        return None

    weights = settings.weights.model_dump()
    reasons = [
        Reason(
            key=ev.key,
            label=ev.label,
            points=int(last[f"pts_{ev.key}"]),
            max_points=int(weights[ev.key]),
            detail=describe(ev.key, features, i, bool(last[f"pts_{ev.key}"] > 0)),
        )
        for ev in EVIDENCES
    ]
    score = int(last["score"])
    market_is_ok = bool(last["market_ok"])
    row = features.iloc[i]

    action = Action.WAIT
    exit_reason: str | None = None
    position = next((p for p in settings.positions if p.pair == pair), None)
    if position is not None:
        stop = trailing_stop(features, position, settings.trailing_atr_mult)
        if stop is not None and row.close < stop:
            action, exit_reason = (
                Action.EXIT,
                f"cierre {fmt(row.close)} bajo trailing stop {fmt(stop)}",
            )
        elif bool(exit_signal(features).iloc[i]):
            action, exit_reason = (
                Action.EXIT,
                f"cierre {fmt(row.close)} pierde la EMA50 {fmt(row.ema50)}",
            )
    elif bool(last["enter"]):
        action = Action.ENTER

    levels = None
    if action is not Action.EXIT and pd.notna(row.atr) and row.atr > 0:
        levels = compute_levels(
            float(row.close),
            float(row.atr),
            capital=settings.capital,
            risk_pct=settings.risk_per_trade_pct,
            max_positions=settings.max_positions,
            stop_atr_mult=settings.stop_atr_mult,
        )

    validated = validation is not None and validation.positive
    alert = action is Action.EXIT or (
        action is Action.ENTER and (validated or not settings.require_validation)
    )
    candle_open = features.index[i].to_pydatetime()
    return Signal(
        pair=pair,
        action=action,
        score=score,
        candle_open=candle_open,
        candle_close=candle_open + duration(settings.bar),
        close=float(row.close),
        reasons=reasons,
        market_ok=market_is_ok,
        market=market_text(features, i, market_is_ok),
        levels=levels,
        exit_reason=exit_reason,
        stop_atr_mult=settings.stop_atr_mult,
        validation=validation,
        alert=alert,
    )
