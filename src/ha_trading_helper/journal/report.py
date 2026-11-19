"""What each page shows, computed from the stored data (no I/O besides reading the store)."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from ha_trading_helper.journal.config import REFERENCE
from ha_trading_helper.journal.equity import (
    DayValue,
    Performance,
    account_bills,
    daily_values,
    performance,
    period_range,
)
from ha_trading_helper.journal.ledger import KIND_LABELS, Book, Disposal, Event, Lot, build_book
from ha_trading_helper.journal.models import Balance, Bot, Fill, OpenOrder, Order, day_of
from ha_trading_helper.journal.prices import PriceBook
from ha_trading_helper.journal.store import Store

DUST_VALUE = 0.01  # USDC; smaller holdings are leftovers of sales, not positions
DEFAULT_TAKER = 0.001


def _ms(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp() * 1000)


@dataclass
class Data:
    """Everything the pages need, loaded once per request."""

    now: datetime
    balances: list[Balance]
    prices: PriceBook
    fills: list[Fill]
    book: Book
    values: list[DayValue]
    bots: list[Bot]
    open_orders: list[OpenOrder]
    orders: dict[str, Order]
    notes: dict[str, tuple[str, str]]
    fee_rates: dict[str, list[float]]
    fee_groups: dict[str, str]
    last_sync: dict[str, Any]
    snapshot_equity: float | None

    @property
    def today(self) -> date:
        return self.now.date()

    @classmethod
    def load(cls, store: Store, now: datetime | None = None) -> Data:
        now = now or datetime.now(UTC)
        balances = store.balances()
        fills_ = store.fills()
        bills_ = store.bills()
        prices = PriceBook(closes=store.prices(), live=store.get_json("live_prices", {}))
        prices.learn(fills_, bills_)
        account, opening = account_bills(
            bills_, {b.ccy: b.cash_bal for b in balances}, {f.ord_id for f in fills_}
        )
        opening_ts = min([b.ts for b in account] + [f.ts for f in fills_], default=0)
        book = build_book(fills_, account, prices, opening=opening, opening_ts=opening_ts)
        snaps = store.snapshots()
        bots = store.bots()
        live = snaps[-1][1] if snaps and day_of(snaps[-1][0]) == now.date() else None
        values = daily_values(account, opening, prices, bots, snaps, now.date(), live_equity=live)
        return cls(
            now=now,
            balances=balances,
            prices=prices,
            fills=fills_,
            book=book,
            values=values,
            bots=bots,
            open_orders=store.open_orders(),
            orders=store.orders(),
            notes=store.notes(),
            fee_rates=store.get_json("fee_rates", {}),
            fee_groups=store.get_json("fee_groups", {}),
            last_sync=store.get_json("last_sync", {}),
            snapshot_equity=live,
        )

    def rates(self, inst_id: str) -> tuple[float, float]:
        """(maker, taker) fee rates of an instrument for this account's tier."""
        group = self.fee_groups.get(inst_id, "1")
        maker, taker = self.fee_rates.get(group, [DEFAULT_TAKER * 0.8, DEFAULT_TAKER])
        return maker, taker

    def px(self, ccy: str) -> float | None:
        return self.prices.now(ccy)


# Today ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class AssetLine:
    ccy: str
    qty: float
    in_bots: float
    px: float | None
    value: float
    share: float
    change_today: float | None  # value change from yesterday's close at today's quantity


@dataclass(frozen=True)
class Overview:
    equity: float
    pnl_today: float
    pct_today: float
    flow_today: float
    yesterday: float
    assets: list[AssetLine]
    dust: int
    profit_all: float
    deposits_all: float
    realized_all: float
    unrealized: float
    fees_all: float
    bots_pnl: float
    last30: list[DayValue]


def overview(d: Data) -> Overview:
    values = d.values
    today = values[-1] if values else DayValue(d.today, 0.0, 0.0, True)
    prev = values[-2].equity if len(values) > 1 else 0.0
    pnl = today.equity - prev - today.flow
    base = prev + today.flow
    lines = []
    total = sum(_value(d, b) for b in d.balances)
    dust = 0
    for b in d.balances:
        value = _value(d, b)
        if value < DUST_VALUE and b.ccy != REFERENCE:
            dust += 1
            continue
        px = d.px(b.ccy)
        close = d.prices.on(b.ccy, d.today - timedelta(days=1))
        change = b.eq * (px - close) if px is not None and close is not None else None
        lines.append(
            AssetLine(b.ccy, b.eq, b.stgy_eq, px, value, value / total if total else 0.0, change)
        )
    lines.sort(key=lambda a: -a.value)
    all_perf = performance(values, values[0].day, d.today) if values else None
    positions_ = positions(d)
    return Overview(
        equity=today.equity,
        pnl_today=pnl,
        pct_today=pnl / base if base > 0 else 0.0,
        flow_today=today.flow,
        yesterday=prev,
        assets=lines,
        dust=dust,
        profit_all=all_perf.profit if all_perf else 0.0,
        deposits_all=all_perf.net_flow if all_perf else 0.0,
        realized_all=sum(x.pnl for x in d.book.disposals),
        unrealized=sum(p.unrealized for p in positions_.rows),
        fees_all=sum(_fill_fee(d, f) for f in d.fills),
        bots_pnl=sum(b.total_pnl for b in d.bots),
        last30=values[-30:],
    )


def _value(d: Data, b: Balance) -> float:
    px = d.px(b.ccy)
    if px is None:
        return b.eq_usd
    return b.eq * px


def _fill_fee(d: Data, f: Fill) -> float:
    if not f.fee:
        return 0.0
    px = d.prices.at(f.fee_ccy, f.ts) if f.fee_ccy != REFERENCE else 1.0
    return -f.fee * (px or 0.0)


# Positions -----------------------------------------------------------------------------


@dataclass(frozen=True)
class LotLine:
    ts: int
    qty: float
    price: float
    cost: float
    fee: float
    kind: str
    exec_type: str
    fee_rate: float
    estimated: bool


@dataclass(frozen=True)
class Position:
    ccy: str
    inst_id: str
    qty: float
    avg_cost: float
    px: float | None
    value: float
    cost: float
    buy_fees: float
    exit_fee: float  # estimated taker fee to sell everything now
    taker_rate: float
    maker_rate: float
    lots: list[LotLine]
    orders: list[OpenOrder]
    estimated: bool
    first_ts: int

    @property
    def unrealized(self) -> float:
        """After the fees already paid; the selling fee is not taken yet."""
        return self.value - self.cost

    @property
    def unrealized_net(self) -> float:
        return self.unrealized - self.exit_fee

    @property
    def unrealized_gross(self) -> float:
        return self.unrealized + self.buy_fees

    @property
    def pct(self) -> float:
        return self.unrealized_net / self.cost if self.cost > 0 else 0.0


@dataclass(frozen=True)
class BotLine:
    bot: Bot
    value: float
    pct: float


@dataclass(frozen=True)
class Positions:
    rows: list[Position]
    bots: list[BotLine]
    dust: int
    orphan_orders: list[OpenOrder] = field(default_factory=list)

    @property
    def cost(self) -> float:
        return sum(p.cost for p in self.rows) + sum(b.bot.investment for b in self.bots)

    @property
    def value(self) -> float:
        return sum(p.value for p in self.rows) + sum(b.value for b in self.bots)

    @property
    def unrealized_net(self) -> float:
        return sum(p.unrealized_net for p in self.rows) + sum(b.bot.total_pnl for b in self.bots)


def positions(d: Data) -> Positions:
    rows: list[Position] = []
    dust = 0
    used_orders: set[str] = set()
    held = {b.ccy: b.cash_bal for b in d.balances}
    for ccy, h in sorted(d.book.holdings.items()):
        px = d.px(ccy) or d.prices.on(ccy, d.today)
        qty = h.qty
        value = qty * px if px is not None else 0.0
        # FIFO leftovers of rounding that OKX no longer shows are not positions.
        if held.get(ccy, 0.0) <= 0 or (px is not None and value < DUST_VALUE):
            dust += 1
            continue
        inst = f"{ccy}-{REFERENCE}"
        maker, taker = d.rates(inst)
        orders_ = [o for o in d.open_orders if o.inst_id.split("-")[0] == ccy]
        used_orders |= {o.id for o in orders_}
        rows.append(
            Position(
                ccy=ccy,
                inst_id=inst,
                qty=qty,
                avg_cost=h.cost / qty if qty else 0.0,
                px=px,
                value=value,
                cost=h.cost,
                buy_fees=h.fee,
                exit_fee=value * taker,
                taker_rate=taker,
                maker_rate=maker,
                lots=[_lot(lot) for lot in h.lots],
                orders=orders_,
                estimated=h.estimated,
                first_ts=h.lots[0].ts,
            )
        )
    rows.sort(key=lambda p: -p.value)
    bots = [
        BotLine(b, b.investment + b.total_pnl, b.total_pnl / b.investment if b.investment else 0.0)
        for b in d.bots
        if b.running
    ]
    orphans = [o for o in d.open_orders if o.id not in used_orders]
    return Positions(rows, bots, dust, orphans)


def _lot(lot: Lot) -> LotLine:
    return LotLine(
        lot.ts,
        lot.qty,
        lot.price,
        lot.cost,
        lot.fee,
        lot.kind,
        lot.exec_type,
        lot.fee_rate,
        lot.estimated,
    )


# Performance ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PeriodView:
    key: str
    start: date
    end: date
    perf: Performance | None
    realized: float
    fees: float
    maker_fees: float
    taker_fees: float
    trades: int
    volume: float


def period_view(
    d: Data, key: str, start: date | None = None, end: date | None = None
) -> PeriodView:
    first = d.values[0].day if d.values else d.today
    if start or end:
        key = "custom"
        s, e = max(start or first, first), min(end or d.today, d.today)
    else:
        s, e = period_range(key, d.today, first)
    perf = performance(d.values, s, e) if s <= e else None
    lo, hi = _ms(s), _ms(e + timedelta(days=1))
    fills_ = [f for f in d.fills if lo <= f.ts < hi]
    fee_m = sum(_fill_fee(d, f) for f in fills_ if f.exec_type == "M")
    fee_t = sum(_fill_fee(d, f) for f in fills_ if f.exec_type != "M")
    return PeriodView(
        key=key,
        start=s,
        end=e,
        perf=perf,
        realized=sum(x.pnl for x in d.book.disposals if lo <= x.ts < hi and _is_trade(x)),
        fees=fee_m + fee_t,
        maker_fees=fee_m,
        taker_fees=fee_t,
        trades=len({f.ord_id for f in fills_}),
        volume=sum(_fill_value(d, f) for f in fills_),
    )


def _is_trade(x: Disposal) -> bool:
    return x.kind in {"trade", "convert"}


def _fill_value(d: Data, f: Fill) -> float:
    if f.quote == REFERENCE:
        return f.sz * f.px
    px = d.prices.at(f.quote, f.ts)
    return f.sz * f.px * (px or 0.0)


# Assets --------------------------------------------------------------------------------


@dataclass
class AssetRow:
    ccy: str
    bought: float = 0.0
    sold: float = 0.0
    realized: float = 0.0
    unrealized: float = 0.0
    fees: float = 0.0
    orders: int = 0
    closed: int = 0
    wins: int = 0
    hold_ms: float = 0.0
    held_qty: float = 0.0
    first_ts: int = 0
    last_ts: int = 0
    is_bot: bool = False
    note: str = ""

    @property
    def total(self) -> float:
        return self.realized + self.unrealized

    @property
    def win_rate(self) -> float | None:
        return self.wins / self.closed if self.closed else None

    @property
    def return_pct(self) -> float | None:
        return self.total / self.bought if self.bought > 0 else None


def assets(d: Data) -> list[AssetRow]:
    rows: dict[str, AssetRow] = {}

    def row(ccy: str) -> AssetRow:
        if ccy not in rows:
            rows[ccy] = AssetRow(ccy)
        return rows[ccy]

    for e in d.book.events:
        if e.kind not in {"trade", "convert"}:
            continue
        for ccy, bought in ((e.received, True), (e.given, False)):
            if not ccy or ccy == REFERENCE:
                continue
            r = row(ccy)
            if bought:
                r.bought += e.value
            else:
                r.sold += e.proceeds
            r.first_ts = min(r.first_ts or e.ts, e.ts)
            r.last_ts = max(r.last_ts, e.ts)
    orders_by_ccy: dict[str, set[str]] = defaultdict(set)
    for f in d.fills:
        r = row(f.base)
        r.fees += _fill_fee(d, f)
        orders_by_ccy[f.base].add(f.ord_id)
    by_sale: dict[tuple[str, str], list[Disposal]] = defaultdict(list)
    for x in d.book.disposals:
        r = row(x.ccy)
        r.realized += x.pnl
        if _is_trade(x):
            by_sale[(x.ccy, x.ord_id or str(x.ts))].append(x)
    for (ccy, _), parts in by_sale.items():
        r = rows[ccy]
        r.closed += 1
        r.wins += sum(p.pnl for p in parts) > 0
        r.hold_ms += sum(p.hold_ms * p.qty for p in parts) / max(sum(p.qty for p in parts), 1e-18)
    for r in rows.values():
        r.orders = len(orders_by_ccy.get(r.ccy, set()))
        r.hold_ms = r.hold_ms / r.closed if r.closed else 0.0
    for p in positions(d).rows:
        r = row(p.ccy)
        r.unrealized = p.unrealized
        r.held_qty = p.qty
    for b in d.bots:
        r = AssetRow(
            f"Bot {b.inst_id}",
            bought=b.investment,
            unrealized=b.total_pnl,
            first_ts=b.c_time,
            last_ts=b.stop_time or 0,
            is_bot=True,
        )
        rows[r.ccy] = r
    for r in rows.values():
        r.note = d.notes.get(f"asset:{r.ccy}", ("", ""))[0]
    visible = [r for r in rows.values() if r.bought or r.sold or r.is_bot or r.held_qty]
    return sorted(visible, key=lambda r: r.total)


# Orders --------------------------------------------------------------------------------

ORDER_TYPES = {
    "market": "Mercado",
    "limit": "Límite",
    "post_only": "Solo maker",
    "fok": "FOK",
    "ioc": "IOC",
    "optimal_limit_ioc": "Mercado (IOC)",
    "conditional": "Stop / TP",
    "oco": "OCO",
    "trigger": "Disparo",
    "move_order_stop": "Stop dinámico",
    "twap": "TWAP",
    "iceberg": "Iceberg",
}
ORDER_STATES = {
    "live": "Abierta",
    "partially_filled": "Parcial",
    "filled": "Ejecutada",
    "canceled": "Cancelada",
    "mmp_canceled": "Cancelada",
}
STATE_FILTERS = {
    "ejecutadas": {"filled", "partially_filled"},
    "canceladas": {"canceled", "mmp_canceled"},
}
MAKER_TYPES = {"limit", "post_only"}  # resting orders; the rest take liquidity
HISTORY_LIMIT = 500


@dataclass(frozen=True)
class OpenOrderLine:
    order: OpenOrder
    ccy: str
    remaining: float
    px: float | None  # the price it executes at: limit, else trigger
    value: float | None  # USDC of what is left to fill
    market_px: float | None
    distance: float | None  # from the market price to px, as a fraction
    fee: float | None  # estimated, at maker for resting orders and taker otherwise
    fee_rate: float
    maker: bool
    has_position: bool

    @property
    def type_label(self) -> str:
        return ORDER_TYPES.get(self.order.ord_type, self.order.ord_type)

    @property
    def filled_pct(self) -> float:
        return self.order.filled / self.order.sz if self.order.sz else 0.0


@dataclass(frozen=True)
class OrderLine:
    order: Order
    ccy: str
    value: float  # USDC actually traded
    fee: float  # USDC, from the fills
    exec_types: set[str]
    pnl: float | None

    @property
    def type_label(self) -> str:
        return ORDER_TYPES.get(self.order.ord_type, self.order.ord_type)

    @property
    def state_label(self) -> str:
        return ORDER_STATES.get(self.order.state, self.order.state)

    @property
    def filled_pct(self) -> float:
        return self.order.filled / self.order.sz if self.order.sz else 0.0

    @property
    def liquidity(self) -> str:
        if self.exec_types == {"M"}:
            return "Maker"
        if self.exec_types == {"T"}:
            return "Taker"
        return "Mixta" if self.exec_types else ""


@dataclass(frozen=True)
class OrdersView:
    open: list[OpenOrderLine]
    history: list[OrderLine]
    total: int  # history rows matching the filters, before HISTORY_LIMIT
    ccys: list[str]

    @property
    def buying(self) -> float:
        return sum(o.value or 0.0 for o in self.open if o.order.side == "buy")

    @property
    def selling(self) -> float:
        return sum(o.value or 0.0 for o in self.open if o.order.side == "sell")


def orders_view(
    d: Data,
    *,
    ccy: str = "",
    state: str = "",
    start: date | None = None,
    end: date | None = None,
) -> OrdersView:
    held = {p.ccy for p in positions(d).rows}
    open_ = [_open_line(d, o, held) for o in d.open_orders]
    open_.sort(key=lambda o: (o.ccy, -o.order.c_time))
    fills_by_order: dict[str, list[Fill]] = defaultdict(list)
    for f in d.fills:
        fills_by_order[f.ord_id].append(f)
    pnl_by_order: dict[str, float] = defaultdict(float)
    for x in d.book.disposals:
        if x.ord_id:
            pnl_by_order[x.ord_id] += x.pnl
    lo = _ms(start) if start else None
    hi = _ms(end + timedelta(days=1)) if end else None
    states = STATE_FILTERS.get(state)
    history: list[OrderLine] = []
    for o in d.orders.values():
        base = o.inst_id.split("-")[0]
        if (
            (ccy and base != ccy)
            or (states and o.state not in states)
            or (lo is not None and o.c_time < lo)
            or (hi is not None and o.c_time >= hi)
        ):
            continue
        fills_ = fills_by_order.get(o.ord_id, [])
        history.append(
            OrderLine(
                order=o,
                ccy=base,
                value=sum(_fill_value(d, f) for f in fills_),
                fee=sum(_fill_fee(d, f) for f in fills_),
                exec_types={f.exec_type for f in fills_ if f.exec_type},
                pnl=pnl_by_order.get(o.ord_id) if o.side == "sell" and fills_ else None,
            )
        )
    history.sort(key=lambda r: -r.order.c_time)
    ccys = sorted({o.inst_id.split("-")[0] for o in d.orders.values()} | {o.ccy for o in open_})
    return OrdersView(open_, history[:HISTORY_LIMIT], len(history), ccys)


def _open_line(d: Data, o: OpenOrder, held: set[str]) -> OpenOrderLine:
    base, _, quote = o.inst_id.partition("-")
    remaining = max(o.sz - o.filled, 0.0)
    market = d.px(base)
    quote_px = 1.0 if quote == REFERENCE else d.px(quote)
    px = o.px or o.trigger_px
    # market_px is in USDC; the order's price is in its quote currency
    px_ref = px * quote_px if px is not None and quote_px is not None else None
    at = px_ref if px_ref is not None else market  # market orders: at today's price
    value = remaining * at if at is not None else None
    maker_rate, taker_rate = d.rates(o.inst_id)
    maker = o.kind == "order" and o.ord_type in MAKER_TYPES
    rate = maker_rate if maker else taker_rate
    return OpenOrderLine(
        order=o,
        ccy=base,
        remaining=remaining,
        px=px,
        value=value,
        market_px=market,
        distance=px_ref / market - 1 if px_ref is not None and market else None,
        fee=value * rate if value is not None else None,
        fee_rate=rate,
        maker=maker,
        has_position=base in held,
    )


# Journal -------------------------------------------------------------------------------


@dataclass
class JournalRow:
    ts: int
    kind: str
    label: str
    inst_id: str
    side: str
    ord_type: str
    qty: float
    price: float
    value: float
    fee: float
    fee_rate: float
    exec_types: set[str]
    pnl: float | None
    ref: str
    note: str
    tags: str
    estimated: bool
    ccy: str

    @property
    def liquidity(self) -> str:
        if self.exec_types == {"M"}:
            return "Maker"
        if self.exec_types == {"T"}:
            return "Taker"
        return "Mixta" if self.exec_types else ""


def journal(
    d: Data,
    *,
    ccy: str = "",
    start: date | None = None,
    end: date | None = None,
    kind: str = "",
    dust: bool = False,
) -> list[JournalRow]:
    """Orders and other movements, newest first. ``dust`` keeps conversions of leftovers."""
    pnl_by_order: dict[str, float] = defaultdict(float)
    for x in d.book.disposals:
        if x.ord_id:
            pnl_by_order[x.ord_id] += x.pnl
    grouped: dict[str, list[Event]] = defaultdict(list)
    rows: list[JournalRow] = []
    for e in d.book.events:
        if e.kind == "trade" and e.ord_id:
            grouped[e.ord_id].append(e)
        elif e.kind not in {"fee", "opening"}:
            rows.append(_event_row(d, e))
    for ord_id, events in grouped.items():
        rows.append(_order_row(d, ord_id, events, pnl_by_order.get(ord_id)))
    lo = _ms(start) if start else None
    hi = _ms(end + timedelta(days=1)) if end else None
    out = [
        r
        for r in rows
        if (not ccy or r.ccy == ccy)
        and (lo is None or r.ts >= lo)
        and (hi is None or r.ts < hi)
        and (not kind or r.kind == kind)
        and (dust or r.kind == "trade" or r.value >= DUST_VALUE)
    ]
    return sorted(out, key=lambda r: -r.ts)


def _order_row(d: Data, ord_id: str, events: list[Event], pnl: float | None) -> JournalRow:
    first = events[0]
    order = d.orders.get(ord_id)
    buy = first.side == "buy"
    sizes = [e.received_qty if buy else e.given_qty for e in events]
    qty = sum(sizes)
    value = sum(e.value if buy else e.proceeds for e in events)
    gross = sum(e.px * size for e, size in zip(events, sizes, strict=True))
    note, tags = d.notes.get(f"order:{ord_id}", ("", ""))
    return JournalRow(
        ts=first.ts,
        kind="trade",
        label="Compra" if buy else "Venta",
        inst_id=first.inst_id,
        side=first.side,
        ord_type=order.ord_type if order else "",
        qty=qty,
        price=gross / qty if qty else 0.0,
        value=value,
        fee=sum(e.fee for e in events),
        fee_rate=max(e.fee_rate for e in events),
        exec_types={e.exec_type for e in events if e.exec_type},
        pnl=None if buy else pnl,
        ref=f"order:{ord_id}",
        note=note,
        tags=tags,
        estimated=any(e.estimated for e in events),
        ccy=first.inst_id.split("-")[0],
    )


def _event_row(d: Data, e: Event) -> JournalRow:
    ccy = e.received if e.received and e.received != REFERENCE else (e.given or REFERENCE)
    qty = e.received_qty if ccy == e.received else e.given_qty
    ref = f"event:{e.kind}:{e.ts}:{ccy}"
    note, tags = d.notes.get(ref, ("", ""))
    pnl = None
    if e.given and e.given != REFERENCE and e.kind in {"convert", "transfer_out", "bot_out"}:
        pnl = sum(x.pnl for x in d.book.disposals if x.ts == e.ts and x.ccy == e.given)
    return JournalRow(
        ts=e.ts,
        kind=e.kind,
        label=KIND_LABELS.get(e.kind, e.kind),
        inst_id=e.inst_id,
        side="",
        ord_type="",
        qty=qty,
        price=e.value / qty if qty else 0.0,
        value=e.value,
        fee=e.fee,
        fee_rate=0.0,
        exec_types=set(),
        pnl=pnl,
        ref=ref,
        note=note,
        tags=tags,
        estimated=e.estimated,
        ccy=ccy,
    )
