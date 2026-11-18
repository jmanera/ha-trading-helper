"""A small account history whose figures can be checked by hand.

Day 1 (2026-11-01, UTC)
  10:00  100 USDC arrive from funding
  11:00  buy 1 SOL at 50, taker, fee 0.001 SOL       -> 0.999 SOL cost 50
Day 2
  12:00  buy 1 SOL at 40, maker, fee 0.0008 SOL      -> 0.9992 SOL cost 40
Day 3
  09:00  sell 1.5 SOL at 60, taker, fee 0.09 USDC    -> 89.91 USDC
  10:00  20 USDC go to a grid bot (plus the bot's own books: its side of the transfer
         and one of its trades, which must be ignored)
  11:00  9.91 USDC go back to funding
Now (day 4): 70 USDC and 0.4982 SOL in the account, the bot holds 10 USDC + 100 DOOD.

FIFO: the sale closes the 0.999 SOL of day 1 (cost 50) and 0.501 of the 0.9992 of day 2
(cost 40 * 0.501 / 0.9992). Proceeds 89.91, so profit = 89.91 - 50 - 20.0560... = 19.8539...
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from ha_trading_helper.journal.models import Balance, Bill, Bot, Fill

D1, D2, D3, D4 = date(2026, 11, 1), date(2026, 11, 2), date(2026, 11, 3), date(2026, 11, 4)
NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)


def ts(day: date, hour: int, minute: int = 0) -> int:
    return int(datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC).timestamp() * 1000)


_ids = iter(range(1000, 9999))


def bill(
    at: int,
    ccy: str,
    type_: int,
    chg: float,
    bal: float,
    *,
    sub: int = 1,
    px: float | None = None,
    inst: str = "",
    ord_id: str = "",
    trade: str = "",
) -> Bill:
    return Bill(str(next(_ids)), at, ccy, type_, sub, chg, bal, px, inst, ord_id, trade, "")


FILLS = [
    Fill("2001", "t1", "o1", "SOL-USDC", "buy", 50.0, 1.0, -0.001, "SOL", -0.001, "T", ts(D1, 11)),
    Fill(
        "2002", "t2", "o2", "SOL-USDC", "buy", 40.0, 1.0, -0.0008, "SOL", -0.0008, "M", ts(D2, 12)
    ),
    Fill("2003", "t3", "o3", "SOL-USDC", "sell", 60.0, 1.5, -0.09, "USDC", -0.001, "T", ts(D3, 9)),
]

BILLS = [
    bill(ts(D1, 10), "USDC", 1, 100.0, 100.0, sub=11),
    bill(ts(D1, 11), "USDC", 2, -50.0, 50.0, px=50.0, inst="SOL-USDC", ord_id="o1"),
    bill(ts(D1, 11), "SOL", 2, 0.999, 0.999, px=50.0, inst="SOL-USDC", ord_id="o1"),
    bill(ts(D2, 12), "USDC", 2, -40.0, 10.0, px=40.0, inst="SOL-USDC", ord_id="o2"),
    bill(ts(D2, 12), "SOL", 2, 0.9992, 1.9982, px=40.0, inst="SOL-USDC", ord_id="o2"),
    bill(ts(D3, 9), "USDC", 2, 89.91, 99.91, px=60.0, inst="SOL-USDC", ord_id="o3"),
    bill(ts(D3, 9), "SOL", 2, -1.5, 0.4982, px=60.0, inst="SOL-USDC", ord_id="o3"),
    # transfer to the bot: the account side, then the bot's side (same time)
    bill(ts(D3, 10), "USDC", 12, -20.0, 79.91, sub=202),
    bill(ts(D3, 10), "USDC", 12, 20.0, 20.0, sub=200),
    # one of the bot's own trades
    bill(ts(D3, 10, 1), "DOOD", 2, 100.0, 100.0, px=0.1, inst="DOOD-USDC", ord_id="bot-1"),
    bill(ts(D3, 10, 1), "USDC", 2, -10.0, 10.0, px=0.1, inst="DOOD-USDC", ord_id="bot-1"),
    bill(ts(D3, 11), "USDC", 1, -9.91, 70.0, sub=12),
]

BALANCES = [
    Balance("USDC", 80.0, 70.0, 10.0, 80.0),
    Balance("SOL", 0.4982, 0.4982, 0.0, 34.874),
    Balance("DOOD", 100.0, 0.0, 100.0, 10.0),
]

BOTS = [
    Bot("b1", "grid", "DOOD-USDC", "running", 20.0, -0.5, 0.1, -0.6, ts(D3, 10), None),
]

CLOSES = {"SOL": {D1: 50.0, D2: 45.0, D3: 62.0}, "DOOD": {D3: 0.1}}
LIVE = {"SOL": 70.0, "DOOD": 0.1, "USDC": 1.0}

SOLD_COST = 50.0 + 40.0 * 0.501 / 0.9992
PROFIT = 89.91 - SOLD_COST
