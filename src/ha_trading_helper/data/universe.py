"""Choose which pairs to analyse: liquid spot pairs in the configured quote currencies."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from ha_trading_helper.okx.models import Ticker

# Pegged / wrapped assets whose price does not trend on its own.
STABLE_BASES = {
    "USDT", "USDC", "DAI", "TUSD", "FDUSD", "PYUSD", "USDE", "USDG", "EURT", "EURC",
    "USD1", "RLUSD", "WBTC", "STETH", "WETH", "XAUT", "PAXG",
}  # fmt: skip


@dataclass(frozen=True)
class UniverseRules:
    quotes: tuple[str, ...] = ("USDT",)
    min_volume_24h_usdt: float = 5_000_000.0
    max_spread_pct: float = 0.2
    max_pairs: int = 20


def select_universe(
    tickers: list[Ticker],
    rules: UniverseRules,
    *,
    eur_usdt: float = 1.0,
    allowed: set[str] | None = None,
) -> list[str]:
    """Return instrument ids sorted by 24h quote volume (in USDT), most liquid first.

    ``allowed`` restricts the result to known live crypto instruments (OKX also lists
    tokenised stocks on spot).
    """
    picked: list[tuple[float, str]] = []
    for t in tickers:
        base, _, quote = t.inst_id.partition("-")
        if quote not in rules.quotes or base in STABLE_BASES:
            continue
        if allowed is not None and t.inst_id not in allowed:
            continue
        volume_usdt = t.volume_24h_quote * (eur_usdt if quote == "EUR" else 1.0)
        if volume_usdt < rules.min_volume_24h_usdt:
            continue
        spread = t.spread_pct
        if spread is None or spread > rules.max_spread_pct:
            continue
        picked.append((volume_usdt, t.inst_id))
    picked.sort(reverse=True)
    return [inst for _, inst in picked[: rules.max_pairs]]


def is_candidate(inst_id: str, quote: str) -> bool:
    """A pair that could ever enter the automatic universe: right quote, not pegged."""
    base, _, q = inst_id.partition("-")
    return q == quote and base not in STABLE_BASES


def historical_membership(
    frames: dict[str, pd.DataFrame],
    *,
    max_pairs: int,
    min_daily_volume: float,
    window_bars: int,
    bars_per_day: float,
    volume_factor: float = 1.0,
) -> pd.DataFrame:
    """Which pairs were in the universe at each bar, using only volume known by then.

    A pair is a member at bar t when its mean quote volume over the last
    ``window_bars`` bars (scaled to a day, times ``volume_factor`` to get USDT) is
    at least ``min_daily_volume`` and among the ``max_pairs`` largest at t.
    Validating with today's most liquid pairs instead would only test the coins
    that did well enough to be liquid now (survivorship bias).
    """
    if not frames:
        return pd.DataFrame(dtype=bool)
    volume = pd.DataFrame({k: f["volume_quote"] for k, f in frames.items()}).sort_index()
    daily = volume.rolling(window_bars, min_periods=window_bars).mean()
    daily = daily * bars_per_day * volume_factor
    rank = daily.rank(axis=1, ascending=False, method="first")
    return (rank <= max_pairs) & (daily >= min_daily_volume)
