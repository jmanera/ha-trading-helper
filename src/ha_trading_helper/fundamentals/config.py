"""Options of the fundamentals app (same sources as the other apps: env HTH_*, options.json)."""

from __future__ import annotations

from pydantic import Field

from ha_trading_helper.config import Settings


class FundamentalsSettings(Settings):
    coingecko_api_key: str = ""  # free Demo key; without it CoinGecko allows a few calls a minute
    github_token: str = ""  # any token without scopes: 5.000 calls an hour instead of 60
    refresh_minutes: int = 5  # the 4h list of pairs
    cache_hours: int = 12  # an analysis is reused for this long
    coin_ids: list[str] = Field(
        default_factory=list, description="SYMBOL=coingecko-id, when the symbol is ambiguous"
    )
    timezone: str = "Europe/Madrid"
    web_host: str = "0.0.0.0"  # noqa: S104  # nosec B104 - reached through HA ingress only
    web_port: int = 8099

    def coin_id_overrides(self) -> dict[str, str]:
        pairs = (item.split("=", 1) for item in self.coin_ids if "=" in item)
        return {k.strip().upper(): v.strip().lower() for k, v in pairs if k.strip() and v.strip()}
