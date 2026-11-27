"""Fundamentals app: scores, the analysis of each source, the 4h list and the pages."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from ha_trading_helper.fundamentals import fmt, sentiment
from ha_trading_helper.fundamentals.analysis import (
    Analysis,
    Analyzer,
    book_depth,
    github_owner,
    plain_text,
    repo_activity,
    supply_growth,
)
from ha_trading_helper.fundamentals.cache import Cache
from ha_trading_helper.fundamentals.config import FundamentalsSettings
from ha_trading_helper.fundamentals.market import Pair, load_pairs, window
from ha_trading_helper.fundamentals.scoring import Inputs, ramp, score
from ha_trading_helper.fundamentals.service import BulkRun, FundamentalsService
from ha_trading_helper.fundamentals.sources import (
    COINGECKO_URL,
    DEFILLAMA_URL,
    GITHUB_URL,
    CoinGecko,
    DefiLlama,
    GitHub,
    SourceError,
)
from ha_trading_helper.fundamentals.web import create_app, sort_pairs
from ha_trading_helper.okx.models import Candle, Instrument, Ticker

NOW = datetime(2026, 11, 27, 12, tzinfo=UTC)
DAY_MS = 86_400_000


async def no_sleep(_: float) -> None:
    return None


# A healthy DeFi protocol and a token with nothing behind it, as hand-made inputs.
STRONG = Inputs(
    "UNI",
    market_cap=3e9,
    fdv=3.3e9,
    volume_24h=3e8,
    circulating=900e6,
    total_supply=1e9,
    max_supply=1e9,
    supply_growth_1y=0.01,
    rank=20,
    ath_change=-0.3,
    age_years=6,
    watchlist_users=500_000,
    tvl=5e9,
    tvl_change_30d=0.1,
    fees_30d=30e6,
    fees_prev_30d=25e6,
    revenue_30d=15e6,
    active_repos_30d=20,
    days_since_push=1,
    stars=40_000,
    depth_2pct=2e6,
    spread=0.0003,
)
WEAK = Inputs(
    "JUNK",
    market_cap=5e6,
    fdv=50e6,
    volume_24h=20_000,
    circulating=100e6,
    total_supply=1e9,
    supply_growth_1y=0.6,
    rank=1500,
    ath_change=-0.97,
    age_years=0.3,
    watchlist_users=800,
    fees_30d=0.0,
    revenue_30d=0.0,
    active_repos_30d=0,
    stars=0,
    depth_2pct=3000,
    spread=0.02,
)


# --- scoring ---------------------------------------------------------------


def test_ramp_is_flat_outside_and_reversible() -> None:
    assert ramp(None, 0, 1) is None
    assert ramp(float("nan"), 0, 1) is None
    assert ramp(-1, 0, 10) == 0
    assert ramp(5, 0, 10) == 50
    assert ramp(20, 0, 10) == 100
    assert ramp(0.0, 0.25, 0.0) == 100  # lower is better
    assert ramp(0.3, 0.25, 0.0) == 0


def test_strong_coin_is_bullish() -> None:
    s = score(STRONG)
    assert s.overall is not None and s.overall >= 80
    assert s.outlook == "alcista"
    assert s.dilution.level == "bajo"
    assert s.liquidity.level == "bajo"
    assert s.coverage == 1.0
    assert all(g.score is not None and g.score > 60 for g in s.categories)
    assert any("Lo más sólido" in r for r in s.reasons)


def test_weak_coin_is_bearish_with_both_risks_high() -> None:
    s = score(WEAK)
    assert s.dilution.level == "alto"
    assert s.liquidity.level == "alto"
    assert s.outlook == "bajista"
    assert s.overall is not None and s.overall < 25
    assert sum("resta 10 puntos" in r for r in s.reasons) == 2


def test_no_data_is_neutral_and_says_why() -> None:
    s = score(Inputs("X"))
    assert s.overall is None
    assert s.outlook == "neutral"
    assert s.dilution.level is None
    assert "no basta para opinar" in s.reasons[0]
    assert all(m.value == "sin datos" for g in s.categories for m in g.metrics)


def test_missing_metrics_do_not_count_against_the_score() -> None:
    only_dev = Inputs("X", active_repos_30d=15, days_since_push=0, stars=100_000)
    assert score(only_dev).categories[3].score == 100
    assert score(only_dev).categories[0].score is None


def test_falling_usage_blocks_a_bullish_outlook() -> None:
    falling = Inputs(**{**STRONG.__dict__, "fees_30d": 10e6, "fees_prev_30d": 30e6})
    s = score(falling)
    assert s.overall is not None and s.overall >= 65
    assert s.outlook == "neutral"
    assert any("El uso cae" in r for r in s.reasons)


def test_tvl_trend_is_used_without_fees() -> None:
    no_fees = Inputs(
        **{**STRONG.__dict__, "fees_30d": None, "fees_prev_30d": None, "tvl_change_30d": -0.5}
    )
    assert any("-50 %" in r for r in score(no_fees).reasons)


def test_texts_of_supply_and_age() -> None:
    s = score(Inputs("X", circulating=5, total_supply=10, age_years=1, age_is_minimum=True))
    tokenomics, adoption = s.categories[0], s.categories[1]
    assert tokenomics.metrics[0].value == "50 % del total"
    assert tokenomics.metrics[2].value == "sin límite"
    assert adoption.metrics[-1].value == "≥ 1,0 años"
    capped = score(Inputs("X", circulating=5, max_supply=10)).categories[0]
    assert capped.metrics[0].value == "50 % del máximo"
    assert capped.metrics[2].value == "fijo, 10"


# --- formatting --------------------------------------------------------------


def test_spanish_numbers() -> None:
    assert fmt.num(1234.5, 1) == "1.234,5"
    assert fmt.big(None) == "—"
    assert fmt.big(3.14e9, "$") == "3,1 mil M $"
    assert fmt.big(2.5e6) == "2,5 M"
    assert fmt.big(25_000) == "25 mil"
    assert fmt.big(250) == "250"
    assert fmt.pct(None) == "—"
    assert fmt.pct(0.0123, sign=True) == "+1,2 %"
    assert fmt.pct(-0.5, 0, sign=True) == "-50 %"
    assert fmt.price(None) == "—"
    assert fmt.price(0) == "0"
    assert fmt.price(61644.9) == "61.644,9"
    assert fmt.price(0.0004812) == "0,0004812"


# --- analysis helpers --------------------------------------------------------


def chart(days: int, start_supply: float, end_supply: float) -> dict[str, Any]:
    t0 = int(NOW.timestamp() * 1000) - days * DAY_MS
    return {
        "prices": [[t0, 2.0], [t0 + days * DAY_MS, 4.0]],
        "market_caps": [[t0, 2.0 * start_supply], [t0 + days * DAY_MS, 4.0 * end_supply]],
    }


def test_supply_growth_from_market_cap_over_price() -> None:
    growth, days = supply_growth(chart(365, 100, 110))
    assert days == pytest.approx(365)
    assert growth == pytest.approx(0.10)
    half, _ = supply_growth(chart(182, 100, 110))
    assert half == pytest.approx(1.1 ** (365 / 182) - 1)
    assert supply_growth(chart(30, 100, 110))[0] is None
    assert supply_growth({}) == (None, 0.0)


def test_book_depth_within_two_percent() -> None:
    bids = [(99.0, 1.0), (97.0, 10.0)]  # 97 is beyond 2 % of the 100 mid
    asks = [(101.0, 2.0), (101.9, 1.0), (103.0, 50.0)]
    depth, spread = book_depth(bids, asks)
    assert depth == pytest.approx(99 + 202 + 101.9)
    assert spread == pytest.approx(0.02)
    assert book_depth([], asks) == (None, None)


def test_repo_activity_ignores_forks_and_archived() -> None:
    def repo(days: int, stars: int, **extra: Any) -> dict[str, Any]:
        pushed = (NOW - timedelta(days=days)).isoformat().replace("+00:00", "Z")
        return {"pushed_at": pushed, "stargazers_count": stars, **extra}

    repos = [
        repo(2, 100),
        repo(40, 50),
        repo(1, 9999, fork=True),
        repo(1, 9999, archived=True),
        {"pushed_at": None},
    ]
    assert repo_activity(repos, NOW) == (1, pytest.approx(2), 150)
    assert repo_activity([], NOW) == (0, None, 0)


def test_github_owner_and_plain_text() -> None:
    assert github_owner("https://github.com/Uniswap/v4-core") == "Uniswap"
    assert github_owner("https://gitlab.com/x/y") is None
    assert github_owner("https://github.com/") is None
    assert plain_text("<p>Hola  <b>mundo</b></p>") == "Hola mundo"
    long = "Una frase. " * 100
    cut = plain_text(long, 50)
    assert len(cut) <= 51 and cut.endswith(".")


# --- the analyzer with fake sources ------------------------------------------


class Closeable:
    async def aclose(self) -> None:
        return None


def global_payload(fng: int = 22, cap: float = 2.1e12) -> dict[str, Any]:
    return {
        "data": {
            "active_cryptocurrencies": 21218,
            "markets": 1525,
            "total_market_cap": {"usd": cap, "btc": 1.0},
            "total_volume": {"usd": 9.1e10},
            "market_cap_percentage": {"eth": 9.8, "btc": 57.8, "usdt": 8.3},
            "market_cap_change_percentage_24h_usd": -3.2,
            "volume_change_percentage_24h_usd": -0.5,
            "updated_at": 1795796057,
            "fear_and_greed_index": {"value": fng, "sentiment": sentiment.level(fng)},
        }
    }


class FakeCoinGecko(Closeable):
    def __init__(self) -> None:
        self.calls = 0
        self.fail = False
        self.global_calls = 0
        self.fng = 22

    async def global_data(self) -> dict[str, Any]:
        self.global_calls += 1
        if self.fail:
            raise SourceError("CoinGecko: caído")
        return global_payload(self.fng)

    async def markets(self, page: int) -> list[dict[str, Any]]:
        if page > 1:
            return []
        return [
            {"id": "uniswap", "symbol": "uni"},
            {"id": "solana", "symbol": "sol"},
            {"id": "pepe", "symbol": "pepe"},
            {"id": "uni-fake", "symbol": "uni"},  # smaller coin, same symbol
        ]

    async def coin(self, coin_id: str) -> dict[str, Any] | None:
        self.calls += 1
        if self.fail:
            raise SourceError("CoinGecko: caído")
        if coin_id == "missing":
            return None
        return {
            "name": coin_id.title(),
            "genesis_date": "2020-09-17" if coin_id == "uniswap" else None,
            "market_cap_rank": 20,
            "watchlist_portfolio_users": 300_000,
            "image": {"small": "https://img/x.png"},
            "description": {"en": "<p>Un protocolo.</p>"},
            "categories": ["DeFi", None],
            "links": {
                "homepage": ["", "https://uniswap.org"],
                "repos_url": {"github": ["https://github.com/Uniswap/v4-core"]}
                if coin_id == "uniswap"
                else {},
            },
            "market_data": {
                "market_cap": {"usd": 3e9},
                "fully_diluted_valuation": {"usd": 4e9},
                "total_volume": {"usd": 3e8},
                "circulating_supply": 600e6,
                "total_supply": 900e6,
                "max_supply": 1e9,
                "ath_change_percentage": {"usd": -89.0},
            },
        }

    async def market_chart(self, coin_id: str, days: int = 365) -> dict[str, Any] | None:
        return chart(365, 100, 102)


class FakeDefiLlama(Closeable):
    async def protocols(self) -> list[dict[str, Any]]:
        return [
            {"slug": "uniswap-v3", "gecko_id": "uniswap", "parentProtocol": "parent#uniswap"},
            {"slug": "solana-bridge", "gecko_id": "solana"},  # a bridge with the chain's id
            {"slug": "nothing"},
        ]

    async def chains(self) -> list[dict[str, Any]]:
        return [{"gecko_id": "solana", "name": "Solana"}, {"name": "NoToken"}]

    async def protocol(self, slug: str) -> dict[str, Any] | None:
        if slug != "uniswap":
            return None
        now = NOW.timestamp()
        return {
            "gecko_id": "uniswap",
            "github": ["Uniswap"],
            "tvl": [
                {"date": now - 40 * 86400, "totalLiquidityUSD": 4e9},
                {"date": now - 30 * 86400, "totalLiquidityUSD": 4e9},
                {"date": now - 3600, "totalLiquidityUSD": 3e9},
            ],
        }

    async def chain_tvl(self, chain: str) -> list[dict[str, Any]]:
        now = NOW.timestamp()
        return [{"date": now - 31 * 86400, "tvl": 5e9}, {"date": now - 60, "tvl": 6e9}]

    async def fees_overview(self, data_type: str = "dailyFees") -> list[dict[str, Any]]:
        factor = 1.0 if data_type == "dailyFees" else 0.1
        return [
            {
                "slug": "uniswap-v3",
                "parentProtocol": "parent#uniswap",
                "total30d": 60e6 * factor,
                "total60dto30d": 80e6 * factor,
            },
            {
                "slug": "uniswap-v2",
                "parentProtocol": "parent#uniswap",
                "total30d": 10e6 * factor,
                "total60dto30d": None,
            },
            {
                "slug": "solana",
                "name": "Solana",
                "protocolType": "chain",
                "total30d": 13e6 * factor,
                "total60dto30d": 24e6 * factor,
            },
        ]


class FakeGitHub(Closeable):
    def __init__(self) -> None:
        self.fail = False

    async def repos(self, owner: str) -> list[dict[str, Any]]:
        if self.fail:
            raise SourceError("GitHub: límite de consultas agotado")
        pushed = (NOW - timedelta(days=3)).isoformat()
        return [{"pushed_at": pushed, "stargazers_count": 5000}] if owner == "Uniswap" else []


class FakeOkx(Closeable):
    async def order_book(
        self, inst_id: str, depth: int = 400
    ) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
        return [(99.0, 1000.0)], [(101.0, 1000.0)]

    async def instruments(self) -> list[Instrument]:
        return [
            Instrument(inst_id="UNI-USDC", category="1", state="live", list_time=1_600_000_000_000),
            Instrument(inst_id="UNI-USDT", category="1", state="live", list_time=1_500_000_000_000),
            Instrument(inst_id="SOL-USDC", category="1", state="live"),
            Instrument(inst_id="USDT-USDC", category="1", state="live"),
            Instrument(inst_id="XAAPL-USDC", category="3", state="live"),
            Instrument(inst_id="BAD-USDC", category="1", state="live"),
        ]

    async def tickers(self) -> list[Ticker]:
        return [
            Ticker(
                inst_id=i,
                last=1.0,
                bid=0.99,
                ask=1.01,
                volume_24h_quote=v,
                open_24h=0.8 if i == "UNI-USDC" else None,
            )
            for i, v in [
                ("UNI-USDC", 1e6),
                ("SOL-USDC", 5e6),
                ("XAAPL-USDC", 9e9),
                ("BAD-USDC", 1.0),
                ("USDT-USDC", 9e9),
            ]
        ]

    async def recent_candles(self, inst_id: str, bar: str, limit: int) -> list[Candle]:
        if inst_id == "BAD-USDC":
            raise RuntimeError("boom")
        t0 = NOW - timedelta(hours=4)
        mult = 2.0 if inst_id == "SOL-USDC" else 1.0
        close = 12.0 if inst_id == "SOL-USDC" else 11.0
        return [
            Candle(
                ts=t0,
                open=10,
                high=11,
                low=9,
                close=10,
                volume=1,
                volume_quote=100 * mult,
                confirmed=True,
            ),
            Candle(
                ts=NOW,
                open=10,
                high=12,
                low=9,
                close=close,
                volume=1,
                volume_quote=50 * mult,
                confirmed=False,
            ),
        ]


def analyzer(tmp_path: Path, **kwargs: Any) -> Analyzer:
    return Analyzer(
        coingecko=kwargs.pop("cg", FakeCoinGecko()),  # type: ignore[arg-type]
        defillama=FakeDefiLlama(),  # type: ignore[arg-type]
        github=kwargs.pop("gh", FakeGitHub()),  # type: ignore[arg-type]
        okx=FakeOkx(),  # type: ignore[arg-type]
        cache=Cache(tmp_path / "c.db", clock=NOW.timestamp),
        clock=NOW.timestamp,
        **kwargs,
    )


async def test_protocol_gets_every_source(tmp_path: Path) -> None:
    a = await analyzer(tmp_path).analyze("uni", "UNI-USDC")
    assert a.coin_id == "uniswap"  # the largest coin with that symbol
    assert a.sources == {"CoinGecko": "ok", "DefiLlama": "ok", "GitHub": "ok", "OKX": "ok"}
    i = a.inputs
    assert i.market_cap == 3e9 and i.max_supply == 1e9 and i.ath_change == pytest.approx(-0.89)
    assert i.supply_growth_1y == pytest.approx(0.02)
    assert i.age_years == pytest.approx(6.2, abs=0.01) and not i.age_is_minimum
    assert i.tvl == 3e9 and i.tvl_change_30d == pytest.approx(-0.25)
    assert i.fees_30d == 70e6 and i.fees_prev_30d == 80e6
    assert i.revenue_30d == pytest.approx(7e6)
    assert (i.active_repos_30d, i.stars) == (1, 5000)
    assert i.depth_2pct == pytest.approx(200_000) and i.spread == pytest.approx(0.02)
    assert set(a.links) == {"CoinGecko", "Web", "DefiLlama", "GitHub", "OKX"}
    assert a.categories == ["DeFi"]
    assert a.description == "Un protocolo."
    assert a.scores.overall is not None


async def test_chain_wins_over_a_bridge_with_its_id(tmp_path: Path) -> None:
    listed = int((NOW - timedelta(days=3 * 365)).timestamp() * 1000)
    a = await analyzer(tmp_path).analyze("SOL", "SOL-USDC", listed=listed)
    assert a.links["DefiLlama"] == "https://defillama.com/chain/Solana"
    assert a.inputs.tvl == 6e9 and a.inputs.tvl_change_30d == pytest.approx(0.2)
    assert a.inputs.fees_30d == 13e6
    # No genesis date: the OKX listing gives a minimum age.
    assert a.inputs.age_years == pytest.approx(3.0) and a.inputs.age_is_minimum
    assert a.sources["GitHub"] == "sin repositorio público"
    assert a.inputs.active_repos_30d == 0


async def test_token_without_protocol_has_no_fees(tmp_path: Path) -> None:
    a = await analyzer(tmp_path).analyze("PEPE", "PEPE-USDC")
    assert a.sources["DefiLlama"] == "no es un protocolo ni una cadena: sin comisiones"
    assert a.inputs.fees_30d == 0 and a.inputs.tvl is None


async def test_failed_sources_leave_no_data_and_are_reported(tmp_path: Path) -> None:
    cg, gh = FakeCoinGecko(), FakeGitHub()
    cg.fail = gh.fail = True
    a = await analyzer(tmp_path, cg=cg, gh=gh).analyze("UNI", "UNI-USDC")
    assert a.sources["CoinGecko"] == "error: CoinGecko: caído"
    assert a.sources["DefiLlama"] == "ok"  # the id comes from the symbol map
    assert a.sources["GitHub"].startswith("error: GitHub")
    assert a.inputs.market_cap is None and a.inputs.active_repos_30d is None


async def test_unknown_symbol_and_overrides(tmp_path: Path) -> None:
    an = analyzer(tmp_path, coin_ids={"zzz": "missing"})
    a = await an.analyze("QQQ", "QQQ-USDC")
    assert a.sources["CoinGecko"] == "no la encuentra entre las 1.250 mayores"
    assert a.sources["DefiLlama"] == "sin moneda de CoinGecko con la que buscarla"
    assert a.sources["GitHub"] == "sin repositorio conocido"
    b = await an.analyze("ZZZ", "ZZZ-USDC")
    assert b.sources["CoinGecko"] == "no existe «missing»"


async def test_analysis_is_cached_and_round_trips(tmp_path: Path) -> None:
    cg = FakeCoinGecko()
    an = analyzer(tmp_path, cg=cg)
    first = await an.analyze("UNI", "UNI-USDC")
    again = await an.analyze("UNI", "UNI-USDC")
    assert cg.calls == 1 and again.inputs == first.inputs
    await an.analyze("UNI", "UNI-USDC", refresh=True)
    assert cg.calls == 2
    assert Analysis.from_json(first.to_json()) == first


# --- cache, market list and HTTP sources -------------------------------------


async def test_cache_expires(tmp_path: Path) -> None:
    now = [1000.0]
    cache = Cache(tmp_path / "sub" / "c.db", clock=lambda: now[0])
    cache.put("k", {"a": 1})
    assert cache.get("k", 10) == {"a": 1}
    assert cache.age("k") == 0 and cache.age("nope") is None
    now[0] += 11
    assert cache.get("k", 10) is None and cache.get("k") == {"a": 1}

    async def load() -> int:
        return 7

    assert await cache.fetch("n", 5, load) == 7
    cache.close()


async def test_load_pairs_filters_and_measures_4h() -> None:
    pairs = await load_pairs(FakeOkx())  # type: ignore[arg-type]
    # By 4h change, highest first; pairs whose candles failed go last.
    assert [p.inst_id for p in pairs] == ["SOL-USDC", "UNI-USDC", "BAD-USDC"]
    sol, uni = pairs[0], pairs[1]
    assert sol.change_4h == pytest.approx(0.2) and sol.volume_4h == 300
    assert uni.change_4h == pytest.approx(0.1)
    assert uni.change_24h == pytest.approx(0.25) and sol.change_24h is None
    assert uni.listed == 1_500_000_000_000  # the oldest listing of the coin
    assert pairs[2].change_4h is None
    assert window([]) == (None, None)


@respx.mock
async def test_json_api_missing_retries_and_rate_limits() -> None:
    respx.get(f"{DEFILLAMA_URL}/protocol/nope").mock(return_value=httpx.Response(400))
    respx.get(f"{DEFILLAMA_URL}/v2/chains").mock(
        side_effect=[httpx.Response(502), httpx.Response(200, json=[{"name": "A"}])]
    )
    respx.get(f"{DEFILLAMA_URL}/protocols").mock(side_effect=httpx.ConnectError("x"))
    respx.get(f"{DEFILLAMA_URL}/overview/fees").mock(
        return_value=httpx.Response(200, json={"protocols": [{"slug": "a"}]})
    )
    async with DefiLlama(sleep=no_sleep, backoff=0) as llama:
        assert await llama.protocol("nope") is None
        assert await llama.chains() == [{"name": "A"}]
        assert await llama.fees_overview() == [{"slug": "a"}]
        with pytest.raises(SourceError, match="transport"):
            await llama.protocols()

    respx.get(f"{COINGECKO_URL}/coins/x").mock(return_value=httpx.Response(429))
    respx.get(f"{COINGECKO_URL}/coins/markets").mock(return_value=httpx.Response(500))
    async with CoinGecko("key", sleep=no_sleep, backoff=0, max_retries=1) as cg:
        with pytest.raises(SourceError, match="límite de consultas"):
            await cg.coin("x")
        with pytest.raises(SourceError, match="HTTP 500"):
            await cg.markets(1)
    assert respx.calls[-1].request.headers["x-cg-demo-api-key"] == "key"

    respx.get(f"{GITHUB_URL}/users/a/repos").mock(
        return_value=httpx.Response(403, text='{"message":"API rate limit exceeded"}')
    )
    respx.get(f"{GITHUB_URL}/users/b/repos").mock(return_value=httpx.Response(401, text="bad"))
    async with GitHub("tok", sleep=no_sleep) as gh:
        with pytest.raises(SourceError, match="GitHub: límite"):
            await gh.repos("a")
        with pytest.raises(SourceError, match="HTTP 401"):
            await gh.repos("b")
    assert respx.calls[-1].request.headers["Authorization"] == "Bearer tok"


@respx.mock
async def test_coingecko_endpoints_and_llama_series() -> None:
    respx.get(f"{COINGECKO_URL}/coins/markets").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{COINGECKO_URL}/coins/a").mock(return_value=httpx.Response(200, json={"id": "a"}))
    respx.get(f"{COINGECKO_URL}/coins/a/market_chart").mock(
        return_value=httpx.Response(200, json={"prices": []})
    )
    respx.get(f"{DEFILLAMA_URL}/v2/historicalChainTvl/A").mock(
        return_value=httpx.Response(200, json=[{"date": 1, "tvl": 2}])
    )
    async with CoinGecko(sleep=no_sleep) as cg, DefiLlama(sleep=no_sleep) as llama:
        assert await cg.markets(1) == []
        assert await cg.coin("a") == {"id": "a"}
        assert await cg.market_chart("a") == {"prices": []}
        respx.get(f"{COINGECKO_URL}/global").mock(
            return_value=httpx.Response(200, json=global_payload())
        )
        assert (await cg.global_data())["data"]["markets"] == 1525
        assert await llama.chain_tvl("A") == [{"date": 1, "tvl": 2}]


# --- settings, service and pages ----------------------------------------------


def test_coin_id_overrides() -> None:
    s = FundamentalsSettings(coin_ids=["ton=The-Open-Network", "bad", " =x", "S=sonic-3"])
    assert s.coin_id_overrides() == {"TON": "the-open-network", "S": "sonic-3"}


def service(tmp_path: Path) -> FundamentalsService:
    settings = FundamentalsSettings(data_dir=tmp_path)
    return FundamentalsService(
        settings,
        okx=FakeOkx(),  # type: ignore[arg-type]
        coingecko=FakeCoinGecko(),  # type: ignore[arg-type]
        defillama=FakeDefiLlama(),  # type: ignore[arg-type]
        github=FakeGitHub(),  # type: ignore[arg-type]
    )


def test_sort_pairs() -> None:
    pairs = [
        Pair("A-USDC", 1, 0.1, 10, 5, change_24h=None),
        Pair("B-USDC", 1, None, None, 50, change_24h=-0.1),
        Pair("C-USDC", 1, 0.3, 1, 1, change_24h=0.2),
    ]

    def order(*args: Any, **kwargs: Any) -> list[str]:
        return [p.base for p in sort_pairs(pairs, *args, **kwargs)]

    assert order("cambio", True) == ["C", "A", "B"]
    assert order("cambio", False) == ["B", "A", "C"]
    assert order("cambio24", True) == ["C", "B", "A"]
    assert order("volumen24", True) == ["B", "A", "C"]
    assert order("puntos", True, scores={"B": 70, "A": 40}) == ["B", "A", "C"]
    assert order("par", False, "b") == ["B"]
    assert order("nope", True) == order("cambio", True)


async def test_service_refresh_persists_and_reports_errors(tmp_path: Path) -> None:
    svc = service(tmp_path)
    await svc.refresh()
    assert svc.pair("sol") is not None and svc.error == "" and svc.updated
    assert await svc.analysis("NOPE") is None
    a = await svc.analysis("uni")
    assert a is not None and svc.cached("UNI") is not None

    class Broken(FakeOkx):
        async def tickers(self) -> list[Ticker]:
            raise RuntimeError("OKX caído")

    svc.okx = Broken()  # type: ignore[assignment]
    await svc.refresh()
    assert svc.error == "OKX caído" and svc.pairs
    await svc.aclose()

    reopened = service(tmp_path)  # the list survives a restart
    assert [p.inst_id for p in reopened.pairs][:2] == ["SOL-USDC", "UNI-USDC"]


async def test_pages(tmp_path: Path) -> None:
    svc = service(tmp_path)
    await svc.refresh()
    ingress = {"X-Ingress-Path": "/api/hassio_ingress/tok"}
    with TestClient(create_app(svc)) as client:
        default = client.get("/").text
        assert "Cambio 4 h ↓" in default and "Cambio 24 h" in default
        assert "Analizar todas" in default and "3 pares sin análisis reciente" in default
        page = client.get("/?orden=cambio&dir=asc&q=so", headers=ingress)
        assert page.status_code == 200
        assert 'href="/api/hassio_ingress/tok/moneda/SOL"' in page.text
        assert "UNI" not in page.text.split("<tbody>")[1].split("</tbody>")[0]
        assert "sin analizar" in page.text

        coin = client.get("/moneda/uni", headers=ingress)
        assert coin.status_code == 200
        for text in (
            "Uniswap",
            "Puntuación fundamental",
            "Riesgo de dilución",
            "Desarrollo",
            "datos leídos",
            "Cómo se calcula",
        ):
            assert text in coin.text
        assert "/100" in client.get("/").text  # the list shows the cached score

        assert "No hay un par" in client.get("/moneda/NOPE").text
        assert "No hay un par" in client.get("/moneda/a-b").text

        r = client.post("/moneda/UNI/actualizar", headers=ingress, follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/api/hassio_ingress/tok/moneda/UNI"
        r = client.post("/moneda/NOPE/actualizar", follow_redirects=False)
        assert r.headers["location"] == "/"
        r = client.post("/actualizar", follow_redirects=False)
        assert r.status_code == 303
        assert client.get("/salud").text == "ok"


def test_empty_list_page(tmp_path: Path) -> None:
    svc = service(tmp_path)
    svc.error = "sin red"
    with TestClient(create_app(svc)) as client:
        text = client.get("/").text
    assert "Leyendo los pares de OKX" in text and "sin red" in text


def test_cli_analyze(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    from ha_trading_helper import cli
    from ha_trading_helper.fundamentals import service as service_module

    monkeypatch.setattr(service_module, "FundamentalsService", lambda _s: service(tmp_path))
    runner = CliRunner()
    result = runner.invoke(cli.app, ["fundamentals", "analyze", "uni"])
    assert result.exit_code == 0, result.output
    assert "Uniswap (uniswap)" in result.output
    assert "Riesgo de liquidez" in result.output and "CoinGecko: ok" in result.output
    missing = runner.invoke(cli.app, ["fundamentals", "analyze", "nope"])
    assert missing.exit_code == 1


async def test_bulk_analyses_every_pending_pair(tmp_path: Path) -> None:
    svc = service(tmp_path)
    await svc.refresh()
    await svc.analysis("UNI")  # already fresh: reused, not read again
    assert [p.base for p in svc.pending()] == ["SOL", "BAD"]
    assert svc.start_bulk()
    assert not svc.start_bulk()  # one at a time
    assert svc._bulk_task is not None
    await svc._bulk_task
    run = svc.bulk
    assert run is not None and not run.running
    assert (run.total, run.done, run.reused) == (3, 3, 1)
    assert run.incomplete == 0 and run.current == ""
    assert svc.cached("SOL") is not None and svc.cached("BAD") is not None
    assert svc.pending() == []

    with TestClient(create_app(svc)) as client:
        text = client.get("/").text
    assert "Todas tienen un análisis reciente" in text and "Última pasada: 3 de 3" in text
    await svc.aclose()


async def test_bulk_can_be_stopped_and_counts_failures(tmp_path: Path) -> None:
    svc = service(tmp_path)
    await svc.refresh()
    seen: list[str] = []

    async def analysis(symbol: str, *, refresh: bool = False) -> Analysis | None:
        seen.append(symbol)
        assert svc.bulk is not None
        svc.cancel_bulk()
        raise RuntimeError("boom")

    svc.analysis = analysis  # type: ignore[method-assign]
    svc.start_bulk()
    assert svc._bulk_task is not None
    await svc._bulk_task
    assert seen == ["SOL"]  # stopped after the first one
    assert svc.bulk is not None and svc.bulk.cancelled and svc.bulk.incomplete == 1
    svc.cancel_bulk()  # nothing running: no effect
    await svc.aclose()


async def test_bulk_pages_and_coingecko_usage(tmp_path: Path) -> None:
    svc = service(tmp_path)
    await svc.refresh()
    svc._count_coingecko()
    svc._count_coingecko()
    assert svc.coingecko_calls == 2
    with TestClient(create_app(svc)) as client:
        text = client.get("/").text
        assert "este mes van 2 de 10.000" in text
        assert "Sin clave de CoinGecko" in text and "Sin token de GitHub" in text
        r = client.post("/analizar-todo", follow_redirects=False)
        assert r.status_code == 303 and svc.bulk is not None
        svc.bulk = BulkRun(total=3, started=NOW.timestamp(), done=1, current="SOL")
        running = client.get("/").text
        assert "1 de 3 · ahora SOL" in running
        assert 'http-equiv="refresh"' in running and "Detener" in running
        r = client.post("/analizar-todo/detener", follow_redirects=False)
        assert r.status_code == 303 and svc.bulk.cancelled
    fresh = service(tmp_path / "other")  # nothing analysed yet
    await fresh.refresh()
    fresh.cache.put(fresh._usage_key(), 9_999)
    with TestClient(create_app(fresh)) as client:
        assert "superaría las 10.000 consultas" in client.get("/").text


def test_cache_bump(tmp_path: Path) -> None:
    cache = Cache(tmp_path / "c.db")
    assert cache.bump("n") == 1 and cache.bump("n") == 2 and cache.get("n") == 2


@respx.mock
async def test_on_call_counts_every_request() -> None:
    respx.get(f"{COINGECKO_URL}/coins/markets").mock(
        side_effect=[httpx.Response(429), httpx.Response(200, json=[])]
    )
    calls: list[int] = []
    async with CoinGecko(sleep=no_sleep, backoff=0, on_call=lambda: calls.append(1)) as cg:
        assert await cg.markets(1) == []
    assert len(calls) == 2  # the retry counts too


def test_ticker_reads_open_24h() -> None:
    t = Ticker.from_api({"instId": "A-USDC", "last": "2", "open24h": "1.6", "volCcy24h": "5"})
    assert t.open_24h == 1.6


# --- the whole market and the Fear & Greed Index ---------------------------------


def test_fear_and_greed_levels() -> None:
    assert [sentiment.level(v) for v in (0, 30, 31, 45, 46, 55, 56, 70, 71, 100)] == [
        "extreme_fear",
        "extreme_fear",
        "fear",
        "fear",
        "neutral",
        "neutral",
        "greed",
        "greed",
        "extreme_greed",
        "extreme_greed",
    ]
    assert sentiment.label("extreme_fear") == "Miedo extremo"
    assert sentiment.label("otro") == "otro"
    assert [sentiment.mood(s) for s in ("fear", "neutral", "extreme_greed")] == [
        "bajista",
        "neutral",
        "alcista",
    ]


def test_global_parse_and_round_trip() -> None:
    g = sentiment.Global.parse(global_payload())
    assert g.fng == 22 and g.sentiment == "extreme_fear"
    assert list(g.dominance) == ["btc", "eth", "usdt"]
    assert g.dominance["btc"] == pytest.approx(0.578)
    assert g.market_cap_change_24h == pytest.approx(-0.032)
    assert sentiment.Global.from_json(g.to_json()) == g
    bare = sentiment.Global.parse({"data": {"fear_and_greed_index": {"value": 60}}})
    assert bare.sentiment == "greed" and bare.market_cap_change_24h is None
    none = sentiment.Global.parse({})
    assert none.fng is None and none.sentiment == ""
    assert fmt.big(2.126e12, "$") == "2,13 B $"


def history(values: dict[int, int], today: datetime = NOW) -> dict[str, Any]:
    """Readings by days before ``today``."""
    out: dict[str, Any] = {}
    for ago, v in values.items():
        g = sentiment.Global.parse(global_payload(v))
        out = sentiment.record(out, (today - timedelta(days=ago)).date(), g)
    return out


def test_record_keeps_one_reading_a_day_and_the_latest_days() -> None:
    day = NOW.date()
    h = sentiment.record({}, day, sentiment.Global.parse(global_payload(20)))
    h = sentiment.record(h, day, sentiment.Global.parse(global_payload(25)))
    assert h == {day.isoformat(): {"v": 25, "s": "extreme_fear", "mc": 2.1e12, "btc": 0.578}}
    assert sentiment.record(h, day, sentiment.Global.parse({})) == h
    long = history({i: 50 for i in range(sentiment.HISTORY_DAYS + 5)})
    assert len(long) == sentiment.HISTORY_DAYS
    assert max(long) == day.isoformat()


def test_summary_of_the_history() -> None:
    h = history({0: 22, 1: 20, 2: 25, 3: 40, 7: 59, 30: 48, 40: 93, 41: 6})
    s = sentiment.summarize(h, NOW.date())
    assert s.days == 8 and s.since == (NOW - timedelta(days=41)).date()
    assert s.streak == 3  # today, yesterday and the day before in extreme fear
    assert s.yesterday and s.yesterday.value == 20 and s.yesterday.label == "Miedo extremo"
    assert s.last_week and s.last_week.value == 59
    assert s.last_month and s.last_month.sentiment == "neutral"
    assert s.last_greed and s.last_greed.value == 59
    assert s.last_fear and s.last_fear.value == 20  # before today
    assert s.high and s.high.value == 93
    assert s.low and s.low.value == 6
    empty = sentiment.summarize({}, NOW.date())
    assert empty.days == 0 and empty.yesterday is None


def test_gauge_and_chart_svg() -> None:
    svg = sentiment.gauge(22, "extreme_fear")
    assert svg.count('class="band') == 5 and 'class="needle"' in svg
    assert 'class="dot extreme_fear"' in svg
    assert "needle" not in sentiment.gauge(None, "")
    assert sentiment.history_chart([]) == ""
    assert sentiment.history_chart(sentiment.readings(history({0: 50}))) == ""
    two = sentiment.history_chart(sentiment.readings(history({0: 50, 3: 80})))
    assert "<polyline" in two and 'class="dot neutral"' in two and 'r="4"' in two
    many = sentiment.history_chart(sentiment.readings(history({i: i for i in range(80)})))
    assert "<polyline" in many and 'r="3"' in many


async def test_service_reads_global_rarely_and_records_the_day(tmp_path: Path) -> None:
    svc = service(tmp_path)
    cg: FakeCoinGecko = svc.sources[0]  # type: ignore[assignment]
    assert svc.market is None and svc.history == {}
    g = await svc.refresh_global()
    assert g and g.fng == 22 and cg.global_calls == 1
    await svc.refresh_global()  # still fresh
    assert cg.global_calls == 1
    cg.fng = 60
    g = await svc.refresh_global(max_age=0)
    assert g and g.fng == 60 and cg.global_calls == 2
    assert next(iter(svc.history.values()))["v"] == 60  # one reading for today
    cg.fail = True
    g = await svc.refresh_global(max_age=0)
    assert g and g.fng == 60 and "caído" in svc.global_error
    await svc.aclose()


async def test_market_page(tmp_path: Path) -> None:
    svc = service(tmp_path)
    cg: FakeCoinGecko = svc.sources[0]  # type: ignore[assignment]
    ingress = {"X-Ingress-Path": "/api/hassio_ingress/tok"}
    cg.fail = True
    with TestClient(create_app(svc)) as client:
        page = client.get("/mercado").text
        assert "Aún no hay datos del mercado" in page and "falló" in page
        cg.fail = False
        page = client.get("/mercado", headers=ingress).text
        for text in (
            'href="/api/hassio_ingress/tok/mercado" class="on"',
            "actualmente <b>bajista</b>",
            "Miedo extremo",
            "Datos históricos",
            "1 día registrado",
            "El gráfico aparece con dos días",
            "Capitalización total",
            "2,10 B $",
            "Dominancia de BTC",
            "21.218",
        ):
            assert text in page, text
        svc.cache.put("global:history", history({0: 22, 1: 21, 2: 20}))
        assert "lleva 3 días seguidos en miedo extremo" in client.get("/mercado").text
        assert 'href="/" class="on"' in client.get("/").text
        r = client.post("/mercado/actualizar", headers=ingress, follow_redirects=False)
        assert r.headers["location"] == "/api/hassio_ingress/tok/mercado"
