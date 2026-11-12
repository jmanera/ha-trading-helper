from __future__ import annotations

import pandas as pd
import pytest

from ha_trading_helper.config import Settings
from ha_trading_helper.strategy.actions import Action
from ha_trading_helper.strategy.plain import Point, explain_plain, span
from ha_trading_helper.strategy.risk import compute_levels
from ha_trading_helper.strategy.signal import build_signal
from tests.unit.test_strategy import bull, crash, flat  # noqa: F401  (fixtures)

ROW = pd.Series(
    {"close": 105.0, "ema50": 100.0, "ema200": 90.0, "rel_volume": 1.84, "ret30": 0.20,
     "btc_ret30": 0.05}
)  # fmt: skip
ALL = dict.fromkeys(["trend", "breakout", "volume", "rsi", "momentum", "relative_strength"], True)


def titles(points: list[Point]) -> list[str]:
    return [p.title for p in points]


def text(points: list[Point]) -> str:
    return "\n".join(f"{p.title} {p.text}" for p in points)


def test_span_in_hours_or_days() -> None:
    assert span(20, "1H") == "unas 20 horas"
    assert span(50, "4H") == "unos 8 días"
    assert span(200, "4H") == "unos 33 días"
    assert span(20, "1D") == "unos 20 días"


def test_enter_with_every_evidence_and_a_plan() -> None:
    levels = compute_levels(105.0, 2.0, capital=10_000, risk_pct=1, max_positions=3)
    points = explain_plain(
        Action.ENTER, score=100, threshold=70, row=ROW, passed=ALL, market_ok=True, bar="4H",
        levels=levels, validation_positive=True,
    )  # fmt: skip
    assert titles(points) == [
        "Las reglas ven motivos para comprar",
        "Va hacia arriba",
        "Ha superado su máximo de unos 3 días",
        "Se está negociando mucho más de lo normal",
        "Venía de un pequeño descanso y vuelve a subir",
        "Está acelerando",
        "Lo está haciendo mejor que Bitcoin",
        "El mercado en general acompaña",
        "Si compras, el plan sería",
        "Importante",
    ]
    t = text(points)
    assert "100 de 100 puntos y hacen falta 70" in t
    assert "1,8 veces el volumen" in t and "15,0 puntos porcentuales más en unos 5 días" in t
    assert "vender si baja a 101" in t and "perderías un 3,8 %" in t
    assert "ganaron dinero, pero eso no garantiza" in t
    assert all(p.icon for p in points)


def test_wait_explains_what_is_missing() -> None:
    row = ROW.copy()
    row["close"], row["ema50"], row["ema200"] = 95.0, 92.0, 100.0
    points = explain_plain(
        Action.WAIT, score=15, threshold=70, row=row, passed={"relative_strength": True},
        market_ok=False, bar="4H",
    )  # fmt: skip
    t = text(points)
    assert titles(points)[0] == "Mejor esperar"
    assert "15 de 100 puntos y hacen falta 70 para comprar" in t
    assert "por encima de su media de unos 8 días y por debajo de la de unos 33 días" in t
    assert "la media corta está por debajo de la larga" in t
    assert "Así estas reglas no compran nada" in t
    assert "todavía no se han probado" in t
    assert "Si compras" not in t and "Ha superado" not in t


def test_enter_without_levels_has_no_plan() -> None:
    points = explain_plain(
        Action.ENTER, score=80, threshold=70, row=ROW, passed={"trend": True}, market_ok=True,
        bar="1D", validation_positive=False,
    )  # fmt: skip
    assert "Si compras, el plan sería" not in titles(points)
    assert "no han ganado dinero" in text(points)


@pytest.mark.parametrize(("reason", "shown"), [("cierre bajo trailing stop 9", "trailing"),
                                               (None, "no indicado")])  # fmt: skip
def test_exit_gives_the_reason_only(reason: str | None, shown: str) -> None:
    points = explain_plain(
        Action.EXIT, score=10, threshold=70, row=ROW, passed=ALL, market_ok=True, bar="4H",
        exit_reason=reason,
    )  # fmt: skip
    assert titles(points) == [
        "Las reglas indican vender la posición abierta",
        "Motivo de la salida",
        "Importante",
    ]
    assert shown in points[1].text


def test_every_signal_carries_its_explanation(bull: pd.DataFrame, flat: pd.DataFrame) -> None:  # noqa: F811
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    enter = build_signal("SOL-USDT", bull, None, s)
    wait = build_signal("XRP-USDT", flat, None, s)
    assert enter is not None and wait is not None
    assert enter.explanation[0].title == "Las reglas ven motivos para comprar"
    assert "Si compras, el plan sería" in titles(enter.explanation)
    assert wait.explanation[0].title == "Mejor esperar"
    assert enter.explanation[-1].title == wait.explanation[-1].title == "Importante"
