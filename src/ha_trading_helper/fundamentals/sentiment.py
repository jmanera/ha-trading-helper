"""The whole crypto market from CoinGecko's ``/global``: size, dominance and the Fear & Greed
Index, with a daily history kept by the app (the history endpoint is on paid plans only).
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from html import escape
from typing import Any

# CoinGecko's levels: 0-30 extreme fear, 31-45 fear, 46-55 neutral, 56-70 greed, 71-100.
LEVELS = (
    (30, "extreme_fear", "Miedo extremo"),
    (45, "fear", "Miedo"),
    (55, "neutral", "Neutral"),
    (70, "greed", "Codicia"),
    (100, "extreme_greed", "Codicia extrema"),
)
LABELS = {key: text for _, key, text in LEVELS}
GREED = {"greed", "extreme_greed"}
FEAR = {"fear", "extreme_fear"}
HISTORY_DAYS = 400


def level(value: float) -> str:
    return next((key for top, key, _ in LEVELS if value <= top), LEVELS[-1][1])


def label(sentiment: str) -> str:
    return LABELS.get(sentiment, sentiment)


def mood(sentiment: str) -> str:
    """Short wording for the market as a whole, like CoinGecko's sentence."""
    if sentiment in FEAR:
        return "bajista"
    return "alcista" if sentiment in GREED else "neutral"


@dataclass
class Global:
    ts: float
    market_cap: float
    volume: float
    market_cap_change_24h: float | None  # a fraction
    volume_change_24h: float | None
    dominance: dict[str, float] = field(default_factory=dict)  # fractions, largest first
    active: int | None = None
    markets: int | None = None
    fng: int | None = None
    sentiment: str = ""

    @classmethod
    def parse(cls, payload: dict[str, Any]) -> Global:
        d = payload.get("data", payload)
        fng = d.get("fear_and_greed_index") or {}
        value = fng.get("value")
        dominance = sorted(
            ((k, v / 100) for k, v in (d.get("market_cap_percentage") or {}).items()),
            key=lambda kv: kv[1],
            reverse=True,
        )

        def change(key: str) -> float | None:
            v = d.get(key)
            return v / 100 if v is not None else None

        return cls(
            ts=float(d.get("updated_at") or 0),
            market_cap=float((d.get("total_market_cap") or {}).get("usd") or 0),
            volume=float((d.get("total_volume") or {}).get("usd") or 0),
            market_cap_change_24h=change("market_cap_change_percentage_24h_usd"),
            volume_change_24h=change("volume_change_percentage_24h_usd"),
            dominance=dict(dominance),
            active=d.get("active_cryptocurrencies"),
            markets=d.get("markets"),
            fng=int(value) if value is not None else None,
            sentiment=fng.get("sentiment") or (level(value) if value is not None else ""),
        )

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Global:
        return cls(**data)


def record(history: dict[str, Any], day: date, g: Global) -> dict[str, Any]:
    """The day's last reading replaces the earlier ones; only the latest days are kept."""
    if g.fng is None:
        return history
    out = dict(history)
    out[day.isoformat()] = {
        "v": g.fng,
        "s": g.sentiment,
        "mc": g.market_cap,
        "btc": g.dominance.get("btc"),
    }
    return dict(sorted(out.items())[-HISTORY_DAYS:])


@dataclass
class Reading:
    day: date
    value: int
    sentiment: str

    @property
    def label(self) -> str:
        return label(self.sentiment)


@dataclass
class Summary:
    since: date | None = None
    days: int = 0
    streak: int = 0  # days in a row, ending on the last one, at the same level
    yesterday: Reading | None = None
    last_week: Reading | None = None
    last_month: Reading | None = None
    last_greed: Reading | None = None
    last_fear: Reading | None = None
    high: Reading | None = None
    low: Reading | None = None


def readings(history: dict[str, Any]) -> list[Reading]:
    return [
        Reading(date.fromisoformat(k), int(v["v"]), v.get("s") or level(v["v"]))
        for k, v in sorted(history.items())
    ]


def summarize(history: dict[str, Any], today: date) -> Summary:
    rows = readings(history)
    if not rows:
        return Summary()
    by_day = {r.day: r for r in rows}
    last = rows[-1]
    streak = 0
    day = last.day
    while (r := by_day.get(day)) is not None and r.sentiment == last.sentiment:
        streak += 1
        day -= timedelta(days=1)
    before = [r for r in rows if r.day < today]
    return Summary(
        since=rows[0].day,
        days=len(rows),
        streak=streak,
        yesterday=by_day.get(today - timedelta(days=1)),
        last_week=by_day.get(today - timedelta(days=7)),
        last_month=by_day.get(today - timedelta(days=30)),
        last_greed=next((r for r in reversed(before) if r.sentiment in GREED), None),
        last_fear=next((r for r in reversed(before) if r.sentiment in FEAR), None),
        high=max(rows, key=lambda r: r.value),
        low=min(rows, key=lambda r: r.value),
    )


# --- SVG drawn on the server, colours from the page's CSS ---------------------------------

GAUGE_W, GAUGE_H, GAUGE_R, GAUGE_STROKE = 240, 140, 100, 18


def _polar(value: float, radius: float) -> tuple[float, float]:
    """A value 0-100 on the half circle: 0 on the left, 100 on the right."""
    angle = math.pi * (1 - value / 100)
    cx, cy = GAUGE_W / 2, GAUGE_R + GAUGE_STROKE
    return cx + radius * math.cos(angle), cy - radius * math.sin(angle)


def gauge(value: int | None, sentiment: str) -> str:
    """A half-circle dial in five coloured bands with a needle at the value."""
    parts = [
        f'<svg class="gauge" viewBox="0 0 {GAUGE_W} {GAUGE_H}" role="img" '
        f'aria-label="Índice de miedo y codicia: {value if value is not None else "sin datos"}">'
    ]
    start = 0
    for top, key, text in LEVELS:
        x0, y0 = _polar(start + 0.8, GAUGE_R)
        x1, y1 = _polar(top - 0.8, GAUGE_R)
        parts.append(
            f'<path class="band {key}" d="M{x0:.1f},{y0:.1f} A{GAUGE_R},{GAUGE_R} 0 0 1 '
            f'{x1:.1f},{y1:.1f}"><title>{escape(text)}: {start}-{top}</title></path>'
        )
        start = top
    if value is not None:
        cx, cy = _polar(50, 0)
        x, y = _polar(value, GAUGE_R - GAUGE_STROKE / 2 - 6)
        parts.append(
            f'<line class="needle" x1="{cx:.1f}" y1="{cy:.1f}" x2="{x:.1f}" y2="{y:.1f}"/>'
            f'<circle class="hub" cx="{cx:.1f}" cy="{cy:.1f}" r="6"/>'
        )
        dx, dy = _polar(value, GAUGE_R)
        parts.append(f'<circle class="dot {sentiment}" cx="{dx:.1f}" cy="{dy:.1f}" r="7"/>')
    for v in (0, 100):
        x, y = _polar(v, GAUGE_R)
        parts.append(
            f'<text class="axis" x="{x:.1f}" y="{y + 16:.1f}" text-anchor="middle">{v}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


CHART_W, CHART_H = 480, 200
PAD_L, PAD_R, PAD_T, PAD_B = 34, 12, 10, 26


def history_chart(rows: list[Reading]) -> str:
    """The recorded index, 0-100, over the five levels' bands, from two readings on."""
    if len(rows) < 2:
        return ""
    first, last = rows[0].day, rows[-1].day
    span = max((last - first).days, 1)

    def x(d: date) -> float:
        return PAD_L + (CHART_W - PAD_L - PAD_R) * (d - first).days / span

    def y(v: float) -> float:
        return PAD_T + (CHART_H - PAD_T - PAD_B) * (1 - v / 100)

    parts = [
        f'<svg class="chart" viewBox="0 0 {CHART_W} {CHART_H}" role="img" '
        'aria-label="Histórico del índice de miedo y codicia">'
    ]
    start = 0
    for top, key, _ in LEVELS:
        parts.append(
            f'<rect class="zone {key}" x="{PAD_L}" y="{y(top):.1f}" '
            f'width="{CHART_W - PAD_L - PAD_R}" height="{y(start) - y(top):.1f}"/>'
        )
        start = top
    for v in (0, 25, 50, 75, 100):
        parts.append(
            f'<text class="axis" x="{PAD_L - 6}" y="{y(v) + 4:.1f}" text-anchor="end">{v}</text>'
        )
    step = max(1, len(rows) // 5)
    for r in rows[::step]:
        parts.append(
            f'<text class="axis" x="{x(r.day):.1f}" y="{CHART_H - 6}" '
            f'text-anchor="middle">{r.day:%d/%m}</text>'
        )
    pts = " ".join(f"{x(r.day):.1f},{y(r.value):.1f}" for r in rows)
    parts.append(f'<polyline class="line s0" points="{pts}"/>')
    for r in rows:
        parts.append(
            f'<circle class="dot {r.sentiment}" cx="{x(r.day):.1f}" cy="{y(r.value):.1f}" '
            f'r="{3 if len(rows) > 60 else 4}"><title>{r.day:%d/%m/%Y}: {r.value} '
            f"({escape(r.label)})</title></circle>"
        )
    parts.append("</svg>")
    return "".join(parts)
