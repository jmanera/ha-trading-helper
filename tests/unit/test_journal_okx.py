from __future__ import annotations

from datetime import date

import httpx
import pytest
import respx

from ha_trading_helper.journal.models import Bot, OpenOrder
from ha_trading_helper.journal.okx import OkxReadClient
from ha_trading_helper.okx.client import OkxError

BASE = "https://eea.okx.com"


async def no_sleep(_: float) -> None:
    return None


def client() -> OkxReadClient:
    return OkxReadClient("key", "secret", "pass", base_url=BASE, sleep=no_sleep)


def ok(data: list[object]) -> httpx.Response:
    return httpx.Response(200, json={"code": "0", "msg": "", "data": data})


def fill(bill_id: int) -> dict[str, str]:
    return {
        "billId": str(bill_id),
        "tradeId": "1",
        "ordId": "o",
        "instId": "SOL-USDC",
        "side": "buy",
        "fillPx": "50",
        "fillSz": "1",
        "fee": "-0.001",
        "feeCcy": "SOL",
        "feeRate": "-0.001",
        "execType": "T",
        "ts": "1700000000000",
    }


@respx.mock
async def test_requests_are_signed_get_only() -> None:
    route = respx.get(f"{BASE}/api/v5/account/balance").mock(
        return_value=ok(
            [{"details": [{"ccy": "USDC", "eq": "5", "cashBal": "4", "stgyEq": "1", "eqUsd": "5"}]}]
        )
    )
    async with client() as c:
        balances = await c.balances()
    assert balances[0].cash_bal == 4 and balances[0].stgy_eq == 1
    headers = route.calls.last.request.headers
    assert headers["OK-ACCESS-KEY"] == "key" and headers["OK-ACCESS-PASSPHRASE"] == "pass"
    assert "x-simulated-trading" not in headers


@respx.mock
async def test_pages_stop_at_the_stored_record() -> None:
    first = [fill(i) for i in range(300, 200, -1)]
    second = [fill(i) for i in range(200, 150, -1)]
    route = respx.get(f"{BASE}/api/v5/trade/fills-history").mock(
        side_effect=[ok(first), ok(second)]
    )
    async with client() as c:
        fills = await c.fills(since="180")
    assert [f.bill_id for f in fills][-1] == "181"
    assert len(fills) == 120
    assert route.calls[1].request.url.params["after"] == "201"


@respx.mock
async def test_retries_rate_limits_and_fails_on_other_codes() -> None:
    respx.get(f"{BASE}/api/v5/account/bills-archive").mock(
        side_effect=[
            httpx.Response(429),
            httpx.Response(200, json={"code": "50011", "data": []}),
            ok([]),
        ]
    )
    async with client() as c:
        assert await c.bills() == []
    respx.get(f"{BASE}/api/v5/trade/orders-history-archive").mock(
        return_value=httpx.Response(200, json={"code": "50119", "msg": "API key doesn't exist"})
    )
    async with client() as c:
        with pytest.raises(OkxError, match="50119"):
            await c.orders()


@respx.mock
async def test_gives_up_after_retries() -> None:
    respx.get(f"{BASE}/api/v5/account/balance").mock(side_effect=httpx.ConnectError("down"))
    async with client() as c:
        with pytest.raises(OkxError, match="sin conexión"):
            await c.balances()


@respx.mock
async def test_not_json_is_an_error() -> None:
    respx.get(f"{BASE}/api/v5/account/balance").mock(
        return_value=httpx.Response(403, text="<html>")
    )
    async with client() as c:
        with pytest.raises(OkxError, match="HTTP 403"):
            await c.balances()


@respx.mock
async def test_open_orders_bots_fees_and_prices() -> None:
    respx.get(f"{BASE}/api/v5/trade/orders-pending").mock(
        return_value=ok(
            [
                {
                    "ordId": "1",
                    "instId": "SOL-USDC",
                    "side": "sell",
                    "ordType": "limit",
                    "px": "80",
                    "sz": "1",
                    "accFillSz": "0",
                    "cTime": "1",
                }
            ]
        )
    )
    respx.get(f"{BASE}/api/v5/trade/orders-algo-pending").mock(
        side_effect=[
            ok(
                [
                    {
                        "algoId": "9",
                        "instId": "SOL-USDC",
                        "side": "sell",
                        "ordType": "conditional",
                        "sz": "1",
                        "slTriggerPx": "40",
                        "slOrdPx": "-1",
                        "cTime": "2",
                    }
                ]
            ),
            ok([]),
            ok([]),
        ]
    )
    respx.get(f"{BASE}/api/v5/tradingBot/grid/orders-algo-pending").mock(
        return_value=ok(
            [
                {
                    "algoId": "b",
                    "algoOrdType": "grid",
                    "instId": "DOOD-USDC",
                    "instType": "SPOT",
                    "state": "running",
                    "investment": "10",
                    "gridProfit": "0.1",
                    "floatProfit": "-0.3",
                    "cTime": "5",
                }
            ]
        )
    )
    respx.get(f"{BASE}/api/v5/tradingBot/grid/orders-algo-history").mock(
        return_value=ok(
            [
                {
                    "algoId": "c",
                    "instId": "BTC-USDC",
                    "state": "stopped",
                    "investment": "5",
                    "totalPnl": "1",
                    "cTime": "1",
                    "uTime": "9",
                }
            ]
        )
    )
    respx.get(f"{BASE}/api/v5/account/trade-fee").mock(
        return_value=ok([{"feeGroup": [{"groupId": "1", "maker": "-0.0008", "taker": "-0.001"}]}])
    )
    respx.get(f"{BASE}/api/v5/public/instruments").mock(
        return_value=ok([{"instId": "SOL-USDC", "groupId": "1"}])
    )
    respx.get(f"{BASE}/api/v5/market/tickers").mock(
        return_value=ok([{"instId": "SOL-USDC", "last": "70"}, {"instId": "X-USDC", "last": ""}])
    )
    async with client() as c:
        orders = await c.open_orders()
        bots = await c.bots()
        fees = await c.fee_rates()
        groups = await c.fee_groups()
        last = await c.last_prices()
    assert orders == [
        OpenOrder("1", "order", "SOL-USDC", "sell", "limit", 80.0, 1.0, 0.0, None, 1),
        OpenOrder("9", "algo", "SOL-USDC", "sell", "conditional", None, 1.0, 0.0, 40.0, 2),
    ]
    assert bots[0] == Bot(
        "b", "grid", "DOOD-USDC", "running", 10.0, pytest.approx(-0.2), 0.1, -0.3, 5, None
    )
    assert bots[1].stop_time == 9 and bots[1].total_pnl == 1
    assert fees == {"1": (0.0008, 0.001)}
    assert groups == {"SOL-USDC": "1"}
    assert last == {"SOL-USDC": 70.0}


@respx.mock
async def test_daily_closes_skip_open_candle_and_stop_at_since() -> None:
    day = 86_400_000
    start = 1_793_491_200_000  # 2026-11-01 00:00 UTC
    rows = [
        [str(start + i * day), "1", "1", "1", str(10 + i), "1", "1", "1", "0" if i == 3 else "1"]
        for i in range(3, -2, -1)
    ]
    respx.get(f"{BASE}/api/v5/market/history-candles").mock(return_value=ok(rows))
    async with client() as c:
        closes = await c.daily_closes("SOL-USDC", date(2026, 11, 1))
    assert closes == {date(2026, 11, 1): 10.0, date(2026, 11, 2): 11.0, date(2026, 11, 3): 12.0}
