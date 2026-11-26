"""Paper trades of the measured rules, advanced one closed candle at a time.

Execution model (the same conservative one as the 4h backtest):

- A signal at the close of candle t is filled at the open of t+1 plus slippage.
- Inside a candle the stop is checked first (worst case). A gap through the stop
  fills at the open; on the entry candle the stop fills at the stop.
- Close-based exits (time stop, middle band) fill at the next open.
- Fees are charged on both sides. Results are net, in % of the entry and in R.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ha_trading_helper.intraday.rules import Rule
from ha_trading_helper.strategy.explain import fmt

FEE = 0.001  # 0.10 % per side (OKX spot taker, lowest tier)
SLIPPAGE = 0.0005  # 0.05 % per side
MIN_DAYS = 28
MIN_TRADES = 100


@dataclass
class Trade:
    rule: str
    pair: str
    signal_time: datetime
    atr: float  # ATR at the signal candle, sets the initial stop
    status: str = "pending"  # pending -> open -> (exiting) -> closed
    entry_time: datetime | None = None
    entry: float = 0.0
    stop: float = 0.0
    risk: float = 0.0
    highest: float = 0.0
    bars: int = 0
    exit_reason: str = ""
    exit_time: datetime | None = None
    exit_price: float = 0.0
    ret_pct: float = 0.0
    r: float = 0.0

    @property
    def closed(self) -> bool:
        return self.status == "closed"

    def _close(self, when: datetime, price: float, reason: str) -> None:
        exit_price = price * (1 - SLIPPAGE)
        net = exit_price - self.entry - FEE * (self.entry + exit_price)
        self.exit_time, self.exit_price, self.exit_reason = when, exit_price, reason
        self.ret_pct = net / self.entry * 100
        self.r = net / self.risk if self.risk > 0 else 0.0
        self.status = "closed"

    def step(self, rule: Rule, when: datetime, bar: dict[str, float]) -> None:
        """Advance with the closed candle that opened at ``when``."""
        if self.closed:
            return
        if self.status == "exiting":
            self._close(when, bar["open"], self.exit_reason)
            return
        first = self.status == "pending"
        if first:
            self.entry = bar["open"] * (1 + SLIPPAGE)
            self.stop = self.entry - rule.exits.stop_atr * self.atr
            self.risk = self.entry - self.stop
            self.highest = self.entry
            self.entry_time = when
            self.status = "open"
        self.bars += 1
        if bar["low"] <= self.stop:
            self._close(when, self.stop if first else min(bar["open"], self.stop), "stop")
            return
        exits = rule.exits
        if exits.trailing_atr is not None and math.isfinite(bar["atr"]):
            self.highest = max(self.highest, bar["high"])
            self.stop = max(self.stop, self.highest - exits.trailing_atr * bar["atr"])
        if exits.exit_on_mid_band and bar["close"] >= bar["bb_mid"]:
            self.status, self.exit_reason = "exiting", "banda media"
        elif self.bars >= exits.max_bars:
            self.status, self.exit_reason = "exiting", "tiempo"


@dataclass(frozen=True)
class Stats:
    closed: int
    open: int
    wins: int
    mean_pct: float
    mean_r: float
    ci_pct: tuple[float, float]  # 95 % interval of the mean, normal approximation
    total_pct: float
    sd_pct: float = field(default=0.0)

    @property
    def win_rate(self) -> float:
        return self.wins / self.closed if self.closed else 0.0


def stats(trades: list[Trade]) -> Stats:
    closed = [t for t in trades if t.closed]
    n = len(closed)
    rets = [t.ret_pct for t in closed]
    mean = sum(rets) / n if n else 0.0
    sd = math.sqrt(sum((x - mean) ** 2 for x in rets) / (n - 1)) if n > 1 else 0.0
    half = 1.96 * sd / math.sqrt(n) if n > 1 else 0.0
    return Stats(
        closed=n,
        open=len(trades) - n,
        wins=sum(1 for x in rets if x > 0),
        mean_pct=mean,
        mean_r=sum(t.r for t in closed) / n if n else 0.0,
        ci_pct=(mean - half, mean + half),
        total_pct=sum(rets),
        sd_pct=sd,
    )


def signed(value: float, decimals: int = 3) -> str:
    return ("+" if value >= 0 else "") + fmt(value, decimals)


def verdict(rule: Stats, control: Stats, started: datetime, now: datetime) -> tuple[str, str]:
    """The criterion fixed with the rules: (code, Spanish sentence).

    After at least 28 days and 100 closed trades, a rule passes when its mean net
    result is positive AND beats its random control with 95 % confidence.
    """
    days = (now - started) / timedelta(days=1)
    if days < MIN_DAYS or rule.closed < MIN_TRADES or control.closed < 2:
        trades_left = max(0, MIN_TRADES - rule.closed)
        days_left = max(0, math.ceil(MIN_DAYS - days))
        missing = [
            f"{n} {one if n == 1 else many}"
            for n, one, many in (
                (trades_left, "operación", "operaciones"),
                (days_left, "día", "días"),
            )
            if n > 0
        ]
        left = (
            f"faltan {' y '.join(missing)} como mínimo"
            if missing
            else "falta que el control cierre operaciones"
        )
        return "midiendo", (
            f"Midiendo: {rule.closed} de {MIN_TRADES} operaciones, {left}. "
            "No saques conclusiones todavía."
        )
    diff = rule.mean_pct - control.mean_pct
    half = 1.96 * math.sqrt(rule.sd_pct**2 / rule.closed + control.sd_pct**2 / control.closed)
    if rule.mean_pct > 0 and diff - half > 0:
        return "cumple", (
            f"Cumple: gana {signed(rule.mean_pct)} % por operación tras costes y supera al "
            f"azar por {signed(diff)} ± {fmt(half, 3)} %."
        )
    return "no_cumple", (
        f"No cumple: {signed(rule.mean_pct)} % por operación tras costes; frente al azar "
        f"{signed(diff)} ± {fmt(half, 3)} %. Sin ventaja demostrada."
    )
