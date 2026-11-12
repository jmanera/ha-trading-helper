"""Options of the intraday monitor app (same sources as the 4h app: env HTH_*, options.json).

Only the alerts are configurable. The measured rules are fixed in rules.py on purpose.
"""

from __future__ import annotations

from datetime import timedelta

from ha_trading_helper.config import QuoteCurrency, Settings
from ha_trading_helper.intraday.alerts import AlertSettings


class IntradaySettings(Settings):
    base_topic: str = "intraday_monitor"
    # USDT has about three times as many liquid pairs as USDC on OKX: the measurement
    # needs the trades (100 per rule) more than the 4h app needs the user's currency.
    quote_currency: QuoteCurrency = "USDT"
    max_pairs: int = 20
    min_volume_24h_usdt: float = 5_000_000.0

    # Alerts (tunable at any time; they do not affect the measurement)
    volume_spike: float = 3.0
    move_atr: float = 2.0
    breakout_alerts: bool = True
    btc_alerts: bool = True
    cooldown_minutes: int = 60

    # Seconds after each 5 min close before asking OKX, so the candle is final.
    poll_delay_seconds: int = 15

    def alert_settings(self) -> AlertSettings:
        return AlertSettings(
            volume_spike=self.volume_spike,
            move_atr=self.move_atr,
            breakouts=self.breakout_alerts,
            btc_regime=self.btc_alerts,
            cooldown=timedelta(minutes=self.cooldown_minutes),
        )
