"""Invented, self-consistent OKX account for the journal screenshots (no real data).

    uv run python docs/demo/demo_journal.py .data/demo.db     # build the database
    uv run python docs/demo/demo_journal.py .data/demo.db --serve   # panel on :8099

Prices are seeded random walks; every fill writes its two ledger entries with the running
balance, exactly as OKX does, so the journal rebuilds the same figures it would from a real
account.
"""

from __future__ import annotations

import pathlib
import random
import sys
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from ha_trading_helper.journal.models import Balance, Bill, Bot, Fill, OpenOrder, Order
from ha_trading_helper.journal.store import Store

START = date(2026, 9, 1)
DAYS = 76
NOW = datetime(2026, 11, 15, 18, 30, tzinfo=UTC)
ASSETS = {"BTC": 62_000.0, "ETH": 2_450.0, "SOL": 145.0, "LINK": 14.5, "ARB": 0.82}
DRIFT = {"BTC": 0.0022, "ETH": 0.0018, "SOL": 0.003, "LINK": -0.0005, "ARB": -0.002}
MAKER, TAKER = 0.0008, 0.001
FLOWS = {0: 2_000.0, 21: 1_000.0, 48: -300.0, 57: 750.0}


def ms(day: date, hour: int, minute: int = 0) -> int:
    return int(datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC).timestamp() * 1000)


@dataclass
class Account:
    rng: random.Random
    next_id: int = 4_100_000_000_000_000_000
    cash: dict[str, float] = field(default_factory=lambda: {"USDC": 0.0})
    fills: list[Fill] = field(default_factory=list)
    bills: list[Bill] = field(default_factory=list)
    orders: list[Order] = field(default_factory=list)

    def nid(self) -> str:
        self.next_id += self.rng.randint(3, 900)
        return str(self.next_id)

    def bill(
        self,
        ts: int,
        ccy: str,
        type_: int,
        sub: int,
        chg: float,
        *,
        px: float | None = None,
        inst: str = "",
        ord_id: str = "",
        trade: str = "",
        notes: str = "",
    ) -> None:
        self.cash[ccy] = self.cash.get(ccy, 0.0) + chg
        self.bills.append(
            Bill(
                self.nid(), ts, ccy, type_, sub, chg, self.cash[ccy], px, inst, ord_id, trade, notes
            )
        )

    def transfer(self, ts: int, amount: float) -> None:
        sub = 11 if amount > 0 else 12
        self.bill(
            ts, "USDC", 1, sub, amount, notes="From: Funding" if amount > 0 else "To: Funding"
        )

    def trade(
        self, ts: int, ccy: str, side: str, px: float, sz: float, maker: bool, kind: str
    ) -> None:
        inst, ord_id = f"{ccy}-USDC", self.nid()
        rate = MAKER if maker else TAKER
        trade_id = str(self.rng.randint(10_000, 99_999))
        if side == "buy":
            fee, fee_ccy = -sz * rate, ccy
            self.bill(ts, "USDC", 2, 2, -sz * px, px=px, inst=inst, ord_id=ord_id, trade=trade_id)
            self.bill(ts, ccy, 2, 1, sz + fee, px=px, inst=inst, ord_id=ord_id, trade=trade_id)
        else:
            fee, fee_ccy = -sz * px * rate, "USDC"
            self.bill(
                ts, "USDC", 2, 1, sz * px + fee, px=px, inst=inst, ord_id=ord_id, trade=trade_id
            )
            self.bill(ts, ccy, 2, 2, -sz, px=px, inst=inst, ord_id=ord_id, trade=trade_id)
        self.fills.append(
            Fill(
                self.nid(),
                trade_id,
                ord_id,
                inst,
                side,
                px,
                sz,
                fee,
                fee_ccy,
                -rate,
                "M" if maker else "T",
                ts,
            )
        )
        self.orders.append(
            Order(
                ord_id,
                inst,
                side,
                kind,
                "filled",
                px if kind != "market" else None,
                sz,
                px,
                sz,
                fee,
                fee_ccy,
                "",
                ts - 60_000,
                ts,
            )
        )


def prices(rng: random.Random) -> dict[str, dict[date, float]]:
    out: dict[str, dict[date, float]] = {}
    for ccy, start_px in ASSETS.items():
        px = start_px
        series = {}
        for i in range(DAYS):
            px *= 1 + DRIFT[ccy] + rng.gauss(0, 0.032 if ccy != "BTC" else 0.022)
            series[START + timedelta(days=i)] = round(px, 6 if px < 1 else 2)
        out[ccy] = series
    return out


def build(path: str) -> Store:
    rng = random.Random(7)  # noqa: S311 - demo prices, not security
    closes = prices(rng)
    acct = Account(rng)
    bot_start = 40
    bot_ccy = "ARB"
    bot_day = START + timedelta(days=bot_start)
    # The bot holds half in ARB, half in USDC, and slowly makes grid profit.
    bot_arb = 125.0 / closes[bot_ccy][bot_day]
    snapshots: list[tuple[int, float, float]] = []

    def bot_value(day: date, px: float) -> float:
        running = (day - bot_day).days
        return 0.0 if running < 0 else 125.0 + 0.125 * running + bot_arb * px

    for i in range(DAYS - 1):
        day = START + timedelta(days=i)
        if i in FLOWS:
            acct.transfer(ms(day, 9), FLOWS[i])
        if i == bot_start:
            acct.bill(ms(day, 10), "USDC", 12, 202, -250.0)  # into the grid bot
        stamps = sorted(
            ms(day, rng.randint(10, 22), rng.randint(0, 59))
            for _ in range(rng.choice([0, 1, 1, 2, 2, 3]))
        )
        for ts in stamps:
            ccy = rng.choice(list(ASSETS))
            px = closes[ccy][day] * (1 + rng.uniform(-0.012, 0.012))
            held = acct.cash.get(ccy, 0.0)
            maker = rng.random() < 0.4
            kind = "limit" if maker else rng.choice(["market", "market", "limit"])
            if held * px > 40 and rng.random() < 0.45:
                share = (
                    rng.choice([0.5, 1.0, 1.0]) if i < DAYS - 12 or ccy in {"LINK", "ARB"} else 0.3
                )
                acct.trade(ts, ccy, "sell", px, round(held * share, 6), maker, kind)
            elif acct.cash["USDC"] > 80:
                budget = acct.cash["USDC"] * rng.uniform(0.12, 0.3)
                acct.trade(ts, ccy, "buy", px, round(budget / px, 6), maker, kind)
        bot = bot_value(day, closes[bot_ccy][day])
        cash = sum(q * (closes[c][day] if c in closes else 1.0) for c, q in acct.cash.items())
        snapshots.append((ms(day, 23, 50), cash + bot, bot))
    pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(path).unlink(missing_ok=True)
    store = Store(f"sqlite:///{path}")
    for snap in snapshots:
        store.add_snapshot(*snap)
    store.save_fills(acct.fills)
    store.save_bills(acct.bills)
    store.save_orders(acct.orders)
    for ccy, series in closes.items():
        store.save_prices(ccy, series)
    live = {c: s[NOW.date()] * 1.004 for c, s in closes.items()}
    live["USDC"] = 1.0
    now_bot = bot_value(NOW.date(), live[bot_ccy])
    bot_usdc = now_bot - bot_arb * live[bot_ccy]
    grid = bot_usdc - 125.0
    bots = [
        Bot(
            "demo-grid",
            "grid",
            f"{bot_ccy}-USDC",
            "running",
            250.0,
            now_bot - 250.0,
            grid,
            now_bot - 250.0 - grid,
            ms(bot_day, 10),
            None,
        )
    ]
    store.save_bots(bots)
    balances = [
        Balance(
            "USDC",
            acct.cash["USDC"] + bot_usdc,
            acct.cash["USDC"],
            bot_usdc,
            acct.cash["USDC"] + bot_usdc,
        )
    ]
    for ccy in ASSETS:
        held = acct.cash.get(ccy, 0.0)
        in_bot = bot_arb if ccy == bot_ccy else 0.0
        if held > 1e-9 or in_bot:
            balances.append(Balance(ccy, held + in_bot, held, in_bot, (held + in_bot) * live[ccy]))
    store.replace_balances(balances)
    equity = sum(b.eq * live[b.ccy] for b in balances)
    store.add_snapshot(int(NOW.timestamp() * 1000), equity, now_bot)
    store.set_json("live_prices", live)
    store.set_json("fee_rates", {"1": [MAKER, TAKER]})
    store.set_json("fee_groups", {f"{c}-USDC": "1" for c in ASSETS})
    store.set_json("last_sync", {"ts": int(NOW.timestamp() * 1000), "errors": [], "new_fills": 0})
    sol = acct.cash.get("SOL", 0.0)
    btc = acct.cash.get("BTC", 0.0)
    store.replace_open_orders(
        [
            OpenOrder(
                "demo-o1",
                "order",
                "SOL-USDC",
                "sell",
                "limit",
                round(live["SOL"] * 1.12, 2),
                round(sol / 2, 4),
                0.0,
                None,
                ms(NOW.date(), 9),
            ),
            OpenOrder(
                "demo-a1",
                "algo",
                "BTC-USDC",
                "sell",
                "conditional",
                None,
                round(btc, 6),
                0.0,
                round(live["BTC"] * 0.92, 1),
                ms(NOW.date(), 9, 5),
            ),
        ]
    )
    sells = [o for o in acct.orders if o.side == "sell"]
    notes = {
        f"order:{sells[-1].ord_id}": (
            "Recojo la mitad: pierde la media de 20 y el volumen cae.",
            "salida parcial, gestión",
        ),
        f"order:{sells[len(sells) // 2].ord_id}": (
            "Salí tarde: el stop mental estaba en -3 % y lo moví. No repetir.",
            "error, disciplina",
        ),
        f"order:{acct.orders[3].ord_id}": (
            "Entrada en retroceso a la EMA 50 con BTC fuerte.",
            "retroceso, tendencia",
        ),
        "asset:SOL": ("Mi activo principal. Plan: acumular en retrocesos, vender por tramos.", ""),
        "asset:ARB": ("Demasiada rotación para lo que aporta. Solo vía bot grid.", ""),
    }
    for ref, (text, tags) in notes.items():
        store.save_note(ref, text, tags, int(NOW.timestamp() * 1000))
    return store


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else ".data/demo.db"
    store = build(path)
    print(f"demo journal in {path}")
    if "--serve" in sys.argv:
        import uvicorn

        from ha_trading_helper.journal.web import create_app

        uvicorn.run(create_app(store, clock=lambda: NOW), host="127.0.0.1", port=8099)


if __name__ == "__main__":
    main()
