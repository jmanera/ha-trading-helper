"""Public sources of fundamental data, all read-only.

- CoinGecko (``/coins/markets``, ``/coins/{id}``, ``/coins/{id}/market_chart``): market data
  and supply. Works without a key; a free Demo key raises the limit to 30 calls a minute.
  Since 28/08/2026 the Demo plan no longer returns ``developer_data``.
- DefiLlama (no key): TVL of protocols and chains, fees and revenue.
- GitHub: activity of the project's repositories. 60 calls an hour per IP without a token,
  5.000 with one.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from types import TracebackType
from typing import Any, Self

import httpx
import structlog

from ha_trading_helper.okx.rate_limit import RateLimiter

COINGECKO_URL = "https://api.coingecko.com/api/v3"
DEFILLAMA_URL = "https://api.llama.fi"
GITHUB_URL = "https://api.github.com"
RETRY_STATUS = {429, 500, 502, 503, 504}
RATE_LIMITED = "límite de consultas agotado, prueba más tarde o añade una clave"

log = structlog.get_logger(__name__)


class SourceError(RuntimeError):
    """A source answered with an error or kept failing after retries."""


class JsonApi:
    """GET JSON with a rate limit and retries; ``None`` when the thing does not exist."""

    name = "api"
    missing_status: frozenset[int] = frozenset({404})

    def __init__(
        self,
        base_url: str,
        *,
        headers: dict[str, str] | None = None,
        limiter: RateLimiter | None = None,
        http: httpx.AsyncClient | None = None,
        max_retries: int = 3,
        backoff: float = 2.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._http = http or httpx.AsyncClient(base_url=base_url, timeout=30.0)
        self._owns_http = http is None
        self._headers = headers or {}
        self._limiter = limiter
        self._max_retries = max_retries
        self._backoff = backoff
        self._sleep = sleep

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

    async def get(self, path: str, params: dict[str, str] | None = None) -> Any:
        last_error = ""
        for attempt in range(self._max_retries + 1):
            if self._limiter:
                await self._limiter.acquire()
            try:
                response = await self._http.get(path, params=params, headers=self._headers)
            except httpx.TransportError as exc:
                last_error = f"transport error: {exc!r}"
            else:
                if response.status_code in self.missing_status:
                    return None
                if response.status_code == 429:
                    last_error = RATE_LIMITED
                elif response.status_code in RETRY_STATUS:
                    last_error = f"HTTP {response.status_code}"
                elif response.status_code == 403 and "rate limit" in response.text:
                    raise SourceError(f"{self.name}: {RATE_LIMITED}")
                elif response.status_code >= 400:
                    raise SourceError(
                        f"{self.name} {path} HTTP {response.status_code}: {response.text[:200]}"
                    )
                else:
                    return response.json()
            if attempt < self._max_retries:
                log.warning("source_retry", source=self.name, path=path, error=last_error)
                await self._sleep(self._backoff * 2**attempt)
        raise SourceError(f"{self.name}: {last_error}")


class CoinGecko(JsonApi):
    name = "CoinGecko"

    def __init__(self, api_key: str = "", **kwargs: Any) -> None:
        # Without a key the public limit is a few calls a minute and varies with load;
        # a 429 means waiting most of a minute.
        kwargs.setdefault("limiter", RateLimiter(25 if api_key else 5, 60.0))
        kwargs.setdefault("backoff", 15.0)
        headers = {"x-cg-demo-api-key": api_key} if api_key else {}
        super().__init__(COINGECKO_URL, headers=headers, **kwargs)

    async def markets(self, page: int) -> list[dict[str, Any]]:
        """Coins by market cap, 250 a page."""
        data = await self.get(
            "/coins/markets",
            {
                "vs_currency": "usd",
                "order": "market_cap_desc",
                "per_page": "250",
                "page": str(page),
            },
        )
        return list(data or [])

    async def coin(self, coin_id: str) -> dict[str, Any] | None:
        data: dict[str, Any] | None = await self.get(
            f"/coins/{coin_id}",
            {
                "localization": "false",
                "tickers": "false",
                "market_data": "true",
                "community_data": "false",
                "developer_data": "false",
                "sparkline": "false",
            },
        )
        return data

    async def market_chart(self, coin_id: str, days: int = 365) -> dict[str, Any] | None:
        data: dict[str, Any] | None = await self.get(
            f"/coins/{coin_id}/market_chart",
            {"vs_currency": "usd", "days": str(days), "interval": "daily"},
        )
        return data


class DefiLlama(JsonApi):
    name = "DefiLlama"
    missing_status = frozenset({400, 404})  # an unknown protocol is a 400

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("limiter", RateLimiter(5, 1.0))
        super().__init__(DEFILLAMA_URL, **kwargs)

    async def protocols(self) -> list[dict[str, Any]]:
        return list(await self.get("/protocols") or [])

    async def chains(self) -> list[dict[str, Any]]:
        return list(await self.get("/v2/chains") or [])

    async def protocol(self, slug: str) -> dict[str, Any] | None:
        data: dict[str, Any] | None = await self.get(f"/protocol/{slug}")
        return data

    async def chain_tvl(self, chain: str) -> list[dict[str, Any]]:
        return list(await self.get(f"/v2/historicalChainTvl/{chain}") or [])

    async def fees_overview(self, data_type: str = "dailyFees") -> list[dict[str, Any]]:
        data = await self.get(
            "/overview/fees",
            {
                "dataType": data_type,
                "excludeTotalDataChart": "true",
                "excludeTotalDataChartBreakdown": "true",
            },
        )
        return list((data or {}).get("protocols", []))


class GitHub(JsonApi):
    name = "GitHub"

    def __init__(self, token: str = "", **kwargs: Any) -> None:  # nosec B107 - no token
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        kwargs.setdefault("max_retries", 1)
        super().__init__(GITHUB_URL, headers=headers, **kwargs)

    async def repos(self, owner: str) -> list[dict[str, Any]]:
        """The owner's (user or organisation) 100 most recently pushed repositories."""
        data = await self.get(
            f"/users/{owner}/repos", {"per_page": "100", "sort": "pushed", "type": "owner"}
        )
        return list(data or [])
