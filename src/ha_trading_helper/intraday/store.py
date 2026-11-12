"""SQLite record of the measurement: trades, progress per candle series, alerts, gaps.

Everything needed to resume after a restart lives here, so a reboot of Home Assistant
never loses an open paper trade or processes a candle twice.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, fields
from datetime import UTC, datetime
from pathlib import Path

from ha_trading_helper.intraday.alerts import Alert
from ha_trading_helper.intraday.paper import Trade

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS progress (
    pair TEXT NOT NULL, bar TEXT NOT NULL, last_open TEXT NOT NULL,
    PRIMARY KEY (pair, bar)
);
CREATE TABLE IF NOT EXISTS trades (
    rule TEXT NOT NULL, pair TEXT NOT NULL, signal_time TEXT NOT NULL,
    status TEXT NOT NULL, exit_time TEXT, ret_pct REAL, r REAL, body TEXT NOT NULL,
    PRIMARY KEY (rule, pair, signal_time)
);
CREATE TABLE IF NOT EXISTS alerts (
    time TEXT NOT NULL, pair TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL,
    price REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS gaps (start TEXT NOT NULL, end TEXT NOT NULL, note TEXT NOT NULL);
"""

_DATES = ("signal_time", "entry_time", "exit_time")


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value is not None else None


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class MeasurementStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    # meta
    def get_meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))
        self.db.commit()

    def started_at(self, now: datetime) -> datetime:
        """When the measurement started (set on the first call)."""
        value = self.get_meta("started_at")
        if value is None:
            self.set_meta("started_at", _iso(now) or "")
            return now
        return datetime.fromisoformat(value)

    # progress
    def last_open(self, pair: str, bar: str) -> datetime | None:
        row = self.db.execute(
            "SELECT last_open FROM progress WHERE pair = ? AND bar = ?", (pair, bar)
        ).fetchone()
        return _dt(row[0]) if row else None

    def set_last_open(self, pair: str, bar: str, when: datetime) -> None:
        self.db.execute("INSERT OR REPLACE INTO progress VALUES (?, ?, ?)", (pair, bar, _iso(when)))

    # trades
    def save_trade(self, t: Trade) -> None:
        body = {k: (_iso(v) if k in _DATES else v) for k, v in asdict(t).items()}
        self.db.execute(
            "INSERT OR REPLACE INTO trades VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (t.rule, t.pair, _iso(t.signal_time), t.status, _iso(t.exit_time),
             t.ret_pct if t.closed else None, t.r if t.closed else None, json.dumps(body)),
        )  # fmt: skip

    def trades(self, rule: str | None = None, *, open_only: bool = False) -> list[Trade]:
        sql, args = "SELECT body FROM trades WHERE 1 = 1", []
        if rule is not None:
            sql, args = sql + " AND rule = ?", [rule]
        if open_only:
            sql += " AND status != 'closed'"
        names = {f.name for f in fields(Trade)}
        out = []
        for (body,) in self.db.execute(sql + " ORDER BY signal_time", args):
            data = {k: v for k, v in json.loads(body).items() if k in names}
            for k in _DATES:
                data[k] = _dt(data.get(k))
            out.append(Trade(**data))
        return out

    # alerts and gaps
    def add_alert(self, a: Alert) -> None:
        self.db.execute(
            "INSERT INTO alerts VALUES (?, ?, ?, ?, ?)",
            (_iso(a.time), a.pair, a.kind, a.text, a.price),
        )

    def alerts(self, since: datetime, limit: int = 10) -> list[Alert]:
        rows = self.db.execute(
            "SELECT time, pair, kind, text, price FROM alerts WHERE time >= ? "
            "ORDER BY time DESC LIMIT ?",
            (_iso(since), limit),
        )
        return [Alert(datetime.fromisoformat(t), p, k, x, pr) for t, p, k, x, pr in rows]

    def count_alerts(self, since: datetime) -> int:
        row = self.db.execute("SELECT COUNT(*) FROM alerts WHERE time >= ?", (_iso(since),))
        return int(row.fetchone()[0])

    def add_gap(self, start: datetime, end: datetime, note: str) -> None:
        self.db.execute("INSERT INTO gaps VALUES (?, ?, ?)", (_iso(start), _iso(end), note))

    def gaps(self) -> list[tuple[datetime, datetime, str]]:
        rows = self.db.execute("SELECT start, end, note FROM gaps ORDER BY start")
        return [(datetime.fromisoformat(a), datetime.fromisoformat(b), n) for a, b, n in rows]

    def commit(self) -> None:
        self.db.commit()
