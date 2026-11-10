"""Candle timeframes: one table for durations, the schedule and OKX bar names."""

from __future__ import annotations

from datetime import timedelta
from typing import Literal

Timeframe = Literal["1H", "2H", "4H", "6H", "12H", "1D"]

HOURS: dict[str, int] = {"1H": 1, "2H": 2, "4H": 4, "6H": 6, "12H": 12, "1D": 24}


def duration(bar: str) -> timedelta:
    return timedelta(hours=HOURS[bar])


def bars_per_year(bar: str) -> int:
    return 24 * 365 // HOURS[bar]


def close_hours(bar: str) -> str:
    """UTC hours at which a candle closes, as a cron ``hour`` field."""
    return ",".join(str(h) for h in range(0, 24, HOURS[bar]))


def okx_bar(bar: str) -> str:
    """OKX aligns 6H and longer candles to Hong Kong time unless asked for the UTC variant."""
    return f"{bar}utc" if HOURS.get(bar, 0) > 4 else bar
