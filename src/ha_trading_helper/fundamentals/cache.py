"""Answers of the sources kept in SQLite, so the free limits last and restarts are cheap."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any


class Cache:
    def __init__(self, path: Path | str, *, clock: Callable[[], float] = time.time) -> None:
        if isinstance(path, Path):
            path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, ts REAL, value TEXT)"
        )
        self._lock = threading.Lock()
        self._clock = clock

    def get(self, key: str, max_age: float | None = None) -> Any:
        """The stored value, or None if missing or older than ``max_age`` seconds."""
        with self._lock:
            row = self._db.execute("SELECT ts, value FROM cache WHERE key = ?", (key,)).fetchone()
        if row is None or (max_age is not None and self._clock() - row[0] > max_age):
            return None
        return json.loads(row[1])

    def age(self, key: str) -> float | None:
        with self._lock:
            row = self._db.execute("SELECT ts FROM cache WHERE key = ?", (key,)).fetchone()
        return self._clock() - row[0] if row else None

    def put(self, key: str, value: Any) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO cache (key, ts, value) VALUES (?, ?, ?)",
                (key, self._clock(), json.dumps(value)),
            )
            self._db.commit()

    def bump(self, key: str) -> int:
        """Add one to a counter and return it."""
        with self._lock:
            row = self._db.execute("SELECT value FROM cache WHERE key = ?", (key,)).fetchone()
            count = (json.loads(row[0]) if row else 0) + 1
            self._db.execute(
                "INSERT OR REPLACE INTO cache (key, ts, value) VALUES (?, ?, ?)",
                (key, self._clock(), json.dumps(count)),
            )
            self._db.commit()
        return count

    async def fetch(self, key: str, ttl: float, load: Callable[[], Awaitable[Any]]) -> Any:
        value = self.get(key, ttl)
        if value is None:
            value = await load()
            self.put(key, value)
        return value

    def close(self) -> None:
        with self._lock:
            self._db.close()
