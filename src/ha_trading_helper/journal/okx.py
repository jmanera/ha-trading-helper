"""Read-only signed client for the private OKX v5 endpoints the journal needs.

Only GET requests: the journal never changes anything in the account, and the API key it
gets should have the *Read* permission only.

Paginated endpoints return newest first; ``after=<id>`` asks for older records. Syncs stop
paging once they reach a record already stored (``since``).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime
from types import TracebackType
from typing import Any, Self
from urllib.parse import urlencode

import httpx
import structlog

from ha_trading_helper.journal.models import Balance, Bill, Bot, Fill, OpenOrder, Order
from ha_trading_helper.okx.client import OkxError
from ha_trading_helper.okx.rate_limit import RateLimiter
from ha_trading_helper.okx.trade import sign

PAGE = 100
RETRY_STATUS = {429, 500, 502, 503, 504}
RETRY_CODES = {"50011", "50013", "50026"}  # rate limit, busy, system error

log = structlog.get_logger(__name__)


def _older(record_id: str, since: str | None) -> bool:
    return since is not None and int(record_id) <= int(since)


class OkxReadClient:
    def __init__(
        self,
        api_key: str,
        secret: str,
        passphrase: str,
        *,
        base_url: str,
        http: httpx.AsyncClient | None = None,
        max_retries: int = 4,
        backoff: float = 0.5,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._key, self._secret, self._passphrase = api_key, secret, passphrase
        self._http = http or httpx.AsyncClient(base_url=base_url, timeout=20.0)
        self._owns_http = http is None
        self._max_retries = max_retries
        self._backoff = backoff
        self._sleep = sleep
        # Documented limits per 2 s; stay below them.
        self._limiters = {
            "/api/v5/account/bills-archive": RateLimiter(4, 2.0, sleep=sleep),
            "/api/v5/trade/fills-history": RateLimiter(8, 2.0, sleep=sleep),
            "/api/v5/trade/orders-history-archive": RateLimiter(4, 2.0, sleep=sleep),
            "/api/v5/market/history-candles": RateLimiter(15, 2.0, sleep=sleep),
        }
        self._default_limiter = RateLimiter(8, 2.0, sleep=sleep)

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

    def _headers(self, request_path: str) -> dict[str, str]:
        ts = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        return {
            "OK-ACCESS-KEY": self._key,
            "OK-ACCESS-SIGN": sign(self._secret, ts, "GET", request_path, ""),
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": self._passphrase,
        }

    async def get(self, path: str, params: dict[str, str] | None = None) -> list[Any]:
        request_path = f"{path}?{urlencode(params)}" if params else path
        limiter = self._limiters.get(path, self._default_limiter)
        last_error = ""
        for attempt in range(self._max_retries + 1):
            await limiter.acquire()
            try:
                response = await self._http.get(request_path, headers=self._headers(request_path))
            except httpx.TransportError as exc:
                last_error = f"sin conexión con OKX ({exc!r})"
            else:
                if response.status_code in RETRY_STATUS:
                    last_error = f"HTTP {response.status_code}"
                else:
                    try:
                        body = response.json()
                    except ValueError as exc:
                        raise OkxError(
                            f"{path} HTTP {response.status_code}: {response.text[:200]}"
                        ) from exc
                    code = str(body.get("code"))
                    if code == "0":
                        data: list[Any] = body.get("data") or []
                        return data
                    if code not in RETRY_CODES:
                        raise OkxError(f"{path} code {code}: {body.get('msg')}")
                    last_error = f"code {code}"
            if attempt < self._max_retries:
                log.warning("okx_retry", path=path, attempt=attempt + 1, error=last_error)
                await self._sleep(self._backoff * 2**attempt)
        raise OkxError(f"{path} falló tras {self._max_retries + 1} intentos: {last_error}")

    async def _pages(
        self, path: str, params: dict[str, str], id_key: str, since: str | None
    ) -> list[dict[str, Any]]:
        """Every record newer than ``since`` (all available ones without it), newest first."""
        out: list[dict[str, Any]] = []
        after: str | None = None
        while True:
            query = {**params, "limit": str(PAGE)}
            if after:
                query["after"] = after
            batch: list[dict[str, Any]] = await self.get(path, query)
            for item in batch:
                if _older(str(item[id_key]), since):
                    return out
                out.append(item)
            if len(batch) < PAGE:
                return out
            after = str(batch[-1][id_key])

    # account ---------------------------------------------------------------------------

    async def balances(self) -> list[Balance]:
        data = await self.get("/api/v5/account/balance")
        details = data[0].get("details", []) if data else []
        return [Balance.from_api(d) for d in details]

    async def fills(self, since: str | None = None) -> list[Fill]:
        """Spot fills of the last 3 months (OKX keeps no more through the API)."""
        items = await self._pages(
            "/api/v5/trade/fills-history", {"instType": "SPOT"}, "billId", since
        )
        return [Fill.from_api(i) for i in items]

    async def bills(self, since: str | None = None) -> list[Bill]:
        """Trading account ledger of the last 3 months."""
        items = await self._pages("/api/v5/account/bills-archive", {}, "billId", since)
        return [Bill.from_api(i) for i in items]

    async def orders(self, since: str | None = None) -> list[Order]:
        items = await self._pages(
            "/api/v5/trade/orders-history-archive", {"instType": "SPOT"}, "ordId", since
        )
        return [Order.from_api(i) for i in items]

    async def open_orders(self) -> list[OpenOrder]:
        orders = await self._pages(
            "/api/v5/trade/orders-pending", {"instType": "SPOT"}, "ordId", None
        )
        result = [OpenOrder.from_order(o) for o in orders]
        for ord_type in ("conditional,oco", "trigger", "move_order_stop"):
            algos = await self._pages(
                "/api/v5/trade/orders-algo-pending",
                {"instType": "SPOT", "ordType": ord_type},
                "algoId",
                None,
            )
            result += [OpenOrder.from_algo(a) for a in algos]
        return result

    async def bots(self) -> list[Bot]:
        result: list[Bot] = []
        for path in (
            "/api/v5/tradingBot/grid/orders-algo-pending",
            "/api/v5/tradingBot/grid/orders-algo-history",
        ):
            items = await self._pages(path, {"algoOrdType": "grid"}, "algoId", None)
            result += [Bot.from_api(i) for i in items if i.get("instType", "SPOT") == "SPOT"]
        return result

    async def fee_rates(self) -> dict[str, tuple[float, float]]:
        """Spot (maker, taker) rates of each fee group, as positive fractions."""
        data = await self.get("/api/v5/account/trade-fee", {"instType": "SPOT"})
        groups = data[0].get("feeGroup", []) if data else []
        return {str(g["groupId"]): (abs(float(g["maker"])), abs(float(g["taker"]))) for g in groups}

    # public ----------------------------------------------------------------------------

    async def fee_groups(self) -> dict[str, str]:
        """Fee group of each spot instrument."""
        data = await self.get("/api/v5/public/instruments", {"instType": "SPOT"})
        return {i["instId"]: str(i.get("groupId") or "1") for i in data}

    async def last_prices(self) -> dict[str, float]:
        data = await self.get("/api/v5/market/tickers", {"instType": "SPOT"})
        return {t["instId"]: float(t["last"]) for t in data if t.get("last")}

    async def daily_closes(self, inst_id: str, since: date) -> dict[date, float]:
        """Closed UTC daily candles from ``since`` on."""
        since_ms = int(datetime(since.year, since.month, since.day, tzinfo=UTC).timestamp() * 1000)
        out: dict[date, float] = {}
        after: str | None = None
        while True:
            query = {"instId": inst_id, "bar": "1Dutc", "limit": str(PAGE)}
            if after:
                query["after"] = after
            rows: list[list[str]] = await self.get("/api/v5/market/history-candles", query)
            for row in rows:
                if int(row[0]) < since_ms:
                    return out
                if len(row) <= 8 or row[8] == "1":
                    out[datetime.fromtimestamp(int(row[0]) / 1000, tz=UTC).date()] = float(row[4])
            if len(rows) < PAGE:
                return out
            after = rows[-1][0]
