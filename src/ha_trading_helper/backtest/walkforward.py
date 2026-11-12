"""Walk-forward validation of the entry threshold.

The weights are fixed; the only free parameter is the entry threshold. For each
test window we pick the threshold with the best expectancy on all *earlier* data
(train), then record the trades of the test window with that threshold. The
out-of-sample trades of every window together are the strategy's validation.
"""

from __future__ import annotations

import itertools
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from ha_trading_helper.backtest.engine import (
    CostModel,
    EngineParams,
    Trade,
    backtest_pair,
    limit_concurrent,
)
from ha_trading_helper.backtest.metrics import Metrics, compute_metrics
from ha_trading_helper.strategy.signal import Validation

DEFAULT_THRESHOLDS = (60, 70, 80)
MIN_TRAIN_TRADES = 10


@dataclass
class Fold:
    train_end: datetime
    test_end: datetime
    threshold: int
    train: Metrics
    test: Metrics


@dataclass
class WalkForwardResult:
    folds: list[Fold]
    oos: Metrics
    oos_trades: list[Trade]
    full_sample: dict[int, Metrics] = field(default_factory=dict)

    def to_validation(self) -> Validation:
        threshold = self.folds[-1].threshold if self.folds else 0
        return Validation(
            trades=self.oos.trades,
            win_rate=self.oos.win_rate,
            expectancy_r=self.oos.expectancy_r,
            profit_factor=self.oos.profit_factor,
            max_drawdown_r=self.oos.max_drawdown_r,
            threshold=threshold,
            generated_at=datetime.now(UTC),
        )


def _between(trades: list[Trade], start: datetime | None, end: datetime) -> list[Trade]:
    return [t for t in trades if (start is None or t.entry_time >= start) and t.entry_time < end]


def walk_forward(
    data: dict[str, pd.DataFrame],
    benchmark: pd.DataFrame | None,
    base: EngineParams,
    costs: CostModel,
    *,
    thresholds: tuple[int, ...] = DEFAULT_THRESHOLDS,
    folds: int = 4,
    max_positions: int = 3,
    members: pd.DataFrame | None = None,
) -> WalkForwardResult:
    """``members`` (bars x pairs, bool) is the universe at each bar; None = every pair
    in ``data`` may enter at any time."""
    # Indicators only look backwards, so one pass per threshold over the full
    # history gives the same trades a fold-by-fold run would.
    by_threshold: dict[int, list[Trade]] = {}
    for th in thresholds:
        params = EngineParams(
            enter_threshold=th,
            min_history=base.min_history,
            stop_atr_mult=base.stop_atr_mult,
            trailing_atr_mult=base.trailing_atr_mult,
            weights=base.weights,
        )
        trades: list[Trade] = []
        for pair, candles in data.items():
            allowed = None
            if members is not None:
                allowed = members[pair] if pair in members else pd.Series(dtype=bool)
            trades += backtest_pair(pair, candles, benchmark, params, costs, allowed=allowed)
        by_threshold[th] = limit_concurrent(trades, max_positions)

    starts = [f.index[0] for f in data.values() if not f.empty]
    ends = [f.index[-1] for f in data.values() if not f.empty]
    if not starts:
        return WalkForwardResult([], compute_metrics([]), [])
    start, end = min(starts), max(ends)
    # First half of the window budget is training-only; the rest is split in folds.
    edges = pd.date_range(start, end, periods=folds + 2)[1:]

    result_folds: list[Fold] = []
    oos: list[Trade] = []
    for train_end, test_end in itertools.pairwise(edges):
        best_th, best_m = thresholds[0], None
        for th in thresholds:
            m = compute_metrics(_between(by_threshold[th], None, train_end))
            ok = m.trades >= MIN_TRAIN_TRADES
            if ok and (best_m is None or m.expectancy_r > best_m.expectancy_r):
                best_th, best_m = th, m
        if best_m is None:  # not enough trades anywhere: use the configured threshold
            best_th = base.enter_threshold if base.enter_threshold in by_threshold else best_th
            best_m = compute_metrics(_between(by_threshold[best_th], None, train_end))
        last = test_end == edges[-1]
        test_trades = _between(
            by_threshold[best_th],
            train_end,
            test_end + pd.Timedelta(seconds=1) if last else test_end,
        )
        oos += test_trades
        result_folds.append(
            Fold(
                train_end=train_end.to_pydatetime(),
                test_end=test_end.to_pydatetime(),
                threshold=best_th,
                train=best_m,
                test=compute_metrics(test_trades),
            )
        )
    return WalkForwardResult(
        folds=result_folds,
        oos=compute_metrics(oos),
        oos_trades=oos,
        full_sample={th: compute_metrics(t) for th, t in by_threshold.items()},
    )


def save_report(result: WalkForwardResult, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "validation": result.to_validation().model_dump(mode="json"),
        "oos": result.oos.as_dict(),
        "folds": [
            {
                "train_end": f.train_end.isoformat(),
                "test_end": f.test_end.isoformat(),
                "threshold": f.threshold,
                "train": f.train.as_dict(),
                "test": f.test.as_dict(),
            }
            for f in result.folds
        ],
        "full_sample": {str(k): v.as_dict() for k, v in result.full_sample.items()},
    }
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")


def load_validation(path: Path) -> Validation | None:
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return Validation.model_validate(data["validation"])
