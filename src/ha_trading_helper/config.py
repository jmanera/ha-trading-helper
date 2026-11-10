"""Runtime settings.

Sources, highest priority first: explicit kwargs, environment variables (prefix ``HTH_``),
``.env`` in the working directory, and the Home Assistant app options file
(``/data/options.json``) when it exists.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

HA_OPTIONS_FILE = Path("/data/options.json")

# Quote currencies with enough OKX spot liquidity to build a universe.
QuoteCurrency = Literal["USDC", "USDT", "EUR"]


class Weights(BaseModel):
    """Points awarded by each evidence. They must add up to 100."""

    trend: int = 25
    momentum: int = 20
    rsi: int = 15
    breakout: int = 15
    volume: int = 10
    relative_strength: int = 15

    def total(self) -> int:
        return sum(self.model_dump().values())


class Position(BaseModel):
    """A position the user marked as open, used to decide SALIR."""

    pair: str
    entry_price: float
    entry_time: datetime


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="HTH_",
        env_file=".env",
        env_nested_delimiter="__",
        extra="ignore",
    )

    # Universe
    pairs: list[str] = Field(default_factory=list, description="Empty = automatic universe")
    quote_currency: QuoteCurrency = "USDC"
    max_pairs: int = 20
    min_volume_24h_usdt: float = 5_000_000.0  # USD equivalent, whatever the quote
    max_spread_pct: float = 0.2
    benchmark: str = Field(default="", description="Empty = BTC in the quote currency")

    # Strategy
    bar: str = "4H"
    min_history: int = 250
    enter_threshold: int = 70
    wait_threshold: int = 40
    weights: Weights = Field(default_factory=Weights)
    positions: list[Position] = Field(default_factory=list)
    require_validation: bool = True

    # Risk
    capital: float = 10_000.0
    risk_per_trade_pct: float = 1.0
    max_positions: int = 3
    stop_atr_mult: float = 2.0
    trailing_atr_mult: float = 2.5

    # Costs (verify against the OKX fee page for your tier)
    fee_pct: float = 0.10
    slippage_pct: float = 0.05

    # Storage
    data_dir: Path = Path(".data")

    # MQTT
    mqtt_host: str = "localhost"
    mqtt_port: int = 1883
    mqtt_username: str | None = None
    mqtt_password: str | None = None
    discovery_prefix: str = "homeassistant"
    base_topic: str = "ha_trading_helper"

    # Scheduler
    run_delay_seconds: int = 60
    log_level: str = "INFO"

    @model_validator(mode="after")
    def _default_benchmark(self) -> Settings:
        if not self.benchmark:
            self.benchmark = f"BTC-{self.quote_currency}"
        return self

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            HaOptionsSource(settings_cls),
        )


class HaOptionsSource(PydanticBaseSettingsSource):
    """Reads the options the user sets in the Home Assistant app UI."""

    path: Path = HA_OPTIONS_FILE

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        return None, field_name, False  # pragma: no cover - __call__ is used instead

    def __call__(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {}
        data: dict[str, Any] = json.loads(self.path.read_text(encoding="utf-8"))
        # Empty strings in the HA UI mean "not set".
        return {k: v for k, v in data.items() if v not in ("", None)}
