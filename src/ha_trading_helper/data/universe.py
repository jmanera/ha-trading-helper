"""Choose which pairs to analyse: liquid spot pairs in the configured quote currencies."""

from __future__ import annotations

from dataclasses import dataclass

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
