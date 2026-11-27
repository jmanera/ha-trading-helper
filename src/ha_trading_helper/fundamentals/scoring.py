"""Fundamental scores of a coin from public data, explained metric by metric.

Each metric turns into 0-100 points along a straight ramp between two thresholds (below the
first 0, above the second 100; reversed when lower is better). A category is the weighted mean
of the metrics that have data, so a missing source lowers the coverage, not the score. The
thresholds are rules of thumb, not fitted to prices: the scores compare coins, they do not
predict returns.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal

from ha_trading_helper.fundamentals.fmt import big, num, pct

Outlook = Literal["alcista", "neutral", "bajista"]
Level = Literal["bajo", "medio", "alto"]

CATEGORY_LABELS = {
    "tokenomics": "Tokenomics",
    "adoption": "Adopción",
    "financial": "Finanzas",
    "development": "Desarrollo",
}
HIGH_RISK_PENALTY = 10.0
MIN_COVERAGE = 0.4


@dataclass(frozen=True)
class Inputs:
    """Raw facts about a coin; None when no source had them."""

    symbol: str
    market_cap: float | None = None  # USD
    fdv: float | None = None
    volume_24h: float | None = None  # every exchange, USD
    circulating: float | None = None
    total_supply: float | None = None
    max_supply: float | None = None
    supply_growth_1y: float | None = None  # circulating supply, fraction per year
    rank: int | None = None
    ath_change: float | None = None  # fraction below the all-time high (negative)
    age_years: float | None = None
    age_is_minimum: bool = False  # only known to be at least age_years old
    watchlist_users: int | None = None
    tvl: float | None = None
    tvl_change_30d: float | None = None
    fees_30d: float | None = None
    fees_prev_30d: float | None = None
    revenue_30d: float | None = None
    active_repos_30d: int | None = None
    days_since_push: float | None = None
    stars: int | None = None
    depth_2pct: float | None = None  # OKX book within ±2 % of the mid, both sides, USDC
    spread: float | None = None  # OKX best ask - best bid over the mid


@dataclass(frozen=True)
class Metric:
    label: str
    value: str
    points: float | None  # 0-100; for a risk, 100 = most risk
    weight: float
    note: str


@dataclass(frozen=True)
class Group:
    key: str
    label: str
    metrics: list[Metric]

    @property
    def score(self) -> float | None:
        known = [m for m in self.metrics if m.points is not None]
        weight = sum(m.weight for m in known)
        if not weight:
            return None
        return sum(m.points * m.weight for m in known if m.points is not None) / weight


@dataclass(frozen=True)
class Risk(Group):
    @property
    def level(self) -> Level | None:
        score = self.score
        if score is None:
            return None
        return "bajo" if score < 35 else "medio" if score < 65 else "alto"


@dataclass(frozen=True)
class Scores:
    categories: list[Group]
    dilution: Risk
    liquidity: Risk
    overall: float | None
    outlook: Outlook
    reasons: list[str] = field(default_factory=list)

    @property
    def coverage(self) -> float:
        """Share of metrics that had data."""
        metrics = [m for g in [*self.categories, self.dilution, self.liquidity] for m in g.metrics]
        return sum(m.points is not None for m in metrics) / len(metrics)


def ramp(x: float | None, zero: float, full: float) -> float | None:
    """0 at ``zero``, 100 at ``full``, straight in between and flat outside."""
    if x is None or math.isnan(x):
        return None
    t = (x - zero) / (full - zero)
    return 100.0 * min(1.0, max(0.0, t))


def _log(x: float | None) -> float | None:
    return math.log10(x) if x is not None and x > 0 else None


def _ratio(a: float | None, b: float | None) -> float | None:
    return a / b if a is not None and b else None


def _metric(label: str, value: str, points: float | None, weight: float, note: str) -> Metric:
    return Metric(label, value if points is not None else "sin datos", points, weight, note)


def circulating_ratio(i: Inputs) -> float | None:
    cap = i.max_supply or i.total_supply
    ratio = _ratio(i.circulating, cap)
    return min(ratio, 1.0) if ratio is not None else None


def tokenomics(i: Inputs) -> Group:
    circ = circulating_ratio(i)
    fdv = _ratio(i.fdv, i.market_cap)
    has_supply_data = i.circulating is not None
    return Group(
        "tokenomics",
        CATEGORY_LABELS["tokenomics"],
        [
            _metric(
                "Suministro en circulación",
                f"{pct(circ, 0)} del {'máximo' if i.max_supply else 'total'}",
                ramp(circ, 0.3, 0.95),
                35,
                "Cuanto más suministro circula ya, menos tokens nuevos quedan por soltar.",
            ),
            _metric(
                "Emisión en el último año",
                pct(i.supply_growth_1y, 1, sign=True),
                ramp(i.supply_growth_1y, 0.25, 0.0),
                35,
                "Crecimiento del suministro en circulación: diluye a quien ya tiene la moneda.",
            ),
            _metric(
                "Suministro máximo",
                f"fijo, {big(i.max_supply)}" if i.max_supply else "sin límite",
                (100.0 if i.max_supply else 40.0) if has_supply_data else None,
                10,
                "Un tope fijo impide emitir sin fin; sin tope depende de la política de emisión.",
            ),
            _metric(
                "FDV / capitalización",
                f"{num(fdv, 2)}×" if fdv is not None else "",
                ramp(fdv, 3.0, 1.0),
                20,
                "Valor con todo el suministro emitido frente al valor actual. 1× = sin dilución.",
            ),
        ],
    )


def adoption(i: Inputs) -> Group:
    age = (
        f"{'≥ ' if i.age_is_minimum else ''}{num(i.age_years, 1)} años"
        if i.age_years is not None
        else ""
    )
    return Group(
        "adoption",
        CATEGORY_LABELS["adoption"],
        [
            _metric(
                "Puesto por capitalización",
                f"#{i.rank}",
                ramp(_log(i.rank), math.log10(500), 0.0),
                25,
                "Su posición entre todas las criptomonedas de CoinGecko.",
            ),
            _metric(
                "Seguidores en CoinGecko",
                big(i.watchlist_users),
                ramp(_log(i.watchlist_users), 3.0, 6.0),
                25,
                "Usuarios que la tienen en una lista o cartera: interés del público.",
            ),
            _metric(
                "Valor bloqueado (TVL)",
                big(i.tvl, "$"),
                ramp(_log(i.tvl), 6.0, 10.0),
                25,
                "Dinero depositado en el protocolo o la cadena (DefiLlama). Solo DeFi y cadenas.",
            ),
            _metric(
                "TVL en 30 días",
                pct(i.tvl_change_30d, 1, sign=True),
                ramp(i.tvl_change_30d, -0.3, 0.3),
                10,
                "Si el valor bloqueado crece o se va.",
            ),
            _metric(
                "Antigüedad",
                age,
                ramp(i.age_years, 0.0, 5.0),
                15,
                "Los proyectos que han sobrevivido varios ciclos tienen más historial.",
            ),
        ],
    )


def financial(i: Inputs) -> Group:
    fee_yield = _ratio(i.fees_30d * 365 / 30 if i.fees_30d is not None else None, i.market_cap)
    rev_yield = _ratio(
        i.revenue_30d * 365 / 30 if i.revenue_30d is not None else None, i.market_cap
    )
    fees_trend = _ratio(i.fees_30d, i.fees_prev_30d)
    fees_trend = fees_trend - 1 if fees_trend is not None else None
    turnover = _ratio(i.volume_24h, i.market_cap)
    return Group(
        "financial",
        CATEGORY_LABELS["financial"],
        [
            _metric(
                "Comisiones al año / capitalización",
                pct(fee_yield, 2),
                ramp(fee_yield, 0.0, 0.10),
                25,
                "Lo que pagan los usuarios por usarlo, anualizado: demanda real del servicio.",
            ),
            _metric(
                "Ingresos al año / capitalización",
                pct(rev_yield, 2),
                ramp(rev_yield, 0.0, 0.05),
                25,
                "La parte de las comisiones que se queda el protocolo o sus tenedores.",
            ),
            _metric(
                "Comisiones frente al mes anterior",
                pct(fees_trend, 0, sign=True),
                ramp(fees_trend, -0.5, 0.5),
                15,
                "Últimos 30 días frente a los 30 anteriores.",
            ),
            _metric(
                "Volumen 24 h / capitalización",
                pct(turnover, 1),
                ramp(turnover, 0.005, 0.05),
                15,
                "Cuánto se negocia cada día respecto a lo que vale (todas las bolsas).",
            ),
            _metric(
                "Distancia al máximo histórico",
                pct(i.ath_change, 0),
                ramp(i.ath_change, -0.95, -0.2),
                10,
                "Una caída muy profunda desde el máximo indica que el mercado perdió la fe.",
            ),
            _metric(
                "Capitalización",
                big(i.market_cap, "$"),
                ramp(_log(i.market_cap), 7.0, 11.0),
                10,
                "Las monedas grandes son más difíciles de manipular.",
            ),
        ],
    )


def development(i: Inputs) -> Group:
    return Group(
        "development",
        CATEGORY_LABELS["development"],
        [
            _metric(
                "Repositorios activos en 30 días",
                str(i.active_repos_30d),
                ramp(i.active_repos_30d, 0.0, 15.0),
                40,
                "Repositorios propios de GitHub con cambios en el último mes.",
            ),
            _metric(
                "Último cambio en el código",
                f"hace {num(i.days_since_push, 0)} días" if i.days_since_push is not None else "",
                ramp(i.days_since_push, 180.0, 0.0),
                30,
                "Un proyecto sin cambios en meses puede estar abandonado.",
            ),
            _metric(
                "Estrellas en GitHub",
                big(i.stars),
                ramp(_log(i.stars), 2.0, 5.0),
                30,
                "Interés de los desarrolladores en el código del proyecto.",
            ),
        ],
    )


def dilution_risk(i: Inputs) -> Risk:
    circ = circulating_ratio(i)
    fdv = _ratio(i.fdv, i.market_cap)
    return Risk(
        "dilution",
        "Riesgo de dilución",
        [
            _metric(
                "Suministro por emitir",
                pct(1 - circ, 0) if circ is not None else "",
                ramp(circ, 0.95, 0.3),
                40,
                "Tokens que aún no circulan (desbloqueos, recompensas, tesorería).",
            ),
            _metric(
                "Emisión en el último año",
                pct(i.supply_growth_1y, 1, sign=True),
                ramp(i.supply_growth_1y, 0.0, 0.2),
                40,
                "Ritmo al que crece el suministro en circulación.",
            ),
            _metric(
                "FDV / capitalización",
                f"{num(fdv, 2)}×" if fdv is not None else "",
                ramp(fdv, 1.0, 3.0),
                20,
                "Cuántas veces cabría la capitalización actual en la totalmente diluida.",
            ),
        ],
    )


def liquidity_risk(i: Inputs) -> Risk:
    turnover = _ratio(i.volume_24h, i.market_cap)
    return Risk(
        "liquidity",
        "Riesgo de liquidez",
        [
            _metric(
                "Profundidad en OKX a ±2 %",
                big(i.depth_2pct, "USDC"),
                ramp(_log(i.depth_2pct), 6.5, 4.0),
                40,
                "Órdenes en el libro de OKX a menos de un 2 % del precio, compras más ventas.",
            ),
            _metric(
                "Diferencial en OKX",
                pct(i.spread, 3),
                ramp(i.spread, 0.0002, 0.005),
                20,
                "Distancia entre la mejor compra y la mejor venta: lo que cuesta entrar y salir.",
            ),
            _metric(
                "Volumen 24 h en todas las bolsas",
                big(i.volume_24h, "$"),
                ramp(_log(i.volume_24h), 9.0, 5.0),
                25,
                "Con poco volumen, una venta grande mueve mucho el precio.",
            ),
            _metric(
                "Volumen / capitalización",
                pct(turnover, 2),
                ramp(turnover, 0.03, 0.002),
                15,
                "Muy poca rotación: puede costar vender sin hundir el precio.",
            ),
        ],
    )


def score(i: Inputs) -> Scores:
    categories = [tokenomics(i), adoption(i), financial(i), development(i)]
    dilution, liquidity = dilution_risk(i), liquidity_risk(i)
    known = [(g, g.score) for g in categories if g.score is not None]
    high = [r for r in (dilution, liquidity) if r.level == "alto"]
    overall = None
    if known:
        mean = sum(s for _, s in known) / len(known)
        overall = max(0.0, min(100.0, mean - HIGH_RISK_PENALTY * len(high)))
    result = Scores(categories, dilution, liquidity, overall, "neutral")
    outlook, reasons = _outlook(i, result, known, high)
    return Scores(categories, dilution, liquidity, overall, outlook, reasons)


def _outlook(
    i: Inputs, s: Scores, known: list[tuple[Group, float]], high: list[Risk]
) -> tuple[Outlook, list[str]]:
    if s.overall is None or s.coverage < MIN_COVERAGE:
        return "neutral", [
            f"Solo hay datos para el {pct(s.coverage, 0)} de las métricas: no basta para opinar."
        ]
    reasons: list[str] = []
    best = max(known, key=lambda gs: gs[1])
    worst = min(known, key=lambda gs: gs[1])
    reasons.append(f"Lo más sólido: {best[0].label.lower()} ({num(best[1], 0)}/100).")
    if worst[0] is not best[0]:
        reasons.append(f"Lo más flojo: {worst[0].label.lower()} ({num(worst[1], 0)}/100).")
    for r in high:
        reasons.append(f"{r.label} alto: resta {num(HIGH_RISK_PENALTY, 0)} puntos.")
    trend = _trend(i)
    if trend is not None and trend < -0.2:
        reasons.append(f"El uso cae: {pct(trend, 0)} en 30 días.")

    if s.overall < 40 or len(high) == 2:
        return "bajista", reasons
    if s.overall >= 65 and not high and (trend is None or trend >= -0.2):
        return "alcista", reasons
    return "neutral", reasons


def _trend(i: Inputs) -> float | None:
    """How usage changed in 30 days: fees if known, otherwise TVL."""
    ratio = _ratio(i.fees_30d, i.fees_prev_30d)
    if ratio is not None:
        return ratio - 1
    return i.tvl_change_30d
