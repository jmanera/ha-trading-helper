from __future__ import annotations

import base64
import hashlib
import hmac
import json
from decimal import Decimal

import httpx
import pytest
import respx

from ha_trading_helper.okx.client import BASE_URL, OkxError
from ha_trading_helper.okx.trade import LotRules, OkxTradeClient, OrderFill, sign


def client(demo: bool = True) -> OkxTradeClient:
    return OkxTradeClient("key", "secret", "pass", demo=demo)


def test_sign_matches_okx_recipe() -> None:
    expected = base64.b64encode(
        hmac.new(b"secret", b"2026-01-01T00:00:00.000ZGET/api/v5/x?a=1", hashlib.sha256).digest()
    ).decode()
    assert sign("secret", "2026-01-01T00:00:00.000Z", "GET", "/api/v5/x?a=1", "") == expected


@respx.mock
async def test_market_order_is_signed_and_flagged_as_demo() -> None:
    route = respx.post(f"{BASE_URL}/api/v5/trade/order").mock(
        return_value=httpx.Response(
            200, json={"code": "0", "data": [{"ordId": "42", "sCode": "0"}]}
        )
    )
    async with client() as c:
        assert await c.market_order("SOL-USDC", "buy", "150", quote=True) == "42"
    request = route.calls.last.request
    body = json.loads(request.content)
    assert body["tgtCcy"] == "quote_ccy" and body["tdMode"] == "cash" and body["sz"] == "150"
    assert body["clOrdId"].startswith("hth")
    headers = request.headers
    assert headers["x-simulated-trading"] == "1" and headers["OK-ACCESS-KEY"] == "key"
    ts = headers["OK-ACCESS-TIMESTAMP"]
    assert headers["OK-ACCESS-SIGN"] == sign(
        "secret", ts, "POST", "/api/v5/trade/order", request.content.decode()
    )


@respx.mock
async def test_real_mode_has_no_demo_header_and_errors_use_smsg() -> None:
    route = respx.post(f"{BASE_URL}/api/v5/trade/order").mock(
        return_value=httpx.Response(
            200,
            json={
                "code": "1",
                "msg": "",
                "data": [{"sCode": "51008", "sMsg": "Saldo insuficiente"}],
            },
        )
    )
    async with client(demo=False) as c:
        with pytest.raises(OkxError, match="Saldo insuficiente"):
            await c.market_order("SOL-USDC", "sell", "1", quote=False)
    assert "x-simulated-trading" not in route.calls.last.request.headers
    assert json.loads(route.calls.last.request.content)["tgtCcy"] == "base_ccy"


@respx.mock
async def test_queries_and_algo_orders() -> None:
    respx.get(f"{BASE_URL}/api/v5/trade/order").mock(
        return_value=httpx.Response(200, json={"code": "0", "data": [{
            "ordId": "42", "state": "filled", "avgPx": "180.5", "accFillSz": "0.83",
            "fee": "-0.00083", "feeCcy": "SOL"}]})
    )  # fmt: skip
    respx.get(f"{BASE_URL}/api/v5/public/instruments").mock(
        return_value=httpx.Response(
            200, json={"code": "0", "data": [{"lotSz": "0.0001", "minSz": "0.001"}]}
        )
    )
    algo = respx.post(f"{BASE_URL}/api/v5/trade/order-algo").mock(
        return_value=httpx.Response(200, json={"code": "0", "data": [{"algoId": "7"}]})
    )
    cancel = respx.post(f"{BASE_URL}/api/v5/trade/cancel-algos").mock(
        return_value=httpx.Response(200, json={"code": "0", "data": [{"algoId": "7"}]})
    )
    balance = respx.get(f"{BASE_URL}/api/v5/account/balance").mock(
        return_value=httpx.Response(200, json={"code": "0", "data": [{"details": [
            {"ccy": "SOL", "availBal": "0.8291"}]}]})
    )  # fmt: skip
    async with client() as c:
        fill = await c.order("SOL-USDC", "42")
        assert fill.done and fill.filled == 0.83 and fill.fee_ccy == "SOL"
        rules = await c.lot_rules("SOL-USDC")
        assert rules.floor(0.82917) == Decimal("0.8291")
        assert await c.stop_loss("SOL-USDC", "0.8291", 171.25) == "7"
        await c.cancel_stop("SOL-USDC", "7")
        assert await c.available("SOL") == 0.8291
    assert json.loads(algo.calls.last.request.content)["slTriggerPx"] == "171.25"
    assert json.loads(cancel.calls.last.request.content) == [{"algoId": "7", "instId": "SOL-USDC"}]
    assert balance.calls.last.request.url.params["ccy"] == "SOL"


@respx.mock
async def test_transport_errors_and_missing_data() -> None:
    respx.get(f"{BASE_URL}/api/v5/public/instruments").mock(
        return_value=httpx.Response(200, json={"code": "0", "data": []})
    )
    respx.get(f"{BASE_URL}/api/v5/account/balance").mock(
        return_value=httpx.Response(502, text="bad gateway")
    )
    respx.get(f"{BASE_URL}/api/v5/trade/order").mock(side_effect=httpx.ConnectError("down"))
    async with client() as c:
        with pytest.raises(OkxError, match="no existe"):
            await c.lot_rules("NOPE-USDC")
        with pytest.raises(OkxError, match="HTTP 502"):
            await c.available("SOL")
        with pytest.raises(OkxError, match="sin conexión"):
            await c.order("SOL-USDC", "1")


def test_models() -> None:
    assert not OrderFill.from_api({"ordId": "1", "state": "live"}).done
    whole = LotRules(lot_size=Decimal("1"), min_size=Decimal("1"))
    assert whole.floor(2.9) == Decimal("2") and str(whole.floor(1000.5)) == "1000"
