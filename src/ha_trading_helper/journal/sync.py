"""Bring the database up to date with the OKX account (read-only)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

import structlog

from ha_trading_helper.journal.config import REFERENCE
from ha_trading_helper.journal.models import Balance, day_of
from ha_trading_helper.journal.okx import OkxReadClient
from ha_trading_helper.journal.prices import from_tickers, price_candidates
from ha_trading_helper.journal.store import Store, bills, fills, orders
from ha_trading_helper.okx.client import OkxError

log = structlog.get_logger(__name__)

FEES_EVERY = timedelta(hours=12)


@dataclass
class SyncResult:
    ts: int
    equity: float = 0.0
    new_fills: int = 0
    new_bills: int = 0
    new_orders: int = 0
    open_orders: int = 0
    bots: int = 0
    prices: int = 0
    errors: list[str] = field(default_factory=list)


def value_now(
    balances: list[Balance], last: dict[str, float]
) -> tuple[float, float, dict[str, float]]:
    """Account value and bots' share in USDC, with the price used for each currency."""
    equity = bots = 0.0
    used: dict[str, float] = {}
    for b in balances:
        px = from_tickers(b.ccy, last)
        if px is None:
            # OKX's own USD valuation; USDC trades at par with it.
            px = b.eq_usd / b.eq if b.eq else 0.0
        used[b.ccy] = px
        equity += b.eq * px
        bots += b.stgy_eq * px
    return equity, bots, used


class Syncer:
    def __init__(self, store: Store, client: OkxReadClient) -> None:
        self.store = store
        self.client = client

    async def run(self, now: datetime | None = None) -> SyncResult:
        now = now or datetime.now(UTC)
        result = SyncResult(ts=int(now.timestamp() * 1000))
        steps = (
            ("saldos", self._balances),
            ("operaciones", self._fills),
            ("movimientos", self._bills),
            ("órdenes", self._orders),
            ("órdenes abiertas", self._open_orders),
            ("bots", self._bots),
            ("comisiones", self._fees),
            ("precios", self._prices),
        )
        for name, step in steps:
            try:
                await step(result, now)
            except (OkxError, OSError) as exc:
                log.warning("journal_sync_step_failed", step=name, error=str(exc))
                result.errors.append(f"{name}: {exc}")
        self.store.set_json(
            "last_sync",
            {"ts": result.ts, "errors": result.errors, "new_fills": result.new_fills},
        )
        log.info(
            "journal_synced",
            equity=round(result.equity, 2),
            fills=result.new_fills,
            bills=result.new_bills,
            errors=len(result.errors),
        )
        return result

    async def _balances(self, result: SyncResult, now: datetime) -> None:
        balances = await self.client.balances()
        last = await self.client.last_prices()
        equity, bots, used = value_now(balances, last)
        self.store.replace_balances(balances)
        self.store.set_json("live_prices", used)
        self.store.add_snapshot(result.ts, equity, bots)
        result.equity = equity

    async def _fills(self, result: SyncResult, now: datetime) -> None:
        new = await self.client.fills(since=self.store.last_id(fills, "bill_id"))
        self.store.save_fills(new)
        result.new_fills = len(new)

    async def _bills(self, result: SyncResult, now: datetime) -> None:
        new = await self.client.bills(since=self.store.last_id(bills, "bill_id"))
        self.store.save_bills(new)
        result.new_bills = len(new)

    async def _orders(self, result: SyncResult, now: datetime) -> None:
        new = await self.client.orders(since=self.store.last_id(orders, "ord_id"))
        self.store.save_orders(new)
        result.new_orders = len(new)

    async def _open_orders(self, result: SyncResult, now: datetime) -> None:
        current = await self.client.open_orders()
        self.store.replace_open_orders(current)
        result.open_orders = len(current)

    async def _bots(self, result: SyncResult, now: datetime) -> None:
        current = await self.client.bots()
        self.store.save_bots(current)
        result.bots = len(current)

    async def _fees(self, result: SyncResult, now: datetime) -> None:
        updated = int(self.store.get_meta("fees_updated") or 0)
        if result.ts - updated < FEES_EVERY.total_seconds() * 1000:
            return
        self.store.set_json("fee_rates", await self.client.fee_rates())
        self.store.set_json("fee_groups", await self.client.fee_groups())
        self.store.set_meta("fees_updated", str(result.ts))

    async def _prices(self, result: SyncResult, now: datetime) -> None:
        """Daily closes of every currency the account has touched, up to yesterday."""
        stored_bills = self.store.bills()
        stored_fills = self.store.fills()
        stamps = [b.ts for b in stored_bills] + [f.ts for f in stored_fills]
        if not stamps:
            return
        first = day_of(min(stamps))
        yesterday = now.date() - timedelta(days=1)
        ccys = {b.ccy for b in stored_bills} | {f.base for f in stored_fills}
        ccys |= {f.quote for f in stored_fills} | {b.ccy for b in self.store.balances()}
        known = self.store.prices()
        sources: dict[str, str] = self.store.get_json("price_sources", {})
        for ccy in sorted(ccys - {REFERENCE}):
            have = known.get(ccy, {})
            since = max(have) + timedelta(days=1) if have else first
            if since > yesterday or sources.get(ccy) == "":
                continue
            closes = await self._closes(ccy, since, sources)
            if closes:
                self.store.save_prices(ccy, closes)
                result.prices += len(closes)
        self.store.set_json("price_sources", sources)

    async def _closes(self, ccy: str, since: date, sources: dict[str, str]) -> dict[date, float]:
        candidates = price_candidates(ccy)
        if ccy in sources:
            candidates = [c for c in candidates if c[0] == sources[ccy]]
        for inst, inverted in candidates:
            try:
                closes = await self.client.daily_closes(inst, since)
            except OkxError as exc:
                if "code 51" in str(exc):
                    continue  # 51xxx: no such instrument on this OKX domain
                raise
            sources[ccy] = inst
            return {d: 1 / c if inverted else c for d, c in closes.items() if c}
        sources[ccy] = ""  # nothing prices it; stop asking every sync
        return {}
