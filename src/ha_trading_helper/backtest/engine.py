"""Bar-by-bar long-only backtest using the same scoring as live signals.

Execution model (conservative, no look-ahead):

- The signal is read at the close of bar ``t``; the entry fills at the open of ``t+1``
  plus slippage.
- Initial stop at ``stop_atr_mult`` x ATR(t) below the fill; half the position is sold
  at 1.5R, the rest at 3R.
- Inside a bar the stop is checked before targets (worst case). A gap through the
  stop fills at the open.
- Close-based exits (trailing stop of ``trailing_atr_mult`` x ATR from the highest
  close, or losing the EMA 50) fill at the next open.
- Fees are charged on both sides; results are expressed in R (initial risk).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
import pandas as pd

from ha_trading_helper.config import Weights
from ha_trading_helper.strategy.features import compute_features
from ha_trading_helper.strategy.rules import exit_signal
from ha_trading_helper.strategy.scoring import score_frame


@dataclass(frozen=True)
class CostModel:
    fee_pct: float = 0.10
    slippage_pct: float = 0.05

    @property
    def fee(self) -> float:
        return self.fee_pct / 100

    @property
    def slip(self) -> float:
        return self.slippage_pct / 100


@dataclass(frozen=True)
class Trade:
    pair: str
    signal_time: datetime
    entry_time: datetime
    entry: float
    stop: float
    exit_time: datetime
    exit_price: float  # size-weighted average
    r_multiple: float
    exit_reason: str
    bars_held: int
    score: int


@dataclass(frozen=True)
class EngineParams:
    enter_threshold: int = 70
    min_history: int = 250
    stop_atr_mult: float = 2.0
    trailing_atr_mult: float = 2.5
    weights: Weights = field(default_factory=Weights)


def simulate(
    pair: str,
    features: pd.DataFrame,
    entries: pd.Series,
    scores: pd.Series,
    params: EngineParams,
    costs: CostModel,
) -> list[Trade]:
    o = features["open"].to_numpy(float)
    h = features["high"].to_numpy(float)
    lo = features["low"].to_numpy(float)
    c = features["close"].to_numpy(float)
    atr = features["atr"].to_numpy(float)
    ema_exit = exit_signal(features).fillna(False).to_numpy(bool)
    enter = entries.to_numpy(bool)
    score = scores.to_numpy(int)
    idx = features.index
    n = len(features)
    trades: list[Trade] = []

    i = 0
    while i < n - 1:
        if not enter[i] or not np.isfinite(atr[i]) or atr[i] <= 0:
            i += 1
            continue
        signal_i, j = i, i + 1
        entry = o[j] * (1 + costs.slip)
        risk = params.stop_atr_mult * atr[signal_i]
        stop = entry - risk
        if stop <= 0:
            i += 1
            continue
        t1, t2 = entry + 1.5 * risk, entry + 3.0 * risk
        remaining = 1.0
        fills: list[tuple[float, float]] = []  # (fraction, price)
        highest = c[j]
        reason = ""
        k = j
        while k < n:
            # 1) stop first (worst case inside the bar)
            if lo[k] <= stop:
                price = min(o[k], stop) if k > j else stop
                fills.append((remaining, price * (1 - costs.slip)))
                remaining, reason = 0.0, "stop"
                break
            # 2) targets
            if remaining == 1.0 and h[k] >= t1:
                fills.append((0.5, t1 * (1 - costs.slip)))
                remaining = 0.5
            if remaining > 0 and h[k] >= t2:
                fills.append((remaining, t2 * (1 - costs.slip)))
                remaining, reason = 0.0, "objetivo 3R"
                break
            # 3) close-based exits fill at the next open
            highest = max(highest, c[k])
            trail = highest - params.trailing_atr_mult * atr[k]
            if c[k] < trail or ema_exit[k]:
                if k + 1 >= n:
                    break
                fills.append((remaining, o[k + 1] * (1 - costs.slip)))
                remaining = 0.0
                reason = "trailing stop" if c[k] < trail else "pierde EMA50"
                k += 1
                break
            k += 1
        if remaining > 0:  # still open at the end of the data: not counted
            break
        exit_avg = sum(f * p for f, p in fills)
        gross = exit_avg - entry
        fees = costs.fee * (entry + exit_avg)
        trades.append(
            Trade(
                pair=pair,
                signal_time=idx[signal_i].to_pydatetime(),
                entry_time=idx[j].to_pydatetime(),
                entry=entry,
                stop=stop,
                exit_time=idx[min(k, n - 1)].to_pydatetime(),
                exit_price=exit_avg,
                r_multiple=(gross - fees) / risk,
                exit_reason=reason,
                bars_held=min(k, n - 1) - j + 1,
                score=int(score[signal_i]),
            )
        )
        i = max(k, signal_i + 1)
    return trades


def backtest_pair(
    pair: str,
    candles: pd.DataFrame,
    benchmark: pd.DataFrame | None,
    params: EngineParams,
    costs: CostModel,
    *,
    allowed: pd.Series | None = None,
) -> list[Trade]:
    """``allowed`` (bool per bar) limits entries to bars where the pair was in the
    universe; exits of a trade already open are not affected."""
    features = compute_features(candles, benchmark)
    scored = score_frame(
        features,
        params.weights,
        min_history=params.min_history,
        enter_threshold=params.enter_threshold,
    )
    entries = scored["enter"]
    if allowed is not None:
        entries = entries & allowed.reindex(features.index, fill_value=False).astype(bool)
    return simulate(pair, features, entries, scored["score"], params, costs)


def limit_concurrent(trades: list[Trade], max_positions: int) -> list[Trade]:
    """Portfolio rule: skip entries while ``max_positions`` trades are already open."""
    accepted: list[Trade] = []
    for t in sorted(trades, key=lambda t: (t.entry_time, -t.score, t.pair)):
        open_now = [a for a in accepted if a.exit_time > t.entry_time]
        if len(open_now) < max_positions:
            accepted.append(t)
    return accepted
