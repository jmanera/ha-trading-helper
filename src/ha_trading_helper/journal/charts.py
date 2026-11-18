"""Small inline SVG charts drawn on the server: no JavaScript, colours from the page's CSS."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from html import escape

W, H = 760, 220
PAD_L, PAD_R, PAD_T, PAD_B = 56, 12, 12, 26


def _fmt(v: float) -> str:
    if abs(v) >= 1000:
        return f"{v / 1000:.1f}k".replace(".", ",")
    return f"{v:.2f}".replace(".", ",") if abs(v) < 10 else f"{v:.0f}"


def _scale(lo: float, hi: float) -> tuple[float, float]:
    if hi - lo < 1e-9:
        pad = abs(hi) * 0.05 or 1.0
        return lo - pad, hi + pad
    pad = (hi - lo) * 0.08
    return lo - pad, hi + pad


def _frame(lo: float, hi: float, days: Sequence[date], label: str) -> list[str]:
    parts = [f'<svg class="chart" viewBox="0 0 {W} {H}" role="img" aria-label="{escape(label)}">']
    for i in range(4):
        v = lo + (hi - lo) * i / 3
        y = _y(v, lo, hi)
        parts.append(
            f'<line class="grid" x1="{PAD_L}" x2="{W - PAD_R}" y1="{y:.1f}" y2="{y:.1f}"/>'
        )
        parts.append(
            f'<text class="axis" x="{PAD_L - 6}" y="{y + 4:.1f}" text-anchor="end">{_fmt(v)}</text>'
        )
    if days:
        step = max(1, len(days) // 6)
        for i in range(0, len(days), step):
            x = _x(i, len(days))
            parts.append(
                f'<text class="axis" x="{x:.1f}" y="{H - 6}" text-anchor="middle">'
                f"{days[i]:%d/%m}</text>"
            )
    return parts


def _x(i: int, n: int) -> float:
    if n <= 1:
        return (PAD_L + W - PAD_R) / 2
    return PAD_L + (W - PAD_L - PAD_R) * i / (n - 1)


def _y(v: float, lo: float, hi: float) -> float:
    return PAD_T + (H - PAD_T - PAD_B) * (1 - (v - lo) / (hi - lo))


def line_chart(
    days: Sequence[date],
    series: Sequence[tuple[str, Sequence[float]]],
    label: str,
) -> str:
    """One line per series; the first is drawn as the main one with a soft area under it."""
    values = [v for _, s in series for v in s]
    if not days or not values:
        return ""
    lo, hi = _scale(min(values), max(values))
    parts = _frame(lo, hi, days, label)
    n = len(days)
    for idx, (name, s) in enumerate(series):
        pts = " ".join(f"{_x(i, n):.1f},{_y(v, lo, hi):.1f}" for i, v in enumerate(s))
        if idx == 0 and n > 1:
            base = _y(max(lo, min(0.0, hi)), lo, hi) if lo < 0 < hi else H - PAD_B
            parts.append(
                f'<polygon class="area" points="{_x(0, n):.1f},{base:.1f} {pts} '
                f'{_x(n - 1, n):.1f},{base:.1f}"/>'
            )
        parts.append(
            f'<polyline class="line s{idx}" points="{pts}"><title>{escape(name)}</title></polyline>'
        )
        if n == 1:
            parts.append(
                f'<circle class="dot s{idx}" cx="{_x(0, 1):.1f}" '
                f'cy="{_y(s[0], lo, hi):.1f}" r="4"/>'
            )
    parts.append("</svg>")
    return "".join(parts)


def bar_chart(days: Sequence[date], values: Sequence[float], label: str) -> str:
    """Daily bars, green above zero and red below."""
    if not days:
        return ""
    lo, hi = _scale(min(0.0, *values), max(0.0, *values))
    parts = _frame(lo, hi, days, label)
    n = len(days)
    slot = (W - PAD_L - PAD_R) / max(n - 1, 1)
    width = max(1.5, min(18.0, slot * 0.7))
    zero = _y(0.0, lo, hi)
    for i, (d, v) in enumerate(zip(days, values, strict=True)):
        x = _x(i, n) - width / 2
        y = _y(v, lo, hi)
        top, height = (y, zero - y) if v >= 0 else (zero, y - zero)
        cls = "pos" if v >= 0 else "neg"
        parts.append(
            f'<rect class="bar {cls}" x="{x:.1f}" y="{top:.1f}" width="{width:.1f}" '
            f'height="{max(height, 0.5):.1f}"><title>{d:%d/%m}: {_fmt(v)}</title></rect>'
        )
    parts.append(
        f'<line class="zero" x1="{PAD_L}" x2="{W - PAD_R}" y1="{zero:.1f}" y2="{zero:.1f}"/>'
    )
    parts.append("</svg>")
    return "".join(parts)
