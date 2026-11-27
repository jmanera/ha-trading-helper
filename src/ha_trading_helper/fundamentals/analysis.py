"""Gathers what every source knows about one coin into scoring ``Inputs``.

Each source fails on its own: the analysis keeps what the others gave and says which one
had no data or failed. The facts (not the scores) are cached, so a change in the scoring
shows at once.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import urlparse

import structlog

from ha_trading_helper.fundamentals.cache import Cache
from ha_trading_helper.fundamentals.scoring import Inputs, Scores, score
from ha_trading_helper.fundamentals.sources import CoinGecko, DefiLlama, GitHub
from ha_trading_helper.okx.client import OkxClient

DAY = 86_400.0
MARKET_PAGES = 5  # the 1.250 largest coins: enough to match almost every OKX listing
DEPTH_BAND = 0.02
ACTIVE_DAYS = 30

log = structlog.get_logger(__name__)


@dataclass
class Analysis:
    symbol: str
    inst_id: str
    inputs: Inputs
    ts: float
    coin_id: str | None = None
    name: str = ""
    description: str = ""
    image: str = ""
    categories: list[str] = field(default_factory=list)
    links: dict[str, str] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)  # source -> "ok" or what went wrong

    @property
    def scores(self) -> Scores:
        return score(self.inputs)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Analysis:
        return cls(**{**data, "inputs": Inputs(**data["inputs"])})


def supply_growth(chart: dict[str, Any]) -> tuple[float | None, float]:
    """(annual growth of the circulating supply, days covered) from a daily market chart.

    CoinGecko has no supply history on the free plan, but market cap / price is the
    circulating supply of each day.
    """
    points = [
        (p[0], m[1] / p[1])
        for p, m in zip(chart.get("prices", []), chart.get("market_caps", []), strict=False)
        if p[1] and m[1]
    ]
    if len(points) < 2:
        return None, 0.0
    (t0, s0), (t1, s1) = points[0], points[-1]
    days = (t1 - t0) / 1000 / DAY
    if days < 90:
        return None, days
    return (s1 / s0) ** (365 / days) - 1, days


def book_depth(
    bids: list[tuple[float, float]], asks: list[tuple[float, float]], band: float = DEPTH_BAND
) -> tuple[float | None, float | None]:
    """(quote value within ±band of the mid on both sides, spread over the mid)."""
    if not bids or not asks:
        return None, None
    mid = (bids[0][0] + asks[0][0]) / 2
    depth = sum(p * s for p, s in bids if p >= mid * (1 - band))
    depth += sum(p * s for p, s in asks if p <= mid * (1 + band))
    return depth, (asks[0][0] - bids[0][0]) / mid


def repo_activity(repos: list[dict[str, Any]], now: datetime) -> tuple[int, float | None, int]:
    """(repositories pushed in the last 30 days, days since the last push, stars)."""
    own = [r for r in repos if not r.get("fork") and not r.get("archived")]
    pushes = [
        datetime.fromisoformat(r["pushed_at"].replace("Z", "+00:00"))
        for r in own
        if r.get("pushed_at")
    ]
    ages = [(now - p).total_seconds() / DAY for p in pushes]
    active = sum(a <= ACTIVE_DAYS for a in ages)
    stars = sum(int(r.get("stargazers_count") or 0) for r in own)
    return active, min(ages) if ages else None, stars


def github_owner(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.netloc.lower() not in ("github.com", "www.github.com"):
        return None
    parts = [p for p in parsed.path.split("/") if p]
    return parts[0] if parts else None


def plain_text(html: str, limit: int = 600) -> str:
    text = re.sub(r"<[^>]+>", "", html or "")
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(". ", 1)[0]
    return cut + ("." if not cut.endswith(".") else "")


def _sum(entries: list[dict[str, Any]], key: str) -> float | None:
    values = [e[key] for e in entries if isinstance(e.get(key), int | float)]
    return float(sum(values)) if values else None


class Analyzer:
    def __init__(
        self,
        *,
        coingecko: CoinGecko,
        defillama: DefiLlama,
        github: GitHub,
        okx: OkxClient,
        cache: Cache,
        cache_hours: float = 12.0,
        coin_ids: dict[str, str] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.cg = coingecko
        self.llama = defillama
        self.github = github
        self.okx = okx
        self.cache = cache
        self.ttl = cache_hours * 3600
        self.coin_ids = {k.upper(): v for k, v in (coin_ids or {}).items()}
        self._clock = clock
        self._locks: dict[str, asyncio.Lock] = {}

    def cached(self, symbol: str) -> Analysis | None:
        data = self.cache.get(f"analysis:{symbol.upper()}", self.ttl)
        return Analysis.from_json(data) if data else None

    async def analyze(
        self, symbol: str, inst_id: str, *, listed: int | None = None, refresh: bool = False
    ) -> Analysis:
        symbol = symbol.upper()
        lock = self._locks.setdefault(symbol, asyncio.Lock())
        async with lock:
            if not refresh and (hit := self.cached(symbol)):
                return hit
            result = await self._gather(symbol, inst_id, listed)
            self.cache.put(f"analysis:{symbol}", result.to_json())
            return result

    async def symbol_map(self) -> dict[str, str]:
        """Symbol → CoinGecko id of the largest coin with that symbol."""

        async def load() -> dict[str, str]:
            mapping: dict[str, str] = {}
            for page in range(1, MARKET_PAGES + 1):
                for coin in await self.cg.markets(page):
                    mapping.setdefault(str(coin["symbol"]).upper(), str(coin["id"]))
            return mapping

        result: dict[str, str] = await self.cache.fetch("cg:symbols", DAY, load)
        return result

    async def coin_id(self, symbol: str) -> str | None:
        if symbol in self.coin_ids:
            return self.coin_ids[symbol]
        return (await self.symbol_map()).get(symbol)

    async def _gather(self, symbol: str, inst_id: str, listed: int | None) -> Analysis:
        facts: dict[str, Any] = {}
        a = Analysis(symbol, inst_id, Inputs(symbol), self._clock())
        now = datetime.fromtimestamp(self._clock(), tz=UTC)
        github_owners: list[str] = []

        async def step(name: str, run: Callable[[], Any]) -> None:
            try:
                note = await run()
                a.sources[name] = note or "ok"
            except Exception as exc:
                log.warning(
                    "fundamentals_source_failed", source=name, symbol=symbol, error=str(exc)
                )
                a.sources[name] = f"error: {exc}"

        async def coingecko() -> str | None:
            a.coin_id = await self.coin_id(symbol)
            if not a.coin_id:
                return "no la encuentra entre las 1.250 mayores"
            coin = await self.cg.coin(a.coin_id)
            if not coin:
                return f"no existe «{a.coin_id}»"
            m = coin.get("market_data") or {}
            usd = {
                k: (m.get(k) or {}).get("usd")
                for k in ("market_cap", "fully_diluted_valuation", "total_volume")
            }
            facts.update(
                market_cap=usd["market_cap"] or None,
                fdv=usd["fully_diluted_valuation"] or None,
                volume_24h=usd["total_volume"],
                circulating=m.get("circulating_supply") or None,
                total_supply=m.get("total_supply") or None,
                max_supply=m.get("max_supply") or None,
                rank=coin.get("market_cap_rank"),
                watchlist_users=coin.get("watchlist_portfolio_users"),
            )
            ath = (m.get("ath_change_percentage") or {}).get("usd")
            if ath is not None:
                facts["ath_change"] = ath / 100
            if genesis := coin.get("genesis_date"):
                with_age = (now.date() - date.fromisoformat(genesis)).days / 365
                facts["age_years"] = round(with_age, 2)
            a.name = coin.get("name", "")
            a.image = (coin.get("image") or {}).get("small", "")
            a.description = plain_text((coin.get("description") or {}).get("en", ""))
            a.categories = [c for c in coin.get("categories") or [] if c][:8]
            links = coin.get("links") or {}
            a.links["CoinGecko"] = f"https://www.coingecko.com/es/monedas/{a.coin_id}"
            if home := next((u for u in links.get("homepage") or [] if u), None):
                a.links["Web"] = home
            for url in (links.get("repos_url") or {}).get("github") or []:
                if owner := github_owner(url):
                    github_owners.append(owner)

            chart = await self.cg.market_chart(a.coin_id)
            growth, days = supply_growth(chart or {})
            facts["supply_growth_1y"] = growth
            if "age_years" not in facts:
                # The free chart reaches one year back, and OKX lists coins after they launch:
                # both only give a minimum age.
                okx_days = (now.timestamp() - listed / 1000) / DAY if listed else 0.0
                if max(days, okx_days) > 0:
                    facts["age_years"] = round(max(days, okx_days) / 365, 2)
                    facts["age_is_minimum"] = True
            return None

        await step("CoinGecko", coingecko)

        async def defillama() -> str | None:
            if not a.coin_id:
                return "sin moneda de CoinGecko con la que buscarla"
            found = await self._defillama(a.coin_id, now, facts, a.links, github_owners)
            if found:
                return None
            # Not a protocol nor a chain: no fees anyone pays for using it.
            facts.update(fees_30d=0.0, revenue_30d=0.0)
            return "no es un protocolo ni una cadena: sin comisiones"

        async def github() -> str | None:
            owner = next(iter(dict.fromkeys(github_owners)), None)
            if not owner:
                if a.sources.get("CoinGecko") == "ok":
                    # Neither CoinGecko nor DefiLlama know any public code.
                    facts.update(active_repos_30d=0, stars=0)
                    return "sin repositorio público"
                return "sin repositorio conocido"
            repos = await self.cache.fetch(f"gh:{owner}", DAY, lambda: self.github.repos(owner))
            if not repos:
                return f"«{owner}» no tiene repositorios públicos"
            active, since, stars = repo_activity(repos, now)
            facts.update(active_repos_30d=active, days_since_push=since, stars=stars)
            a.links["GitHub"] = f"https://github.com/{owner}"
            return None

        async def okx() -> str | None:
            bids, asks = await self.okx.order_book(inst_id)
            depth, spread = book_depth(bids, asks)
            facts.update(depth_2pct=depth, spread=spread)
            a.links["OKX"] = f"https://www.okx.com/es-es/trade-spot/{inst_id.lower()}"
            return None if depth is not None else "libro vacío"

        await step("DefiLlama", defillama)
        await asyncio.gather(step("GitHub", github), step("OKX", okx))
        a.inputs = Inputs(symbol, **facts)
        return a

    async def _defillama(
        self,
        coin_id: str,
        now: datetime,
        facts: dict[str, Any],
        links: dict[str, str],
        github_owners: list[str],
    ) -> bool:
        protocols, chains = await asyncio.gather(
            self.cache.fetch("llama:protocols-map", DAY, self._protocol_map),
            self.cache.fetch("llama:chains-map", DAY, self._chain_map),
        )
        month_ago = now.timestamp() - 30 * DAY
        # A chain's own token first: its bridges often carry the same CoinGecko id.
        chain = chains.get(coin_id)
        slug = None if chain else protocols.get(coin_id)
        detail = None
        if not chain:
            # Protocols whose versions carry no CoinGecko id are listed under the parent only.
            detail = await self.llama.protocol(slug or coin_id)
            if detail is not None and not slug and detail.get("gecko_id") != coin_id:
                detail = None
        match: dict[str, str]
        if detail is not None:
            slug = slug or coin_id
            series = detail.get("tvl") or []
            tvl_now = _tvl_at(series, now.timestamp(), "totalLiquidityUSD")
            tvl_then = _tvl_at(series, month_ago, "totalLiquidityUSD")
            github_owners[:0] = [g for g in detail.get("github") or [] if g]
            links["DefiLlama"] = f"https://defillama.com/protocol/{slug}"
            match = {"slug": slug, "parentProtocol": f"parent#{slug}"}
        elif chain:
            series = await self.llama.chain_tvl(chain)
            tvl_now = _tvl_at(series, now.timestamp(), "tvl")
            tvl_then = _tvl_at(series, month_ago, "tvl")
            links["DefiLlama"] = f"https://defillama.com/chain/{chain}"
            match = {"name": chain, "protocolType": "chain"}
        else:
            return False
        facts["tvl"] = tvl_now
        if tvl_now and tvl_then:
            facts["tvl_change_30d"] = tvl_now / tvl_then - 1
        fees, revenue = await asyncio.gather(
            self.cache.fetch("llama:fees", self.ttl, lambda: self._fees("dailyFees")),
            self.cache.fetch("llama:revenue", self.ttl, lambda: self._fees("dailyRevenue")),
        )
        mine = _entries(fees, match)
        facts["fees_30d"] = _sum(mine, "total30d")
        facts["fees_prev_30d"] = _sum(mine, "total60dto30d")
        facts["revenue_30d"] = _sum(_entries(revenue, match), "total30d")
        return True

    async def _protocol_map(self) -> dict[str, str]:
        """CoinGecko id → DefiLlama slug; the parent's when the protocol has versions."""
        mapping: dict[str, str] = {}
        for p in await self.llama.protocols():
            if gecko := p.get("gecko_id"):
                parent = str(p.get("parentProtocol") or "").removeprefix("parent#")
                mapping.setdefault(gecko, parent or p["slug"])
        return mapping

    async def _chain_map(self) -> dict[str, str]:
        return {c["gecko_id"]: c["name"] for c in await self.llama.chains() if c.get("gecko_id")}

    async def _fees(self, data_type: str) -> list[dict[str, Any]]:
        keep = ("slug", "name", "parentProtocol", "protocolType", "total30d", "total60dto30d")
        return [{k: e.get(k) for k in keep} for e in await self.llama.fees_overview(data_type)]


def _entries(rows: list[dict[str, Any]], match: dict[str, str]) -> list[dict[str, Any]]:
    """Fee rows of a protocol (itself or its versions) or of a chain."""
    if "slug" in match:
        return [
            r
            for r in rows
            if r.get("slug") == match["slug"] or r.get("parentProtocol") == match["parentProtocol"]
        ]
    return [
        r
        for r in rows
        if r.get("protocolType") == "chain"
        and str(r.get("name", "")).lower() == match["name"].lower()
    ]


def _tvl_at(series: list[dict[str, Any]], ts: float, key: str) -> float | None:
    """The last value at or before ``ts``."""
    best = None
    for point in series:
        if float(point.get("date", 0)) <= ts:
            best = point.get(key)
    return float(best) if best is not None else None
