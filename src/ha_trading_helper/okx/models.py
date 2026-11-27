"""Typed views of the OKX v5 public market endpoints we use."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel


class Candle(BaseModel):
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float  # base currency
    volume_quote: float
    confirmed: bool

    @classmethod
    def from_row(cls, row: list[str]) -> Candle:
        # [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]
        return cls(
            ts=datetime.fromtimestamp(int(row[0]) / 1000, tz=UTC),
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]),
            volume_quote=float(row[7]) if len(row) > 7 else float(row[6]),
            confirmed=row[8] == "1" if len(row) > 8 else True,
        )


class Ticker(BaseModel):
    inst_id: str
    last: float
    bid: float | None
    ask: float | None
    volume_24h_quote: float

    @property
    def quote(self) -> str:
        return self.inst_id.rsplit("-", 1)[-1]

    @property
    def spread_pct(self) -> float | None:
        if not self.bid or not self.ask or self.last <= 0:
            return None
        return (self.ask - self.bid) / self.last * 100

    @classmethod
    def from_api(cls, item: dict[str, str]) -> Ticker:
        def num(key: str) -> float | None:
            value = item.get(key, "")
            return float(value) if value not in ("", None) else None

        return cls(
            inst_id=item["instId"],
            last=num("last") or 0.0,
            bid=num("bidPx"),
            ask=num("askPx"),
            # For SPOT, volCcy24h is the 24h volume in the quote currency.
            volume_24h_quote=num("volCcy24h") or 0.0,
        )


class Instrument(BaseModel):
    inst_id: str
    category: str  # "1" = crypto; other values are tokenised stocks, metals, etc.
    state: str
    list_time: int | None = None  # ms

    @property
    def is_crypto_live(self) -> bool:
        return self.category == "1" and self.state == "live"

    @classmethod
    def from_api(cls, item: dict[str, str]) -> Instrument:
        return cls(
            inst_id=item["instId"],
            category=item.get("instCategory", ""),
            state=item.get("state", ""),
            list_time=int(item["listTime"]) if item.get("listTime") else None,
        )
