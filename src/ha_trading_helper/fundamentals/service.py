"""Long-running fundamentals app: the 4h pair list refreshed every few minutes and the panel."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import structlog
import uvicorn

from ha_trading_helper.fundamentals.analysis import Analysis, Analyzer
from ha_trading_helper.fundamentals.cache import Cache
from ha_trading_helper.fundamentals.config import FundamentalsSettings
from ha_trading_helper.fundamentals.market import Pair, load_pairs
from ha_trading_helper.fundamentals.sentiment import Global, record
from ha_trading_helper.fundamentals.sources import CoinGecko, DefiLlama, GitHub
from ha_trading_helper.okx.client import OkxClient

# Free Demo plan: 30 calls a minute and 10.000 a month.
COINGECKO_MONTHLY = 10_000
COINGECKO_CALLS_PER_COIN = 2  # /coins/{id} and its market chart
# /global is read when the market page opens and, for the daily history, a few times a day
# by the loop: under 1.500 calls a month even with the page always open.
GLOBAL_MINUTES = 30
GLOBAL_LOOP_HOURS = 6

log = structlog.get_logger(__name__)


@dataclass
class BulkRun:
    """Progress of the analysis of every pair, one after another."""

    total: int
    started: float
    done: int = 0
    reused: int = 0  # still fresh in the cache
    incomplete: int = 0  # a source failed
    current: str = ""
    finished: float | None = None
    cancelled: bool = False

    @property
    def running(self) -> bool:
        return self.finished is None


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
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.settings = settings
        self.cache = cache or Cache(settings.data_dir / "fundamentals.db")
        self._clock = clock
        self.okx = okx or OkxClient()
        self.sources = (
            coingecko or CoinGecko(settings.coingecko_api_key, on_call=self._count_coingecko),
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
        self.bulk: BulkRun | None = None
        self._bulk_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._global_lock = asyncio.Lock()
        self.global_error = ""

    def _usage_key(self) -> str:
        return f"usage:coingecko:{datetime.fromtimestamp(self._clock(), tz=UTC):%Y-%m}"

    def _count_coingecko(self) -> None:
        self.cache.bump(self._usage_key())

    @property
    def coingecko_calls(self) -> int:
        """CoinGecko requests sent this calendar month (UTC), retries included."""
        return int(self.cache.get(self._usage_key()) or 0)

    async def refresh(self) -> None:
        async with self._lock:
            try:
                self.pairs = await load_pairs(self.okx)
            except Exception as exc:
                log.warning("fundamentals_pairs_failed", error=str(exc))
                self.error = str(exc)
                return
            self.updated, self.error = self._clock(), ""
            self.cache.put("pairs", {"ts": self.updated, "pairs": [asdict(p) for p in self.pairs]})

    @property
    def market(self) -> Global | None:
        """The last reading of the whole market, however old."""
        saved = self.cache.get("global")
        return Global.from_json(saved) if saved else None

    @property
    def history(self) -> dict[str, Any]:
        """Fear & Greed readings by local day, one a day, recorded by the app."""
        return dict(self.cache.get("global:history") or {})

    def today(self) -> date:
        return datetime.fromtimestamp(self._clock(), tz=ZoneInfo(self.settings.timezone)).date()

    async def refresh_global(self, max_age: float = GLOBAL_MINUTES * 60) -> Global | None:
        """Read ``/global`` unless the last reading is younger than ``max_age`` seconds."""
        async with self._global_lock:
            age = self.cache.age("global")
            if age is not None and age < max_age:
                return self.market
            try:
                reading = Global.parse(await self.sources[0].global_data())
            except Exception as exc:
                log.warning("fundamentals_global_failed", error=str(exc))
                self.global_error = str(exc)
                return self.market
            self.global_error = ""
            self.cache.put("global", reading.to_json())
            self.cache.put("global:history", record(self.history, self.today(), reading))
            return reading

    def pair(self, symbol: str) -> Pair | None:
        return next((p for p in self.pairs if p.base == symbol.upper()), None)

    def cached(self, symbol: str) -> Analysis | None:
        return self.analyzer.cached(symbol)

    def pending(self) -> list[Pair]:
        """Pairs without a fresh analysis, most traded first."""
        rows = [p for p in self.pairs if self.cached(p.base) is None]
        return sorted(rows, key=lambda p: p.volume_24h, reverse=True)

    async def analysis(self, symbol: str, *, refresh: bool = False) -> Analysis | None:
        """None when the coin has no USDC pair on OKX."""
        pair = self.pair(symbol)
        if pair is None:
            return None
        return await self.analyzer.analyze(
            pair.base, pair.inst_id, listed=pair.listed, refresh=refresh
        )

    def start_bulk(self) -> bool:
        """Analyse every pair without a fresh analysis in the background; False if running."""
        if self.bulk is not None and self.bulk.running:
            return False
        todo = self.pending()
        self.bulk = BulkRun(
            total=len(self.pairs), started=self._clock(), reused=len(self.pairs) - len(todo)
        )
        self.bulk.done = self.bulk.reused
        self._bulk_task = asyncio.create_task(self._run_bulk(self.bulk, todo))
        return True

    def cancel_bulk(self) -> None:
        if self.bulk is not None and self.bulk.running:
            self.bulk.cancelled = True

    async def _run_bulk(self, run: BulkRun, todo: list[Pair]) -> None:
        log.info("fundamentals_bulk_started", pairs=len(todo))
        try:
            for pair in todo:
                if run.cancelled:
                    break
                run.current = pair.base
                try:
                    result = await self.analysis(pair.base)
                    if result is None or any(
                        s.startswith("error") for s in result.sources.values()
                    ):
                        run.incomplete += 1
                except Exception as exc:
                    log.warning("fundamentals_bulk_failed", symbol=pair.base, error=str(exc))
                    run.incomplete += 1
                run.done += 1
        finally:
            run.current, run.finished = "", self._clock()
            log.info("fundamentals_bulk_finished", done=run.done, incomplete=run.incomplete)

    async def refresh_loop(self) -> None:
        while True:
            await self.refresh()
            await self.refresh_global(GLOBAL_LOOP_HOURS * 3600)
            await asyncio.sleep(self.settings.refresh_minutes * 60)

    async def aclose(self) -> None:
        if self._bulk_task is not None:
            self._bulk_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._bulk_task
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
