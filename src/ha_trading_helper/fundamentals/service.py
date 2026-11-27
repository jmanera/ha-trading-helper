"""Long-running fundamentals app: the 4h pair list refreshed every few minutes and the panel."""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import asdict

import structlog
import uvicorn

from ha_trading_helper.fundamentals.analysis import Analysis, Analyzer
from ha_trading_helper.fundamentals.cache import Cache
from ha_trading_helper.fundamentals.config import FundamentalsSettings
from ha_trading_helper.fundamentals.market import Pair, load_pairs
from ha_trading_helper.fundamentals.sources import CoinGecko, DefiLlama, GitHub
from ha_trading_helper.okx.client import OkxClient

log = structlog.get_logger(__name__)


class FundamentalsService:
    def __init__(
        self,
        settings: FundamentalsSettings,
        *,
        okx: OkxClient | None = None,
        coingecko: CoinGecko | None = None,
        defillama: DefiLlama | None = None,
        github: GitHub | None = None,
        cache: Cache | None = None,
    ) -> None:
        self.settings = settings
        self.cache = cache or Cache(settings.data_dir / "fundamentals.db")
        self.okx = okx or OkxClient()
        self.sources = (
            coingecko or CoinGecko(settings.coingecko_api_key),
            defillama or DefiLlama(),
            github or GitHub(settings.github_token),
        )
        self.analyzer = Analyzer(
            coingecko=self.sources[0],
            defillama=self.sources[1],
            github=self.sources[2],
            okx=self.okx,
            cache=self.cache,
            cache_hours=settings.cache_hours,
            coin_ids=settings.coin_id_overrides(),
        )
        saved = self.cache.get("pairs") or {}
        self.pairs: list[Pair] = [Pair(**p) for p in saved.get("pairs", [])]
        self.updated: float | None = saved.get("ts")
        self.error = ""
        self._lock = asyncio.Lock()

    async def refresh(self) -> None:
        async with self._lock:
            try:
                self.pairs = await load_pairs(self.okx)
            except Exception as exc:
                log.warning("fundamentals_pairs_failed", error=str(exc))
                self.error = str(exc)
                return
            self.updated, self.error = time.time(), ""
            self.cache.put("pairs", {"ts": self.updated, "pairs": [asdict(p) for p in self.pairs]})

    def pair(self, symbol: str) -> Pair | None:
        return next((p for p in self.pairs if p.base == symbol.upper()), None)

    def cached(self, symbol: str) -> Analysis | None:
        return self.analyzer.cached(symbol)

    async def analysis(self, symbol: str, *, refresh: bool = False) -> Analysis | None:
        """None when the coin has no USDC pair on OKX."""
        pair = self.pair(symbol)
        if pair is None:
            return None
        return await self.analyzer.analyze(
            pair.base, pair.inst_id, listed=pair.listed, refresh=refresh
        )

    async def refresh_loop(self) -> None:
        while True:
            await self.refresh()
            await asyncio.sleep(self.settings.refresh_minutes * 60)

    async def aclose(self) -> None:
        await self.okx.aclose()
        for source in self.sources:
            await source.aclose()
        self.cache.close()

    async def run_forever(self) -> None:
        from ha_trading_helper.fundamentals.web import create_app

        s = self.settings
        server = uvicorn.Server(
            uvicorn.Config(
                create_app(self, timezone=s.timezone),
                host=s.web_host,
                port=s.web_port,
                log_level="warning",
                proxy_headers=False,
                access_log=False,
            )
        )
        loop_task = asyncio.create_task(self.refresh_loop())
        log.info(
            "fundamentals_started",
            port=s.web_port,
            coingecko_key=bool(s.coingecko_api_key),
            github_token=bool(s.github_token),
        )
        try:
            await server.serve()
        finally:
            loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await loop_task
            await self.aclose()
