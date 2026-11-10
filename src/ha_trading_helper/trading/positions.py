"""Positions opened from Telegram, persisted in the app's data directory."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel

from ha_trading_helper.config import Position


class OpenPosition(BaseModel):
    pair: str
    quantity: float  # base currency actually held, net of the buy fee
    entry_price: float
    cost: float  # quote currency spent
    entry_time: datetime
    stop_price: float | None = None
    stop_algo_id: str | None = None

    @property
    def base(self) -> str:
        return self.pair.split("-")[0]

    def as_signal_position(self) -> Position:
        return Position(pair=self.pair, entry_price=self.entry_price, entry_time=self.entry_time)


class PositionStore:
    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / "positions.json"

    def load(self) -> dict[str, OpenPosition]:
        if not self.path.is_file():
            return {}
        rows = json.loads(self.path.read_text(encoding="utf-8"))
        return {r["pair"]: OpenPosition.model_validate(r) for r in rows}

    def _save(self, positions: dict[str, OpenPosition]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        rows = [p.model_dump(mode="json") for p in positions.values()]
        self.path.write_text(json.dumps(rows, indent=2), encoding="utf-8")

    def get(self, pair: str) -> OpenPosition | None:
        return self.load().get(pair)

    def add(self, position: OpenPosition) -> None:
        positions = self.load()
        positions[position.pair] = position
        self._save(positions)

    def remove(self, pair: str) -> None:
        positions = self.load()
        positions.pop(pair, None)
        self._save(positions)
