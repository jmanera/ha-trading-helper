"""Why a signal is what it is, in plain Spanish for someone who does not trade.

Built from the same evidences and filters that decided the signal, never from free
text, so it cannot say anything the rules did not see. Every explanation ends with
what the validation says, because a reader without trading experience is the one
most likely to take a signal as advice.
"""

from __future__ import annotations

import pandas as pd
from pydantic import BaseModel

from ha_trading_helper.strategy.actions import Action
from ha_trading_helper.strategy.explain import fmt
from ha_trading_helper.strategy.features import BREAKOUT_PERIOD, EMA_FAST, EMA_SLOW, RS_PERIOD
from ha_trading_helper.strategy.risk import Levels
from ha_trading_helper.timeframe import HOURS


class Point(BaseModel):
    """One idea: an icon, a short bold title and one or two sentences."""

    icon: str
    title: str
    text: str = ""


def span(bars: int, bar: str) -> str:
    """How long ``bars`` candles of ``bar`` last, in words."""
    hours = bars * HOURS[bar]
    if hours < 48:
        return f"unas {hours} horas"
    return f"unos {round(hours / 24)} días"


def _side(a: float, b: float) -> str:
    return "por encima" if a > b else "por debajo"


def summary(action: Action, score: int, threshold: int) -> Point:
    if action is Action.ENTER:
        return Point(
            icon="🟢",
            title="Las reglas ven motivos para comprar",
            text=f"Ha sumado {score} de 100 puntos y hacen falta {threshold}.",
        )
    if action is Action.EXIT:
        return Point(icon="🔴", title="Las reglas indican vender la posición abierta")
    return Point(
        icon="🟡",
        title="Mejor esperar",
        text=f"Ha sumado {score} de 100 puntos y hacen falta {threshold} para comprar.",
    )


def trend_point(row: pd.Series, passed: bool, bar: str) -> Point:
    fast, slow = span(EMA_FAST, bar), span(EMA_SLOW, bar)
    if passed:
        return Point(
            icon="📈",
            title="Va hacia arriba",
            text=(
                f"El precio está por encima de su media de {fast} (línea azul) y esa media "
                f"por encima de la de {slow} (línea morada): lleva semanas subiendo."
            ),
        )
    return Point(
        icon="📉",
        title="No va claramente hacia arriba",
        text=(
            f"El precio está {_side(row.close, row.ema50)} de su media de {fast} y "
            f"{_side(row.close, row.ema200)} de la de {slow}, y la media corta está "
            f"{_side(row.ema50, row.ema200)} de la larga. Estas reglas solo compran con el "
            "precio por encima de las dos y la corta por encima de la larga, porque comprar "
            "algo que está cayendo suele salir mal."
        ),
    )


def evidence_points(row: pd.Series, passed: dict[str, bool], bar: str) -> list[Point]:
    """The evidences that scored, apart from the trend, in the order they are drawn."""
    out: list[Point] = []
    if passed.get("breakout"):
        out.append(
            Point(
                icon="🚀",
                title=f"Ha superado su máximo de {span(BREAKOUT_PERIOD, bar)}",
                text="(línea discontinua). Cuando un precio rompe un techo reciente, a veces "
                "sigue subiendo.",
            )
        )
    if passed.get("volume"):
        out.append(
            Point(
                icon="🔊",
                title="Se está negociando mucho más de lo normal",
                text=f"{fmt(row.rel_volume, 1)} veces el volumen habitual (barras naranjas). "
                "Un movimiento con mucho volumen es más creíble.",
            )
        )
    if passed.get("rsi"):
        out.append(
            Point(
                icon="🔁",
                title="Venía de un pequeño descanso y vuelve a subir",
                text="El RSI sale de la zona amarilla: es comprar tras una bajada pasajera.",
            )
        )
    if passed.get("momentum"):
        out.append(
            Point(
                icon="⚡",
                title="Está acelerando",
                text="Las barras del MACD acaban de pasar a verde: la subida gana fuerza.",
            )
        )
    if passed.get("relative_strength"):
        diff = (row.ret30 - row.btc_ret30) * 100
        out.append(
            Point(
                icon="🏁",
                title="Lo está haciendo mejor que Bitcoin",
                text=f"{fmt(diff, 1)} puntos porcentuales más en {span(RS_PERIOD, bar)}.",
            )
        )
    return out


def market_point(market_ok: bool, bar: str) -> Point:
    slow = span(EMA_SLOW, bar)
    if market_ok:
        return Point(
            icon="🌍",
            title="El mercado en general acompaña",
            text=f"Bitcoin está por encima de su media de {slow}.",
        )
    return Point(
        icon="🌍",
        title="El mercado en general está flojo",
        text=f"Bitcoin está por debajo de su media de {slow}. Así estas reglas no compran nada.",
    )


def plan_point(levels: Levels) -> Point:
    loss = (levels.entry - levels.stop) / levels.entry * 100
    return Point(
        icon="🛡️",
        title="Si compras, el plan sería",
        text=(
            f"vender si baja a {fmt(levels.stop)} (perderías un {fmt(loss, 1)} %, línea roja) "
            f"y recoger beneficios en {fmt(levels.target1)} y {fmt(levels.target2)} "
            "(líneas verdes)."
        ),
    )


def validation_point(positive: bool | None) -> Point:
    """``positive`` is None when there is no backtest yet."""
    if positive is None:
        text = "Estas reglas todavía no se han probado con datos pasados."
    elif positive:
        text = (
            "Probadas con datos pasados, estas reglas ganaron dinero, pero eso no garantiza "
            "que lo sigan haciendo."
        )
    else:
        text = "Probadas con datos pasados, estas reglas no han ganado dinero."
    return Point(
        icon="⚠️",
        title="Importante",
        text=f"{text} Esto explica lo que ven los indicadores; no es una recomendación.",
    )


def explain_plain(
    action: Action,
    *,
    score: int,
    threshold: int,
    row: pd.Series,
    passed: dict[str, bool],
    market_ok: bool,
    bar: str,
    levels: Levels | None = None,
    exit_reason: str | None = None,
    validation_positive: bool | None = None,
) -> list[Point]:
    points = [summary(action, score, threshold)]
    if action is Action.EXIT:
        points.append(
            Point(icon="↘️", title="Motivo de la salida", text=exit_reason or "no indicado")
        )
    else:
        points.append(trend_point(row, passed.get("trend", False), bar))
        points += evidence_points(row, passed, bar)
        points.append(market_point(market_ok, bar))
        if action is Action.ENTER and levels is not None:
            points.append(plan_point(levels))
    points.append(validation_point(validation_positive))
    return points
