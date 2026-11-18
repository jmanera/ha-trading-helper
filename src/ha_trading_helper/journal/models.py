"""Typed views of the private OKX v5 records the journal keeps.

Times are kept as epoch milliseconds, as OKX sends them. Fees are signed the OKX way:
negative = charged, positive = rebate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

# Bill types (``type`` of /account/bills*) the journal tells apart.
BILL_TRANSFER = 1  # between the trading account and funding (deposits arrive there)
BILL_TRADE = 2
BILL_STRATEGY = 12  # into or out of a trading bot
BILL_CONVERT = 27
BILL_EASY_CONVERT = 28  # dust conversion
BILL_SIMPLE_TRADE = 30
CONVERSIONS = {BILL_CONVERT, BILL_EASY_CONVERT, BILL_SIMPLE_TRADE}


def num(value: Any) -> float:
    try:
        return float(value) if value not in (None, "") else 0.0
    except (TypeError, ValueError):
        return 0.0


def opt(value: Any) -> float | None:
    return float(value) if value not in (None, "") else None


def ms(value: Any) -> int:
    return int(value) if value not in (None, "") else 0


def day_of(ts_ms: int) -> date:
    return datetime.fromtimestamp(ts_ms / 1000, tz=UTC).date()


def split_inst(inst_id: str) -> tuple[str, str]:
    base, _, quote = inst_id.partition("-")
    return base, quote


@dataclass(frozen=True)
class Fill:
    bill_id: str
    trade_id: str
    ord_id: str
    inst_id: str
    side: str  # buy | sell
    px: float
    sz: float  # base currency
    fee: float
    fee_ccy: str
    fee_rate: float
    exec_type: str  # M maker | T taker
    ts: int

    @property
    def base(self) -> str:
        return split_inst(self.inst_id)[0]

    @property
    def quote(self) -> str:
        return split_inst(self.inst_id)[1]

    @classmethod
    def from_api(cls, item: dict[str, Any]) -> Fill:
        return cls(
            bill_id=str(item["billId"]),
            trade_id=str(item.get("tradeId", "")),
            ord_id=str(item.get("ordId", "")),
            inst_id=item["instId"],
            side=item["side"],
            px=num(item.get("fillPx")),
            sz=num(item.get("fillSz")),
            fee=num(item.get("fee")),
            fee_ccy=item.get("feeCcy", ""),
            fee_rate=num(item.get("feeRate")),
            exec_type=item.get("execType", ""),
            ts=ms(item.get("ts") or item.get("fillTime")),
        )


@dataclass(frozen=True)
class Bill:
    bill_id: str
    ts: int
    ccy: str
    type: int
    sub_type: int
    bal_chg: float
    bal: float
    px: float | None
    inst_id: str
    ord_id: str
    trade_id: str
    notes: str

    @classmethod
    def from_api(cls, item: dict[str, Any]) -> Bill:
        return cls(
            bill_id=str(item["billId"]),
            ts=ms(item.get("ts")),
            ccy=item.get("ccy", ""),
            type=int(item.get("type") or 0),
            sub_type=int(item.get("subType") or 0),
            bal_chg=num(item.get("balChg")),
            bal=num(item.get("bal")),
            px=opt(item.get("px")),
            inst_id=item.get("instId", ""),
            ord_id=str(item.get("ordId", "")),
            trade_id=str(item.get("tradeId", "")),
            notes=item.get("notes", ""),
        )


@dataclass(frozen=True)
class Order:
    ord_id: str
    inst_id: str
    side: str
    ord_type: str  # market, limit, post_only, ...
    state: str
    px: float | None
    sz: float
    avg_px: float | None
    filled: float
    fee: float
    fee_ccy: str
    source: str
    c_time: int
    u_time: int

    @classmethod
    def from_api(cls, item: dict[str, Any]) -> Order:
        return cls(
            ord_id=str(item["ordId"]),
            inst_id=item["instId"],
            side=item.get("side", ""),
            ord_type=item.get("ordType", ""),
            state=item.get("state", ""),
            px=opt(item.get("px")),
            sz=num(item.get("sz")),
            avg_px=opt(item.get("avgPx")),
            filled=num(item.get("accFillSz")),
            fee=num(item.get("fee")),
            fee_ccy=item.get("feeCcy", ""),
            source=str(item.get("source", "")),
            c_time=ms(item.get("cTime")),
            u_time=ms(item.get("uTime")),
        )


@dataclass(frozen=True)
class OpenOrder:
    """A resting order (``kind="order"``) or a conditional/stop one (``kind="algo"``)."""

    id: str
    kind: str
    inst_id: str
    side: str
    ord_type: str
    px: float | None  # limit price; None = market
    sz: float
    filled: float
    trigger_px: float | None
    c_time: int

    @classmethod
    def from_order(cls, item: dict[str, Any]) -> OpenOrder:
        return cls(
            id=str(item["ordId"]),
            kind="order",
            inst_id=item["instId"],
            side=item.get("side", ""),
            ord_type=item.get("ordType", ""),
            px=opt(item.get("px")),
            sz=num(item.get("sz")),
            filled=num(item.get("accFillSz")),
            trigger_px=None,
            c_time=ms(item.get("cTime")),
        )

    @classmethod
    def from_algo(cls, item: dict[str, Any]) -> OpenOrder:
        trigger = (
            opt(item.get("slTriggerPx"))
            or opt(item.get("tpTriggerPx"))
            or opt(item.get("triggerPx"))
            or opt(item.get("moveTriggerPx"))
        )
        limit = opt(item.get("slOrdPx")) or opt(item.get("tpOrdPx")) or opt(item.get("orderPx"))
        return cls(
            id=str(item["algoId"]),
            kind="algo",
            inst_id=item["instId"],
            side=item.get("side", ""),
            ord_type=item.get("ordType", ""),
            px=limit if limit and limit > 0 else None,
            sz=num(item.get("sz")),
            filled=num(item.get("actualSz")),
            trigger_px=trigger,
            c_time=ms(item.get("cTime")),
        )


@dataclass(frozen=True)
class Bot:
    """A trading bot (grid). Its trades never show up as account fills."""

    algo_id: str
    algo_type: str
    inst_id: str
    state: str  # running, stopped, ...
    investment: float  # in the quote currency
    total_pnl: float
    grid_profit: float
    float_profit: float
    c_time: int
    stop_time: int | None

    @property
    def running(self) -> bool:
        return self.stop_time is None

    @classmethod
    def from_api(cls, item: dict[str, Any]) -> Bot:
        grid, floating = num(item.get("gridProfit")), num(item.get("floatProfit"))
        total = opt(item.get("totalPnl"))
        state = item.get("state", "")
        stopped = state not in {"starting", "running", "pending_signal", "no_close_position"}
        return cls(
            algo_id=str(item["algoId"]),
            algo_type=item.get("algoOrdType", "grid"),
            inst_id=item["instId"],
            state=state,
            investment=num(item.get("investment")),
            total_pnl=total if total is not None else grid + floating,
            grid_profit=grid,
            float_profit=floating,
            c_time=ms(item.get("cTime")),
            stop_time=(ms(item.get("uTime")) or None) if stopped else None,
        )


@dataclass(frozen=True)
class Balance:
    ccy: str
    eq: float  # everything held, bots included
    cash_bal: float  # held directly in the trading account (what bills add up to)
    stgy_eq: float  # held by bots
    eq_usd: float

    @classmethod
    def from_api(cls, item: dict[str, Any]) -> Balance:
        return cls(
            ccy=item["ccy"],
            eq=num(item.get("eq")),
            cash_bal=num(item.get("cashBal")),
            stgy_eq=num(item.get("stgyEq")),
            eq_usd=num(item.get("eqUsd")),
        )
