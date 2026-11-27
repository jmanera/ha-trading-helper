"""Numbers written the Spanish way (1.234,5), shared by the scores' texts and the pages."""

from __future__ import annotations

import math


def num(value: float, decimals: int = 2) -> str:
    text = f"{value:,.{decimals}f}"
    return text.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def big(value: float | None, unit: str = "") -> str:
    """1.234 → «1.234», 2,5e6 → «2,5 M», 3,1e9 → «3,1 mil M»."""
    if value is None or math.isnan(value):
        return "—"
    suffix = f" {unit}" if unit else ""
    size = abs(value)
    if size >= 1e9:
        return f"{num(value / 1e9, 1)} mil M{suffix}"
    if size >= 1e6:
        return f"{num(value / 1e6, 1)} M{suffix}"
    if size >= 1e4:
        return f"{num(value / 1e3, 0)} mil{suffix}"
    return f"{num(value, 0)}{suffix}"


def pct(value: float | None, decimals: int = 1, *, sign: bool = False) -> str:
    """A fraction as a percentage: 0.0123 → «1,2 %» («+1,2 %» with ``sign``)."""
    if value is None or math.isnan(value):
        return "—"
    text = f"{num(value * 100, decimals)} %"
    return f"+{text}" if sign and value > 0 else text


def price(value: float | None) -> str:
    """Enough significant digits for a price of any size."""
    if value is None or math.isnan(value):
        return "—"
    if value == 0:
        return "0"
    digits = max(0, min(10, 5 - math.floor(math.log10(abs(value)))))
    text = num(value, digits)
    return text.rstrip("0").rstrip(",") if "," in text else text
