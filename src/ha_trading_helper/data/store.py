"""Parquet cache of closed candles, one file per instrument and bar."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd

from ha_trading_helper.okx.client import OkxClient
from ha_trading_helper.okx.models import Candle

COLUMNS = ["open", "high", "low", "close", "volume", "volume_quote"]


def candles_to_frame(candles: list[Candle]) -> pd.DataFrame:
    """Closed candles only, indexed by UTC open time, oldest first."""
    rows = [c.model_dump() for c in candles if c.confirmed]
    if not rows:
        return pd.DataFrame(columns=COLUMNS, index=pd.DatetimeIndex([], tz="UTC", name="ts"))
    frame = pd.DataFrame(rows).set_index("ts").sort_index()
    frame.index = pd.DatetimeIndex(frame.index, name="ts")
    frame = frame[~frame.index.duplicated(keep="last")]
    return frame[COLUMNS].astype(float)


class CandleStore:
    def __init__(self, root: Path) -> None:
        self.root = root / "candles"

    def path(self, inst_id: str, bar: str) -> Path:
        return self.root / bar / f"{inst_id}.parquet"

    def load(self, inst_id: str, bar: str) -> pd.DataFrame:
        path = self.path(inst_id, bar)
        if not path.is_file():
            return candles_to_frame([])
        return pd.read_parquet(path)

    def save(self, inst_id: str, bar: str, frame: pd.DataFrame) -> None:
        path = self.path(inst_id, bar)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        frame.to_parquet(tmp)
        tmp.replace(path)

    def merge(self, inst_id: str, bar: str, new: pd.DataFrame) -> pd.DataFrame:
        old = self.load(inst_id, bar)
        frame = new if old.empty else pd.concat([old, new])
        frame = frame[~frame.index.duplicated(keep="last")].sort_index()
        self.save(inst_id, bar, frame)
        return frame

    async def sync(
        self, client: OkxClient, inst_id: str, bar: str = "4H", *, count: int = 300
    ) -> pd.DataFrame:
        """Download what is missing (incremental after the first run) and return the cache."""
        cached = self.load(inst_id, bar)
        since: datetime | None = None
        if len(cached) >= count:
            since = cached.index[-1].to_pydatetime()
        # One extra row: the newest candle is usually still open and gets dropped.
        candles = await client.candles(inst_id, bar, count=count + 1, since=since)
        return self.merge(inst_id, bar, candles_to_frame(candles))
