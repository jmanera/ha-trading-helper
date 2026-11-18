"""Database of the journal: MariaDB in Home Assistant, SQLite for development and tests.

OKX only serves the last 3 months of fills and ledger entries and has no history of the
account value, so everything is kept here as it arrives. The schema is created on start
(``create_all`` is idempotent); ``meta.schema_version`` marks it for future migrations.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import asdict, fields
from datetime import date
from typing import Any, TypeVar

from sqlalchemy import (
    BigInteger,
    Column,
    Date,
    Double,
    Engine,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    delete,
    func,
    insert,
    select,
)
from sqlalchemy.dialects import mysql, sqlite
from sqlalchemy.pool import StaticPool

from ha_trading_helper.journal.models import Balance, Bill, Bot, Fill, OpenOrder, Order

SCHEMA_VERSION = "1"

metadata = MetaData()

ID = 32
CCY = 32
INST = 48

meta = Table(
    "meta",
    metadata,
    Column("key", String(64), primary_key=True),
    Column("value", Text, nullable=False),
)

fills = Table(
    "fills",
    metadata,
    Column("bill_id", String(ID), primary_key=True),
    Column("trade_id", String(ID), nullable=False),
    Column("ord_id", String(ID), nullable=False, index=True),
    Column("inst_id", String(INST), nullable=False, index=True),
    Column("side", String(8), nullable=False),
    Column("px", Double, nullable=False),
    Column("sz", Double, nullable=False),
    Column("fee", Double, nullable=False),
    Column("fee_ccy", String(CCY), nullable=False),
    Column("fee_rate", Double, nullable=False),
    Column("exec_type", String(4), nullable=False),
    Column("ts", BigInteger, nullable=False, index=True),
)

bills = Table(
    "bills",
    metadata,
    Column("bill_id", String(ID), primary_key=True),
    Column("ts", BigInteger, nullable=False, index=True),
    Column("ccy", String(CCY), nullable=False),
    Column("type", Integer, nullable=False),
    Column("sub_type", Integer, nullable=False),
    Column("bal_chg", Double, nullable=False),
    Column("bal", Double, nullable=False),
    Column("px", Double, nullable=True),
    Column("inst_id", String(INST), nullable=False),
    Column("ord_id", String(ID), nullable=False),
    Column("trade_id", String(ID), nullable=False),
    Column("notes", String(255), nullable=False),
)

orders = Table(
    "orders",
    metadata,
    Column("ord_id", String(ID), primary_key=True),
    Column("inst_id", String(INST), nullable=False),
    Column("side", String(8), nullable=False),
    Column("ord_type", String(24), nullable=False),
    Column("state", String(24), nullable=False),
    Column("px", Double, nullable=True),
    Column("sz", Double, nullable=False),
    Column("avg_px", Double, nullable=True),
    Column("filled", Double, nullable=False),
    Column("fee", Double, nullable=False),
    Column("fee_ccy", String(CCY), nullable=False),
    Column("source", String(8), nullable=False),
    Column("c_time", BigInteger, nullable=False, index=True),
    Column("u_time", BigInteger, nullable=False),
)

open_orders = Table(
    "open_orders",
    metadata,
    Column("id", String(ID), primary_key=True),
    Column("kind", String(8), nullable=False),
    Column("inst_id", String(INST), nullable=False),
    Column("side", String(8), nullable=False),
    Column("ord_type", String(24), nullable=False),
    Column("px", Double, nullable=True),
    Column("sz", Double, nullable=False),
    Column("filled", Double, nullable=False),
    Column("trigger_px", Double, nullable=True),
    Column("c_time", BigInteger, nullable=False),
)

bots = Table(
    "bots",
    metadata,
    Column("algo_id", String(ID), primary_key=True),
    Column("algo_type", String(24), nullable=False),
    Column("inst_id", String(INST), nullable=False),
    Column("state", String(24), nullable=False),
    Column("investment", Double, nullable=False),
    Column("total_pnl", Double, nullable=False),
    Column("grid_profit", Double, nullable=False),
    Column("float_profit", Double, nullable=False),
    Column("c_time", BigInteger, nullable=False),
    Column("stop_time", BigInteger, nullable=True),
)

balances = Table(
    "balances",
    metadata,
    Column("ccy", String(CCY), primary_key=True),
    Column("eq", Double, nullable=False),
    Column("cash_bal", Double, nullable=False),
    Column("stgy_eq", Double, nullable=False),
    Column("eq_usd", Double, nullable=False),
)

# One row per sync: the account value as OKX reported it at that moment.
snapshots = Table(
    "snapshots",
    metadata,
    Column("ts", BigInteger, primary_key=True),
    Column("equity", Double, nullable=False),
    Column("bot_value", Double, nullable=False),
)

prices_daily = Table(
    "prices_daily",
    metadata,
    Column("ccy", String(CCY), primary_key=True),
    Column("day", Date, primary_key=True),
    Column("close", Double, nullable=False),
)

notes = Table(
    "notes",
    metadata,
    Column("ref", String(80), primary_key=True),  # "order:<ordId>" or "asset:<ccy>"
    Column("text", Text, nullable=False),
    Column("tags", String(255), nullable=False, default=""),
    Column("updated", BigInteger, nullable=False),
)

T = TypeVar("T")


def _rows(items: Iterable[Any]) -> list[dict[str, Any]]:
    return [asdict(i) for i in items]


def _build[T](cls: type[T], rows: Sequence[Any]) -> list[T]:
    names = [f.name for f in fields(cls)]  # type: ignore[arg-type]
    return [cls(**{n: getattr(r, n) for n in names}) for r in rows]


class Store:
    def __init__(self, url: str, *, engine: Engine | None = None) -> None:
        if engine is None and url == "sqlite://":
            # In-memory (tests): one connection shared by every thread, or each sees its own DB.
            engine = create_engine(
                url, poolclass=StaticPool, connect_args={"check_same_thread": False}
            )
        self.engine = engine or create_engine(url, pool_pre_ping=True, pool_recycle=3600)
        metadata.create_all(self.engine)
        if self.get_meta("schema_version") is None:
            self.set_meta("schema_version", SCHEMA_VERSION)

    def close(self) -> None:
        self.engine.dispose()

    # helpers ---------------------------------------------------------------------------

    def _upsert(self, table: Table, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        keys = {c.name for c in table.primary_key.columns}
        others = [c.name for c in table.columns if c.name not in keys]
        dialect = self.engine.dialect.name
        with self.engine.begin() as conn:
            for start in range(0, len(rows), 500):
                chunk = rows[start : start + 500]
                if dialect == "sqlite":
                    s_stmt = sqlite.insert(table).values(chunk)
                    update = {n: s_stmt.excluded[n] for n in others}
                    conn.execute(
                        s_stmt.on_conflict_do_update(index_elements=list(keys), set_=update)
                        if update
                        else s_stmt.on_conflict_do_nothing()
                    )
                elif dialect in {"mysql", "mariadb"}:
                    m_stmt = mysql.insert(table).values(chunk)
                    mupdate = {n: m_stmt.inserted[n] for n in others} or {
                        k: m_stmt.inserted[k] for k in keys
                    }
                    conn.execute(m_stmt.on_duplicate_key_update(**mupdate))
                else:  # pragma: no cover - only MariaDB and SQLite are supported
                    raise NotImplementedError(dialect)

    def _replace(self, table: Table, rows: list[dict[str, Any]]) -> None:
        with self.engine.begin() as conn:
            conn.execute(delete(table))
            if rows:
                conn.execute(insert(table), rows)

    # meta ------------------------------------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        with self.engine.connect() as conn:
            value = conn.execute(select(meta.c.value).where(meta.c.key == key)).scalar()
        return str(value) if value is not None else None

    def set_meta(self, key: str, value: str) -> None:
        self._upsert(meta, [{"key": key, "value": value}])

    def get_json(self, key: str, default: Any) -> Any:
        raw = self.get_meta(key)
        return json.loads(raw) if raw else default

    def set_json(self, key: str, value: Any) -> None:
        self.set_meta(key, json.dumps(value))

    # writes ----------------------------------------------------------------------------

    def save_fills(self, items: Iterable[Fill]) -> None:
        self._upsert(fills, _rows(items))

    def save_bills(self, items: Iterable[Bill]) -> None:
        self._upsert(bills, _rows(items))

    def save_orders(self, items: Iterable[Order]) -> None:
        self._upsert(orders, _rows(items))

    def save_bots(self, items: Iterable[Bot]) -> None:
        self._upsert(bots, _rows(items))

    def replace_open_orders(self, items: Iterable[OpenOrder]) -> None:
        self._replace(open_orders, _rows(items))

    def replace_balances(self, items: Iterable[Balance]) -> None:
        self._replace(balances, _rows(items))

    def add_snapshot(self, ts: int, equity: float, bot_value: float) -> None:
        self._upsert(snapshots, [{"ts": ts, "equity": equity, "bot_value": bot_value}])

    def save_prices(self, ccy: str, closes: dict[date, float]) -> None:
        self._upsert(prices_daily, [{"ccy": ccy, "day": d, "close": c} for d, c in closes.items()])

    def save_note(self, ref: str, text: str, tags: str, updated: int) -> None:
        if not text.strip() and not tags.strip():
            with self.engine.begin() as conn:
                conn.execute(delete(notes).where(notes.c.ref == ref))
            return
        self._upsert(
            notes, [{"ref": ref, "text": text.strip(), "tags": tags.strip(), "updated": updated}]
        )

    # reads -----------------------------------------------------------------------------

    def last_id(self, table: Table, column: str) -> str | None:
        """Highest stored id (OKX ids grow over time), to resume paging."""
        col = table.c[column]
        with self.engine.connect() as conn:
            # Ids are numeric strings of different lengths: order by length, then value.
            value = conn.execute(
                select(col).order_by(func.length(col).desc(), col.desc()).limit(1)
            ).scalar()
        return str(value) if value is not None else None

    def fills(self) -> list[Fill]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(fills).order_by(fills.c.ts, fills.c.bill_id)).all()
        return _build(Fill, rows)

    def bills(self) -> list[Bill]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(bills).order_by(bills.c.ts, bills.c.bill_id)).all()
        return _build(Bill, rows)

    def orders(self) -> dict[str, Order]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(orders)).all()
        return {o.ord_id: o for o in _build(Order, rows)}

    def open_orders(self) -> list[OpenOrder]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(open_orders).order_by(open_orders.c.c_time)).all()
        return _build(OpenOrder, rows)

    def bots(self) -> list[Bot]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(bots).order_by(bots.c.c_time)).all()
        return _build(Bot, rows)

    def balances(self) -> list[Balance]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(balances)).all()
        return _build(Balance, rows)

    def snapshots(self) -> list[tuple[int, float, float]]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(snapshots).order_by(snapshots.c.ts)).all()
        return [(int(r.ts), float(r.equity), float(r.bot_value)) for r in rows]

    def prices(self) -> dict[str, dict[date, float]]:
        out: dict[str, dict[date, float]] = {}
        with self.engine.connect() as conn:
            for r in conn.execute(select(prices_daily)).all():
                out.setdefault(r.ccy, {})[r.day] = float(r.close)
        return out

    def notes(self) -> dict[str, tuple[str, str]]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(notes)).all()
        return {r.ref: (r.text, r.tags) for r in rows}
