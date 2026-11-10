from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from ha_trading_helper.data.universe import UniverseRules, select_universe
from ha_trading_helper.okx.client import BASE_URL, OkxClient, OkxError
from ha_trading_helper.okx.models import Candle, Instrument, Ticker
from ha_trading_helper.okx.rate_limit import RateLimiter

H4 = 4 * 3600 * 1000


async def no_sleep(_: float) -> None:
    return None


def row(ts: int, close: float = 1.0, confirm: str = "1") -> list[str]:
    c = str(close)
    return [str(ts), c, c, c, c, "10", "10", "10", confirm]


def ok(data: list[object]) -> httpx.Response:
    return httpx.Response(200, json={"code": "0", "msg": "", "data": data})


def client() -> OkxClient:
    return OkxClient(sleep=no_sleep, backoff=0.0)


@respx.mock
async def test_recorded_candles_parse(fixture_json: Callable[[str], object]) -> None:
    respx.get(f"{BASE_URL}/api/v5/market/candles").mock(
        return_value=httpx.Response(200, json=fixture_json("okx_candles_eth_4h.json"))
    )
    async with client() as c:
        candles = await c.recent_candles("ETH-USDT", "4H", 5)
    assert len(candles) == 5
    assert candles == sorted(candles, key=lambda x: x.ts)
    assert not candles[-1].confirmed  # the in-progress candle
    assert all(x.confirmed for x in candles[:-1])
    assert candles[0].ts.tzinfo is UTC


@respx.mock
async def test_pagination_goes_back_with_after() -> None:
    newest = 1_000 * H4
    recent = [row(newest - i * H4) for i in range(3)]
    older = [row(newest - i * H4) for i in range(3, 6)]
    oldest = [row(newest - i * H4) for i in range(6, 8)]
    respx.get(f"{BASE_URL}/api/v5/market/candles").mock(return_value=ok(recent))
    hist = respx.get(f"{BASE_URL}/api/v5/market/history-candles").mock(
        side_effect=[ok(older), ok(oldest), ok([])]
    )
    async with client() as c:
        candles = await c.candles("BTC-USDT", count=10)
    assert len(candles) == 8
    assert hist.calls[0].request.url.params["after"] == str(newest - 2 * H4)
    assert hist.calls[1].request.url.params["after"] == str(newest - 5 * H4)


@respx.mock
async def test_incremental_stops_at_since() -> None:
    newest = 1_000 * H4
    respx.get(f"{BASE_URL}/api/v5/market/candles").mock(
        return_value=ok([row(newest - i * H4) for i in range(3)])
    )
    hist = respx.get(f"{BASE_URL}/api/v5/market/history-candles")
    since = datetime.fromtimestamp((newest - H4) / 1000, tz=UTC)
    async with client() as c:
        candles = await c.candles("BTC-USDT", count=300, since=since)
    assert [x.ts for x in candles] == [since, since + timedelta(hours=4)]
    assert not hist.called


@respx.mock
async def test_retries_on_429_and_5xx_then_succeeds() -> None:
    route = respx.get(f"{BASE_URL}/api/v5/market/candles").mock(
        side_effect=[httpx.Response(429), httpx.Response(503), ok([row(H4)])]
    )
    async with client() as c:
        candles = await c.recent_candles("BTC-USDT")
    assert len(candles) == 1
    assert route.call_count == 3


@respx.mock
async def test_rate_limit_code_and_transport_errors_are_retried() -> None:
    respx.get(f"{BASE_URL}/api/v5/market/candles").mock(
        side_effect=[
            httpx.Response(200, json={"code": "50011", "msg": "Too Many Requests", "data": []}),
            httpx.ConnectError("boom"),
            ok([row(H4)]),
        ]
    )
    async with client() as c:
        assert len(await c.recent_candles("BTC-USDT")) == 1


@respx.mock
async def test_gives_up_after_retries() -> None:
    respx.get(f"{BASE_URL}/api/v5/market/candles").mock(return_value=httpx.Response(500))
    c = OkxClient(sleep=no_sleep, backoff=0.0, max_retries=2)
    with pytest.raises(OkxError, match="3 attempts"):
        await c.recent_candles("BTC-USDT")
    await c.aclose()


@respx.mock
async def test_api_error_code_is_not_retried() -> None:
    route = respx.get(f"{BASE_URL}/api/v5/market/candles").mock(
        return_value=httpx.Response(
            200, json={"code": "51001", "msg": "Instrument ID does not exist"}
        )
    )
    async with client() as c:
        with pytest.raises(OkxError, match="51001"):
            await c.recent_candles("NOPE-USDT")
    assert route.call_count == 1


@respx.mock
async def test_http_4xx_is_not_retried() -> None:
    respx.get(f"{BASE_URL}/api/v5/market/candles").mock(return_value=httpx.Response(404))
    async with client() as c:
        with pytest.raises(OkxError, match="404"):
            await c.recent_candles("BTC-USDT")


@respx.mock
async def test_tickers_instruments_and_universe(fixture_json: Callable[[str], object]) -> None:
    respx.get(f"{BASE_URL}/api/v5/market/tickers").mock(
        return_value=httpx.Response(200, json=fixture_json("okx_tickers_spot.json"))
    )
    respx.get(f"{BASE_URL}/api/v5/public/instruments").mock(
        return_value=ok([
            {"instId": "BTC-USDT", "instCategory": "1", "state": "live"},
            {"instId": "ETH-USDT", "instCategory": "1", "state": "live"},
            {"instId": "SOL-USDT", "instCategory": "1", "state": "suspend"},
            {"instId": "XNVDA-USDT", "instCategory": "3", "state": "live"},
        ])
    )  # fmt: skip
    async with client() as c:
        tickers = await c.tickers()
        instruments = await c.instruments()
    allowed = {i.inst_id for i in instruments if i.is_crypto_live}
    pairs = select_universe(tickers, UniverseRules(min_volume_24h_usdt=1000), allowed=allowed)
    assert pairs[:2] == ["BTC-USDT", "ETH-USDT"]
    assert "USDC-USDT" not in pairs and "XNVDA-USDT" not in pairs and "SOL-USDT" not in pairs


def test_universe_rules() -> None:
    def t(inst: str, vol: float, bid: float | None = 99.9, ask: float | None = 100.0) -> Ticker:
        return Ticker(inst_id=inst, last=100, bid=bid, ask=ask, volume_24h_quote=vol)

    tickers = [
        t("AAA-USDT", 9e6), t("BBB-USDT", 2e7), t("CCC-USDT", 1e3),
        t("DDD-USDT", 9e7, bid=90, ask=100), t("EEE-USDT", 9e7, bid=None),
        t("FFF-EUR", 9e6), t("USDC-USDT", 9e9),
    ]  # fmt: skip
    rules = UniverseRules(quotes=("USDT", "EUR"), min_volume_24h_usdt=5e6, max_pairs=10)
    assert select_universe(tickers, rules, eur_usdt=1.1) == ["BBB-USDT", "FFF-EUR", "AAA-USDT"]
    assert select_universe(tickers, UniverseRules(max_pairs=1)) == ["BBB-USDT"]
    assert t("X-USDT", 1, bid=1, ask=1).quote == "USDT"
    assert Ticker(inst_id="Z-USDT", last=0, bid=1, ask=1, volume_24h_quote=0).spread_pct is None


def test_models_from_rows() -> None:
    c = Candle.from_row(["0", "1", "2", "0.5", "1.5", "10", "15"])
    assert c.confirmed and c.volume_quote == 15
    tk = Ticker.from_api({"instId": "A-USDT", "last": "", "bidPx": "", "askPx": "1"})
    assert tk.last == 0 and tk.bid is None and tk.volume_24h_quote == 0
    assert not Instrument.from_api({"instId": "A-USDT"}).is_crypto_live


async def test_rate_limiter_waits_when_window_is_full() -> None:
    now = [0.0]
    waits: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        waits.append(seconds)
        now[0] += seconds

    limiter = RateLimiter(2, 2.0, clock=lambda: now[0], sleep=fake_sleep)
    for _ in range(3):
        await limiter.acquire()
    assert waits == [2.0]
    with pytest.raises(ValueError):
        RateLimiter(0, 1)
