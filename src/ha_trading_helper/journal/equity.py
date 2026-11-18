"""Daily account value, money moved in and out, and returns over any period (pure, no I/O).

OKX keeps no history of the account value, so it is rebuilt from the ledger: every entry
carries the balance of its currency after it (``bal``), which gives the end-of-day holdings,
valued at that day's close. Bots are added at their investment for the days before the
journal started taking snapshots, and at their real value afterwards.

Returns keep deposits apart from gains:

- profit = value at the end − value at the start − net money moved in;
- time-weighted return (TWR) chains daily returns, each one discounting that day's money
  moved in (taken as arriving at the start of the day), so adding money does not look like
  a gain and the size of each deposit does not distort the percentage.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from ha_trading_helper.journal.models import (
    BILL_STRATEGY,
    BILL_TRADE,
    BILL_TRANSFER,
    Bill,
    Bot,
    day_of,
)
from ha_trading_helper.journal.prices import PriceBook

DAY_MS = 86_400_000


def _tol(value: float) -> float:
    return max(1e-9, abs(value) * 1e-6)


def _key(b: Bill) -> tuple[int, int]:
    return b.ts, int(b.bill_id)


def account_bills(
    bills: Iterable[Bill], cash_now: dict[str, float], fill_orders: set[str]
) -> tuple[list[Bill], dict[str, float]]:
    """The entries of the trading account itself, and what it held before the first one.

    The ledger also carries a bot's own books: its trades (orders that are not among the
    account's fills) and its side of each transfer to or from it, each with the bot's
    balance in ``bal``. Those are kept only when ``bal`` follows the account's balance,
    walking backwards from the current one; any other entry resets the chain to its ``bal``.
    """
    by_ccy: dict[str, list[Bill]] = defaultdict(list)
    for b in bills:
        by_ccy[b.ccy].append(b)
    kept: list[Bill] = []
    opening: dict[str, float] = {}
    for ccy, items in by_ccy.items():
        running = cash_now.get(ccy, 0.0)
        for b in sorted(items, key=_key, reverse=True):
            maybe_bot = b.type == BILL_STRATEGY or (
                b.type == BILL_TRADE and b.ord_id not in fill_orders
            )
            if maybe_bot and abs(b.bal - running) > _tol(running):
                continue
            kept.append(b)
            running = b.bal - b.bal_chg
        opening[ccy] = running if abs(running) > _tol(0) else 0.0
    kept.sort(key=_key)
    return kept, opening


@dataclass(frozen=True)
class Flow:
    ts: int
    ccy: str
    qty: float
    value: float  # USDC, positive = in
    estimated: bool


def flows(bills: Iterable[Bill], prices: PriceBook) -> list[Flow]:
    """Money moved between the trading account and funding (deposits arrive in funding)."""
    out = []
    for b in bills:
        if b.type != BILL_TRANSFER or not b.bal_chg:
            continue
        px = prices.at(b.ccy, b.ts)
        out.append(Flow(b.ts, b.ccy, b.bal_chg, b.bal_chg * (px or 0.0), px is None))
    return out


@dataclass(frozen=True)
class DayValue:
    day: date
    equity: float
    flow: float  # net money moved in that day
    estimated: bool  # rebuilt from the ledger (not a snapshot), or a price was missing


def _end_ms(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp() * 1000) + DAY_MS


def bot_value_on(bots: Iterable[Bot], day: date) -> float:
    end = _end_ms(day)
    return sum(
        b.investment for b in bots if b.c_time < end and (b.stop_time is None or b.stop_time >= end)
    )


def daily_values(
    account: Sequence[Bill],
    opening: dict[str, float],
    prices: PriceBook,
    bots: Sequence[Bot],
    snapshots: Sequence[tuple[int, float, float]],
    today: date,
    *,
    live_equity: float | None = None,
) -> list[DayValue]:
    """Value of the account at the end of each day, from the first entry to ``today``.

    ``account`` and ``opening`` come from :func:`account_bills`. A snapshot taken in the last
    hour of a day replaces the rebuilt value; any earlier one still gives the bots' value.
    ``live_equity`` is the value right now and becomes today's.
    """
    starts = [day_of(b.ts) for b in account[:1]] + [day_of(s[0]) for s in snapshots[:1]]
    if not starts:
        return []
    first = min(starts)
    eod: dict[str, dict[date, float]] = defaultdict(dict)
    for b in account:
        eod[b.ccy][day_of(b.ts)] = b.bal
    flow_by_day: dict[date, float] = defaultdict(float)
    flow_est: set[date] = set()
    for f in flows(account, prices):
        flow_by_day[day_of(f.ts)] += f.value
        if f.estimated:
            flow_est.add(day_of(f.ts))
    last_snap: dict[date, tuple[int, float, float]] = {}
    for shot in snapshots:
        last_snap[day_of(shot[0])] = shot

    ccys = set(opening) | set(eod)
    balance = {c: opening.get(c, 0.0) for c in ccys}
    out: list[DayValue] = []
    day = first
    while day <= today:
        estimated = day in flow_est
        cash = 0.0
        for ccy in ccys:
            if day in eod[ccy]:
                balance[ccy] = eod[ccy][day]
            qty = balance[ccy]
            if abs(qty) <= 1e-12:
                continue
            px = prices.now(ccy) if day == today else prices.on(ccy, day)
            if px is None:
                estimated = True
                continue
            cash += qty * px
        snap = last_snap.get(day)
        if snap and snap[0] >= _end_ms(day) - 3_600_000:
            equity, estimated = snap[1], False
        elif snap:
            equity, estimated = cash + snap[2], True
        else:
            equity, estimated = cash + bot_value_on(bots, day), True
        if day == today and live_equity is not None:
            equity, estimated = live_equity, False
        out.append(DayValue(day, equity, flow_by_day.get(day, 0.0), estimated))
        day += timedelta(days=1)
    return out


@dataclass(frozen=True)
class DayReturn:
    day: date
    equity: float
    flow: float
    pnl: float
    ret: float  # fraction
    index: float  # growth of 1 USDC since the start of the period
    drawdown: float  # fraction below the best index so far (≤ 0)
    estimated: bool


@dataclass(frozen=True)
class MonthRow:
    month: str  # YYYY-MM
    pnl: float
    flow: float
    twr: float
    end_equity: float


@dataclass(frozen=True)
class Performance:
    start: date
    end: date
    start_equity: float
    end_equity: float
    deposits: float
    withdrawals: float
    profit: float
    twr: float
    max_drawdown: float
    best: DayReturn | None
    worst: DayReturn | None
    days: list[DayReturn]
    months: list[MonthRow]
    estimated: bool

    @property
    def net_flow(self) -> float:
        return self.deposits + self.withdrawals

    @property
    def profit_pct_of_capital(self) -> float:
        """Profit over the money at work: starting value plus what was added."""
        base = self.start_equity + max(self.deposits, 0.0)
        return self.profit / base if base > 0 else 0.0


def day_return(prev: float, equity: float, flow: float) -> float:
    base = prev + flow
    return (equity - prev - flow) / base if base > 1e-9 else 0.0


def performance(values: Sequence[DayValue], start: date, end: date) -> Performance | None:
    """Profit and returns between the end of the day before ``start`` and the end of ``end``."""
    inside = [v for v in values if start <= v.day <= end]
    if not inside:
        return None
    before = [v for v in values if v.day < start]
    prev = before[-1].equity if before else 0.0
    start_equity = prev
    index = peak = 1.0
    max_dd = 0.0
    days: list[DayReturn] = []
    for v in inside:
        pnl = v.equity - prev - v.flow
        r = day_return(prev, v.equity, v.flow)
        index *= 1 + r
        peak = max(peak, index)
        dd = index / peak - 1
        max_dd = min(max_dd, dd)
        days.append(DayReturn(v.day, v.equity, v.flow, pnl, r, index, dd, v.estimated))
        prev = v.equity
    deposits = sum(v.flow for v in inside if v.flow > 0)
    withdrawals = sum(v.flow for v in inside if v.flow < 0)
    end_equity = inside[-1].equity
    active = [d for d in days if abs(d.pnl) > 1e-9]
    return Performance(
        start=inside[0].day,
        end=inside[-1].day,
        start_equity=start_equity,
        end_equity=end_equity,
        deposits=deposits,
        withdrawals=withdrawals,
        profit=end_equity - start_equity - deposits - withdrawals,
        twr=index - 1,
        max_drawdown=max_dd,
        best=max(active, key=lambda d: d.pnl) if active else None,
        worst=min(active, key=lambda d: d.pnl) if active else None,
        days=days,
        months=_months(days),
        estimated=any(v.estimated for v in inside[:-1]),
    )


def _months(days: Sequence[DayReturn]) -> list[MonthRow]:
    groups: dict[str, list[DayReturn]] = defaultdict(list)
    for d in days:
        groups[d.day.strftime("%Y-%m")].append(d)
    rows = []
    for month, items in sorted(groups.items()):
        growth = 1.0
        for d in items:
            growth *= 1 + d.ret
        rows.append(
            MonthRow(
                month,
                sum(d.pnl for d in items),
                sum(d.flow for d in items),
                growth - 1,
                items[-1].equity,
            )
        )
    return rows


PERIODS = {
    "7d": "7 días",
    "30d": "30 días",
    "90d": "90 días",
    "mes": "Este mes",
    "ytd": "Este año",
    "1a": "12 meses",
    "todo": "Todo",
}


def period_range(key: str, today: date, first: date) -> tuple[date, date]:
    if key == "7d":
        start = today - timedelta(days=6)
    elif key == "30d":
        start = today - timedelta(days=29)
    elif key == "90d":
        start = today - timedelta(days=89)
    elif key == "mes":
        start = today.replace(day=1)
    elif key == "ytd":
        start = today.replace(month=1, day=1)
    elif key == "1a":
        start = today - timedelta(days=364)
    else:
        start = first
    return max(start, first), today
