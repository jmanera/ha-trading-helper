"""Stop, targets and position size for a long entry."""

from __future__ import annotations

from pydantic import BaseModel


class Levels(BaseModel):
    entry: float
    stop: float
    target1: float
    target2: float
    risk_per_unit: float
    size: float  # units of the base currency
    notional: float  # in quote currency
    risk_amount: float


def compute_levels(
    entry: float,
    atr: float,
    *,
    capital: float,
    risk_pct: float,
    max_positions: int,
    stop_atr_mult: float = 2.0,
) -> Levels:
    """Stop at ``stop_atr_mult`` x ATR below entry, targets at 1.5R and 3R.

    The size risks ``risk_pct`` % of capital, capped so a single position never
    uses more than ``capital / max_positions``.
    """
    if entry <= 0 or atr <= 0:
        raise ValueError("entry and atr must be positive")
    risk_unit = stop_atr_mult * atr
    stop = max(entry - risk_unit, 0.0)
    risk_unit = entry - stop
    risk_amount = capital * risk_pct / 100
    size = risk_amount / risk_unit if risk_unit > 0 else 0.0
    max_notional = capital / max(max_positions, 1)
    if size * entry > max_notional:
        size = max_notional / entry
    return Levels(
        entry=entry,
        stop=stop,
        target1=entry + 1.5 * risk_unit,
        target2=entry + 3.0 * risk_unit,
        risk_per_unit=risk_unit,
        size=size,
        notional=size * entry,
        risk_amount=size * risk_unit,
    )
