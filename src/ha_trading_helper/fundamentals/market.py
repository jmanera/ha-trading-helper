"""The list of OKX crypto/USDC pairs with their price change and volume over the last 4 hours."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import structlog

from ha_trading_helper.data.universe import STABLE_BASES
from ha_trading_helper.okx.client import OkxClient
from ha_trading_helper.okx.models import Candle

QUOTE = "USDC"
# 48 five-minute candles = the last 4 hours, the newest one still open.
WINDOW_BAR = "5m"
WINDOW_BARS = 48

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class Pair:
    inst_id: str
    last: float
    change_4h: float | None  # fraction
    volume_4h: float | None  # USDC
    volume_24h: float  # USDC
    listed: int | None = None  # ms, first listing of the coin on OKX in any quote

    @property
    def base(self) -> str:
        return self.inst_id.split("-", 1)[0]


def window(candles: list[Candle]) -> tuple[float | None, float | None]:
    """(price change, quote volume) over the candles, oldest first."""
    if not candles or candles[0].open <= 0:
        return None, None
    change = candles[-1].close / candles[0].open - 1
    return change, sum(c.volume_quote for c in candles)


async def load_pairs(okx: OkxClient) -> list[Pair]:
    """Live crypto pairs quoted in USDC (no stablecoins nor tokenised stocks), by 4h volume."""
    instruments, tickers = await asyncio.gather(okx.instruments(), okx.tickers())
    live = {
        i.inst_id
        for i in instruments
        if i.is_crypto_live
        and i.inst_id.endswith(f"-{QUOTE}")
        and i.inst_id.split("-", 1)[0] not in STABLE_BASES
    }
    picked = [t for t in tickers if t.inst_id in live]
    listed: dict[str, int] = {}
    for i in instruments:
        base = i.inst_id.split("-", 1)[0]
        if i.list_time:
            listed[base] = min(listed.get(base, i.list_time), i.list_time)

    async def one(inst_id: str) -> tuple[float | None, float | None]:
        try:
            return window(await okx.recent_candles(inst_id, WINDOW_BAR, WINDOW_BARS))
        except Exception as exc:
            log.warning("pair_window_failed", inst_id=inst_id, error=str(exc))
            return None, None

    windows = await asyncio.gather(*(one(t.inst_id) for t in picked))
    pairs = [
        Pair(
            t.inst_id,
            t.last,
            change,
            volume,
            t.volume_24h_quote,
            listed.get(t.inst_id.split("-", 1)[0]),
        )
        for t, (change, volume) in zip(picked, windows, strict=True)
    ]
    return sorted(pairs, key=lambda p: p.volume_4h or 0.0, reverse=True)
