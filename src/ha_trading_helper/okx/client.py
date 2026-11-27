"""Thin async client for the public OKX v5 market endpoints.

- ``/api/v5/market/candles``: most recent candles, up to 300 per request.
- ``/api/v5/market/history-candles``: older candles, up to 100 per request.
- ``/api/v5/market/tickers``: 24h stats for every instrument of a type.
- ``/api/v5/market/books``: order book, up to 400 levels a side.

Pagination goes backwards in time with ``after`` (return records older than ``ts``).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
from types import TracebackType
from typing import Any, Self

import httpx
import structlog

from ha_trading_helper.okx.models import Candle, Instrument, Ticker
from ha_trading_helper.okx.rate_limit import RateLimiter
from ha_trading_helper.timeframe import okx_bar

BASE_URL = "https://www.okx.com"
RECENT_LIMIT = 300
HISTORY_LIMIT = 100
RETRY_STATUS = {429, 500, 502, 503, 504}

log = structlog.get_logger(__name__)


class OkxError(RuntimeError):
    """OKX answered with an error code or kept failing after retries."""


class OkxClient:
    def __init__(
        self,
        *,
        base_url: str = BASE_URL,
        http: httpx.AsyncClient | None = None,
        max_retries: int = 4,
        backoff: float = 0.5,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._http = http or httpx.AsyncClient(base_url=base_url, timeout=15.0)
        self._owns_http = http is None
        self._max_retries = max_retries
        self._backoff = backoff
        self._sleep = sleep
        # Documented limits: candles 40/2s, history-candles 20/2s, tickers 20/2s, books 40/2s.
        # Stay a bit below them.
        self._limiters = {
            "/api/v5/market/candles": RateLimiter(30, 2.0, sleep=sleep),
            "/api/v5/market/history-candles": RateLimiter(15, 2.0, sleep=sleep),
            "/api/v5/market/tickers": RateLimiter(15, 2.0, sleep=sleep),
            "/api/v5/public/instruments": RateLimiter(15, 2.0, sleep=sleep),
            "/api/v5/market/books": RateLimiter(30, 2.0, sleep=sleep),
        }

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def _get(self, path: str, params: dict[str, str]) -> list[Any]:
        limiter = self._limiters.get(path)
        last_error: str = ""
        for attempt in range(self._max_retries + 1):
            if limiter:
                await limiter.acquire()
            try:
                response = await self._http.get(path, params=params)
            except httpx.TransportError as exc:
                last_error = f"transport error: {exc!r}"
            else:
                if response.status_code in RETRY_STATUS:
                    last_error = f"HTTP {response.status_code}"
                elif response.status_code >= 400:
                    raise OkxError(f"{path} HTTP {response.status_code}: {response.text[:200]}")
                else:
                    body = response.json()
                    if str(body.get("code")) != "0":
                        # 50011 = rate limit; anything else is not retryable.
                        if str(body.get("code")) == "50011":
                            last_error = "rate limited (50011)"
                        else:
                            raise OkxError(f"{path} code {body.get('code')}: {body.get('msg')}")
                    else:
                        data: list[Any] = body.get("data", [])
                        return data
            if attempt < self._max_retries:
                delay = self._backoff * 2**attempt
                log.warning("okx_retry", path=path, attempt=attempt + 1, error=last_error)
                await self._sleep(delay)
        raise OkxError(f"{path} failed after {self._max_retries + 1} attempts: {last_error}")

    async def tickers(self, inst_type: str = "SPOT") -> list[Ticker]:
        data = await self._get("/api/v5/market/tickers", {"instType": inst_type})
        return [Ticker.from_api(item) for item in data]

    async def instruments(self, inst_type: str = "SPOT") -> list[Instrument]:
        data = await self._get("/api/v5/public/instruments", {"instType": inst_type})
        return [Instrument.from_api(item) for item in data]

    async def order_book(
        self, inst_id: str, depth: int = 400
    ) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
        """(bids, asks) as (price, size in base currency), best first."""
        data = await self._get("/api/v5/market/books", {"instId": inst_id, "sz": str(depth)})
        if not data:
            return [], []
        book = data[0]
        bids = [(float(r[0]), float(r[1])) for r in book.get("bids", [])]
        asks = [(float(r[0]), float(r[1])) for r in book.get("asks", [])]
        return bids, asks

    async def recent_candles(
        self, inst_id: str, bar: str = "4H", limit: int = RECENT_LIMIT
    ) -> list[Candle]:
        params = {"instId": inst_id, "bar": okx_bar(bar), "limit": str(min(limit, RECENT_LIMIT))}
        rows = await self._get("/api/v5/market/candles", params)
        return sorted((Candle.from_row(r) for r in rows), key=lambda c: c.ts)

    async def candles(
        self,
        inst_id: str,
        bar: str = "4H",
        *,
        count: int = 300,
        since: datetime | None = None,
    ) -> list[Candle]:
        """Return up to ``count`` candles (oldest first), newest included.

        Stops early when it reaches ``since`` or runs out of history.
        """
        result: dict[datetime, Candle] = {}
        batch = await self.recent_candles(inst_id, bar, min(count, RECENT_LIMIT))
        for candle in batch:
            result[candle.ts] = candle
        while batch and len(result) < count:
            oldest = min(result)
            if since is not None and oldest <= since:
                break
            params = {
                "instId": inst_id,
                "bar": okx_bar(bar),
                "after": str(int(oldest.timestamp() * 1000)),
                "limit": str(HISTORY_LIMIT),
            }
            rows = await self._get("/api/v5/market/history-candles", params)
            batch = [Candle.from_row(r) for r in rows]
            for candle in batch:
                result[candle.ts] = candle
        ordered = sorted(result.values(), key=lambda c: c.ts)
        if since is not None:
            ordered = [c for c in ordered if c.ts >= since]
        return ordered[-count:]
