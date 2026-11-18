"""Value of any currency in USDC, on a given day or at a given moment (pure, no I/O).

Sources, best first: the daily closes stored from OKX, prices seen in the account's own
fills and conversions, and the current price. A currency without any of them is worth
nothing (``None``) and the caller flags the figure as estimated.
"""

from __future__ import annotations

import bisect
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta

from ha_trading_helper.journal.config import REFERENCE
from ha_trading_helper.journal.models import Bill, Fill, day_of, split_inst

# Stablecoins pegged to the dollar: worth 1 USDC when nothing better is known.
DOLLARS = {"USDC", "USDT", "USD", "USDG", "DAI", "FDUSD", "PYUSD"}
MAX_GAP = timedelta(days=7)


def price_candidates(ccy: str) -> list[tuple[str, bool]]:
    """Instruments that price ``ccy`` in USDC: (instId, inverted)."""
    return [(f"{ccy}-{REFERENCE}", False), (f"{REFERENCE}-{ccy}", True), (f"{ccy}-USDT", False)]


def from_tickers(ccy: str, last: dict[str, float]) -> float | None:
    if ccy == REFERENCE:
        return 1.0
    for inst, inverted in price_candidates(ccy):
        px = last.get(inst)
        if px:
            return 1 / px if inverted else px
    return 1.0 if ccy in DOLLARS else None


@dataclass
class PriceBook:
    closes: dict[str, dict[date, float]] = field(default_factory=dict)
    live: dict[str, float] = field(default_factory=dict)
    _points: dict[str, list[tuple[int, float]]] = field(default_factory=dict)

    def add_point(self, ccy: str, ts: int, px: float) -> None:
        if px > 0 and ccy != REFERENCE:
            bisect.insort(self._points.setdefault(ccy, []), (ts, px))

    def add_instrument_price(self, inst_id: str, ts: int, px: float | None) -> None:
        """A price of ``base`` in ``quote`` seen at ``ts``; only USDC quotes are usable."""
        if not px:
            return
        base, quote = split_inst(inst_id)
        if quote == REFERENCE:
            self.add_point(base, ts, px)
        elif base == REFERENCE:
            self.add_point(quote, ts, 1 / px)

    def learn(self, fills: Iterable[Fill], bills: Iterable[Bill]) -> PriceBook:
        for f in fills:
            self.add_instrument_price(f.inst_id, f.ts, f.px)
        for b in bills:
            self.add_instrument_price(b.inst_id, b.ts, b.px)
        return self

    def on(self, ccy: str, day: date) -> float | None:
        """Close of ``day`` (or the nearest one within a week)."""
        if ccy == REFERENCE:
            return 1.0
        series = self.closes.get(ccy, {})
        if day in series:
            return series[day]
        for gap in range(1, MAX_GAP.days + 1):
            for d in (day - timedelta(days=gap), day + timedelta(days=gap)):
                if d in series:
                    return series[d]
        point = self._nearest_point(ccy, None, day)
        if point is not None:
            return point
        if ccy in self.live:
            return self.live[ccy]
        return 1.0 if ccy in DOLLARS else None

    def at(self, ccy: str, ts: int) -> float | None:
        """Price at a moment: an own trade that same day beats the daily close."""
        if ccy == REFERENCE:
            return 1.0
        point = self._nearest_point(ccy, ts, day_of(ts))
        return point if point is not None else self.on(ccy, day_of(ts))

    def now(self, ccy: str) -> float | None:
        if ccy == REFERENCE:
            return 1.0
        if ccy in self.live:
            return self.live[ccy]
        return 1.0 if ccy in DOLLARS else None

    def _nearest_point(self, ccy: str, ts: int | None, day: date) -> float | None:
        points = self._points.get(ccy)
        if not points:
            return None
        same_day = [p for p in points if day_of(p[0]) == day]
        if not same_day:
            return None
        if ts is None:
            return same_day[-1][1]
        return min(same_day, key=lambda p: abs(p[0] - ts))[1]
