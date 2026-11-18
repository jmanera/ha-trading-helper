"""From fills and ledger entries to positions and realised profit, FIFO, fees included.

Every change of the trading account becomes an :class:`Event` that gives one currency and
receives another. Each currency other than USDC is held as FIFO lots (first in, first out,
the method Spanish tax rules use for crypto), so each sale knows exactly which purchases it
closes and what they cost:

- a lot costs what was given for it in USDC, so the purchase fee is inside its cost when
  OKX charges it in the received currency (the usual case: buy fee in base, sell fee in
  quote);
- a sale yields what was received in USDC, net of its fee;
- profit = proceeds - cost. ``fee`` adds up both fees so the gross figure can be shown.

Money moved in from funding is bought at the market price of that moment, money moved out
is sold at market: from the account's point of view, that is what they are.
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Iterable
from dataclasses import dataclass, field, replace

from ha_trading_helper.journal.config import REFERENCE
from ha_trading_helper.journal.models import (
    BILL_STRATEGY,
    BILL_TRADE,
    BILL_TRANSFER,
    CONVERSIONS,
    Bill,
    Fill,
)
from ha_trading_helper.journal.prices import PriceBook

DUST = 1e-12

# Event kinds, as the journal names them.
KIND_LABELS = {
    "trade": "Operación",
    "convert": "Conversión",
    "transfer_in": "Entrada desde financiación",
    "transfer_out": "Salida a financiación",
    "bot_in": "Vuelve de un bot",
    "bot_out": "Pasa a un bot",
    "opening": "Saldo inicial",
    "other": "Ajuste de OKX",
    "fee": "Comisión en otra moneda",
}


@dataclass(frozen=True)
class Event:
    ts: int
    kind: str
    received: str | None
    received_qty: float
    given: str | None
    given_qty: float
    value: float  # USDC value of what was given (= cost of what was received)
    proceeds: float  # USDC value of what was received (= proceeds of what was given)
    fee: float = 0.0  # USDC, positive = paid
    estimated: bool = False  # value from a daily close, not from the trade itself
    inst_id: str = ""
    ord_id: str = ""
    side: str = ""
    exec_type: str = ""
    fee_rate: float = 0.0
    px: float = 0.0


@dataclass
class Lot:
    qty: float
    cost: float
    fee: float
    ts: int
    estimated: bool
    kind: str = "trade"
    exec_type: str = ""
    fee_rate: float = 0.0
    ord_id: str = ""

    @property
    def price(self) -> float:
        return self.cost / self.qty if self.qty > DUST else 0.0


@dataclass(frozen=True)
class Disposal:
    ts: int
    ccy: str
    qty: float
    proceeds: float
    cost: float
    fee: float  # sale fee + the share of purchase fees of the lots it closes
    hold_ms: float  # quantity-weighted time in position
    kind: str
    ord_id: str
    inst_id: str
    unknown_qty: float  # sold without a known purchase (before the data starts)
    estimated: bool

    @property
    def pnl(self) -> float:
        return self.proceeds - self.cost


@dataclass
class Holding:
    lots: deque[Lot] = field(default_factory=deque)

    @property
    def qty(self) -> float:
        return sum(lot.qty for lot in self.lots)

    @property
    def cost(self) -> float:
        return sum(lot.cost for lot in self.lots)

    @property
    def fee(self) -> float:
        return sum(lot.fee for lot in self.lots)

    @property
    def estimated(self) -> bool:
        return any(lot.estimated for lot in self.lots)


@dataclass
class Book:
    holdings: dict[str, Holding]
    disposals: list[Disposal]
    events: list[Event]


def _value(prices: PriceBook, ccy: str, qty: float, ts: int) -> tuple[float, bool]:
    if ccy == REFERENCE:
        return qty, False
    px = prices.at(ccy, ts)
    return (qty * px, False) if px is not None else (0.0, True)


def fill_events(f: Fill, prices: PriceBook) -> list[Event]:
    base, quote = f.base, f.quote
    base_chg = f.sz if f.side == "buy" else -f.sz
    quote_chg = -f.sz * f.px if f.side == "buy" else f.sz * f.px
    extra: list[Event] = []
    if f.fee_ccy == base:
        base_chg += f.fee
    elif f.fee_ccy == quote:
        quote_chg += f.fee
    elif f.fee:
        cost, est = _value(prices, f.fee_ccy, abs(f.fee), f.ts)
        extra.append(
            Event(
                f.ts,
                "fee",
                None,
                0.0,
                f.fee_ccy,
                abs(f.fee),
                0.0,
                0.0,
                fee=cost,
                estimated=est,
                inst_id=f.inst_id,
                ord_id=f.ord_id,
            )
        )
    fee_usdc, fee_est = _value(prices, f.fee_ccy, -f.fee, f.ts) if f.fee else (0.0, False)
    (got, got_qty), (gave, gave_qty) = (
        ((base, base_chg), (quote, -quote_chg))
        if f.side == "buy"
        else ((quote, quote_chg), (base, -base_chg))
    )
    value, est1 = _value(prices, gave, gave_qty, f.ts)
    proceeds, est2 = _value(prices, got, got_qty, f.ts)
    return [
        Event(
            f.ts,
            "trade",
            got,
            got_qty,
            gave,
            gave_qty,
            value or proceeds,
            proceeds or value,
            fee=0.0 if extra else fee_usdc,
            estimated=(est1 and est2) or fee_est,
            inst_id=f.inst_id,
            ord_id=f.ord_id,
            side=f.side,
            exec_type=f.exec_type,
            fee_rate=abs(f.fee_rate),
            px=f.px,
        ),
        *extra,
    ]


def bill_events(bills: Iterable[Bill], prices: PriceBook) -> list[Event]:
    """Events of the ledger entries that are not fills (those come from :func:`fill_events`).

    ``bills`` must hold the trading account side only (see ``equity.account_bills``).
    """
    events: list[Event] = []
    pairs: dict[tuple[int, str], list[Bill]] = defaultdict(list)
    for b in bills:
        if b.type == BILL_TRADE or abs(b.bal_chg) < DUST:
            continue
        if b.type in CONVERSIONS and b.trade_id not in {"", "0"}:
            pairs[(b.type, b.trade_id)].append(b)
            continue
        qty = abs(b.bal_chg)
        value, est = _value(prices, b.ccy, qty, b.ts)
        incoming = b.bal_chg > 0
        if b.type == BILL_TRANSFER:
            kind = "transfer_in" if incoming else "transfer_out"
        elif b.type == BILL_STRATEGY:
            kind = "bot_in" if incoming else "bot_out"
        else:
            kind = "other"
        if incoming:
            # A credit that is not a transfer (airdrop, reward) cost nothing.
            cost = 0.0 if kind == "other" else value
            events.append(
                Event(
                    b.ts, kind, b.ccy, qty, None, 0.0, cost, value, estimated=est, inst_id=b.inst_id
                )
            )
        else:
            events.append(
                Event(
                    b.ts,
                    kind,
                    None,
                    0.0,
                    b.ccy,
                    qty,
                    value,
                    value,
                    estimated=est,
                    inst_id=b.inst_id,
                )
            )
    for legs in pairs.values():
        got = [b for b in legs if b.bal_chg > 0]
        gave = [b for b in legs if b.bal_chg < 0]
        if not got or not gave:
            # One-legged conversion: treat each leg on its own, at market.
            events += bill_events([replace(b, type=0) for b in legs], prices)
            continue
        g, s = got[0], gave[0]
        value, est1 = _value(prices, s.ccy, -s.bal_chg, s.ts)
        proceeds, est2 = _value(prices, g.ccy, g.bal_chg, g.ts)
        events.append(
            Event(
                g.ts,
                "convert",
                g.ccy,
                g.bal_chg,
                s.ccy,
                -s.bal_chg,
                value or proceeds,
                proceeds or value,
                estimated=est1 and est2,
                inst_id=g.inst_id or s.inst_id,
                px=g.px or 0.0,
            )
        )
    return events


def opening_events(opening: dict[str, float], ts: int, prices: PriceBook) -> list[Event]:
    """What was already held when the data starts, bought at that day's price (estimated)."""
    events = []
    for ccy, qty in sorted(opening.items()):
        if ccy == REFERENCE or qty <= DUST:
            continue
        value, _ = _value(prices, ccy, qty, ts)
        events.append(Event(ts, "opening", ccy, qty, None, 0.0, value, value, estimated=True))
    return events


def run_fifo(events: Iterable[Event]) -> Book:
    ordered = sorted(events, key=lambda e: (e.ts, e.kind != "opening"))
    holdings: dict[str, Holding] = defaultdict(Holding)
    disposals: list[Disposal] = []
    for e in ordered:
        if e.given and e.given != REFERENCE and e.given_qty > DUST:
            disposals.append(_dispose(holdings[e.given], e, e.given, e.fee))
        if e.received and e.received != REFERENCE and e.received_qty > DUST:
            # The fee goes with the sale when the event is also one (cross pairs).
            lot_fee = e.fee if not (e.given and e.given != REFERENCE) else 0.0
            holdings[e.received].lots.append(
                Lot(
                    e.received_qty,
                    e.value,
                    lot_fee,
                    e.ts,
                    e.estimated or e.kind == "opening",
                    e.kind,
                    e.exec_type,
                    e.fee_rate,
                    e.ord_id,
                )
            )
    return Book(
        holdings={c: h for c, h in holdings.items() if h.lots},
        disposals=disposals,
        events=ordered,
    )


def _dispose(holding: Holding, e: Event, ccy: str, sale_fee: float) -> Disposal:
    left = e.given_qty
    cost = fee = hold = 0.0
    estimated = e.estimated
    while left > DUST and holding.lots:
        lot = holding.lots[0]
        take = min(left, lot.qty)
        share = take / lot.qty
        cost += lot.cost * share
        fee += lot.fee * share
        hold += take * (e.ts - lot.ts)
        estimated = estimated or lot.estimated
        lot.qty -= take
        lot.cost -= lot.cost * share
        lot.fee -= lot.fee * share
        left -= take
        if lot.qty <= DUST * max(1.0, take):
            holding.lots.popleft()
    unknown = max(left, 0.0) if left > DUST else 0.0
    sold_known = e.given_qty - unknown
    if unknown:
        # Sold more than the data shows was bought: no profit claimed on that part.
        cost += e.proceeds * unknown / e.given_qty
        estimated = True
    if e.kind not in {"trade", "convert"}:
        sale_fee = 0.0
    return Disposal(
        ts=e.ts,
        ccy=ccy,
        qty=e.given_qty,
        proceeds=e.proceeds,
        cost=cost,
        fee=fee + sale_fee,
        hold_ms=hold / sold_known if sold_known > DUST else 0.0,
        kind=e.kind,
        ord_id=e.ord_id,
        inst_id=e.inst_id,
        unknown_qty=unknown,
        estimated=estimated,
    )


def build_book(
    fills: Iterable[Fill],
    bills: Iterable[Bill],
    prices: PriceBook,
    *,
    opening: dict[str, float] | None = None,
    opening_ts: int = 0,
) -> Book:
    events = [e for f in fills for e in fill_events(f, prices)]
    events += bill_events(bills, prices)
    if opening:
        events += opening_events(opening, opening_ts, prices)
    return run_fifo(events)
