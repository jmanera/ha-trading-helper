"""Signed client for the OKX v5 trading endpoints (spot, cash mode).

Every request is signed with the API key, secret and passphrase. With ``demo=True``
requests carry ``x-simulated-trading: 1`` and run against OKX demo trading, which
needs API keys created in demo mode.

Orders are never retried automatically: a timeout after the request reached OKX
could otherwise buy twice. Each order carries a ``clOrdId`` so it can be traced.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal
from types import TracebackType
from typing import Any, Self
from urllib.parse import urlencode

import httpx
from pydantic import BaseModel

from ha_trading_helper.okx.client import BASE_URL, OkxError


class OrderFill(BaseModel):
    order_id: str
    state: str  # live, partially_filled, filled, canceled, mmp_canceled
    avg_price: float
    filled: float  # base currency
    fee: float  # negative = charged
    fee_ccy: str

    @property
    def done(self) -> bool:
        return self.state in {"filled", "canceled", "mmp_canceled"}

    @classmethod
    def from_api(cls, item: dict[str, str]) -> OrderFill:
        def num(key: str) -> float:
            return float(item.get(key) or 0)

        return cls(
            order_id=item["ordId"],
            state=item.get("state", ""),
            avg_price=num("avgPx"),
            filled=num("accFillSz"),
            fee=num("fee"),
            fee_ccy=item.get("feeCcy", ""),
        )


class LotRules(BaseModel):
    lot_size: Decimal
    min_size: Decimal

    def floor(self, qty: float) -> Decimal:
        """Largest multiple of the lot size not above ``qty``."""
        lots = (Decimal(str(qty)) / self.lot_size).to_integral_value(rounding=ROUND_DOWN)
        # quantize keeps a plain decimal (never "1E+3"), which is what OKX expects in sz.
        return (lots * self.lot_size).quantize(self.lot_size)


def sign(secret: str, timestamp: str, method: str, request_path: str, body: str) -> str:
    digest = hmac.new(
        secret.encode(), f"{timestamp}{method}{request_path}{body}".encode(), hashlib.sha256
    ).digest()
    return base64.b64encode(digest).decode()


def client_order_id() -> str:
    return f"hth{uuid.uuid4().hex[:24]}"


class OkxTradeClient:
    def __init__(
        self,
        api_key: str,
        secret: str,
        passphrase: str,
        *,
        demo: bool = True,
        base_url: str = BASE_URL,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._key, self._secret, self._passphrase = api_key, secret, passphrase
        self.demo = demo
        self._http = http or httpx.AsyncClient(base_url=base_url, timeout=15.0)
        self._owns_http = http is None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._owns_http:
            await self._http.aclose()

    def _headers(self, method: str, request_path: str, body: str) -> dict[str, str]:
        ts = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        headers = {
            "OK-ACCESS-KEY": self._key,
            "OK-ACCESS-SIGN": sign(self._secret, ts, method, request_path, body),
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": self._passphrase,
            "Content-Type": "application/json",
        }
        if self.demo:
            headers["x-simulated-trading"] = "1"
        return headers

    async def _request(
        self, method: str, path: str, *, params: dict[str, str] | None = None, body: Any = None
    ) -> list[dict[str, Any]]:
        request_path = f"{path}?{urlencode(params)}" if params else path
        payload = json.dumps(body) if body is not None else ""
        try:
            response = await self._http.request(
                method,
                request_path,
                content=payload or None,
                headers=self._headers(method, request_path, payload),
            )
        except httpx.TransportError as exc:
            raise OkxError(f"{path}: sin conexión con OKX ({exc!r})") from exc
        try:
            data = response.json()
        except ValueError as exc:
            raise OkxError(f"{path} HTTP {response.status_code}: {response.text[:200]}") from exc
        items: list[dict[str, Any]] = data.get("data") or []
        if str(data.get("code")) != "0":
            # Order endpoints put the useful message in data[].sMsg.
            detail = next((i.get("sMsg") for i in items if i.get("sMsg")), None)
            raise OkxError(detail or f"{path} code {data.get('code')}: {data.get('msg')}")
        return items

    async def lot_rules(self, inst_id: str) -> LotRules:
        items = await self._request(
            "GET", "/api/v5/public/instruments", params={"instType": "SPOT", "instId": inst_id}
        )
        if not items:
            raise OkxError(f"{inst_id} no existe en OKX spot")
        return LotRules(lot_size=Decimal(items[0]["lotSz"]), min_size=Decimal(items[0]["minSz"]))

    async def market_order(self, inst_id: str, side: str, size: str, *, quote: bool) -> str:
        """Place a spot market order; ``quote`` sizes it in the quote currency."""
        body = {
            "instId": inst_id,
            "tdMode": "cash",
            "side": side,
            "ordType": "market",
            "sz": size,
            "tgtCcy": "quote_ccy" if quote else "base_ccy",
            "clOrdId": client_order_id(),
        }
        items = await self._request("POST", "/api/v5/trade/order", body=body)
        return str(items[0]["ordId"])

    async def order(self, inst_id: str, order_id: str) -> OrderFill:
        items = await self._request(
            "GET", "/api/v5/trade/order", params={"instId": inst_id, "ordId": order_id}
        )
        return OrderFill.from_api(items[0])

    async def stop_loss(self, inst_id: str, size: str, trigger_price: float) -> str:
        """Sell ``size`` at market once the last price touches ``trigger_price``."""
        body = {
            "instId": inst_id,
            "tdMode": "cash",
            "side": "sell",
            "ordType": "conditional",
            "sz": size,
            "slTriggerPx": f"{trigger_price:.10g}",
            "slOrdPx": "-1",
        }
        items = await self._request("POST", "/api/v5/trade/order-algo", body=body)
        return str(items[0]["algoId"])

    async def cancel_stop(self, inst_id: str, algo_id: str) -> None:
        await self._request(
            "POST", "/api/v5/trade/cancel-algos", body=[{"algoId": algo_id, "instId": inst_id}]
        )

    async def available(self, ccy: str) -> float:
        items = await self._request("GET", "/api/v5/account/balance", params={"ccy": ccy})
        details = items[0].get("details", []) if items else []
        return next((float(d.get("availBal") or 0) for d in details if d.get("ccy") == ccy), 0.0)
