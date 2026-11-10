"""Vectorised scoring shared by live signals and the backtest."""

from __future__ import annotations

import pandas as pd

from ha_trading_helper.config import Weights
from ha_trading_helper.strategy.rules import EVIDENCES, history_ok, market_ok


def score_frame(
    features: pd.DataFrame, weights: Weights, *, min_history: int, enter_threshold: int
) -> pd.DataFrame:
    """Return per-bar points for each evidence, the total score and the entry flag."""
    w = weights.model_dump()
    out = pd.DataFrame(index=features.index)
    for ev in EVIDENCES:
        passed = ev.condition(features).fillna(False).astype(bool)
        out[f"pts_{ev.key}"] = passed.astype(int) * int(w[ev.key])
    out["score"] = out[[c for c in out.columns if c.startswith("pts_")]].sum(axis=1)
    out["history_ok"] = history_ok(features, min_history).fillna(False).astype(bool)
    out["market_ok"] = market_ok(features).fillna(False).astype(bool)
    out["enter"] = out["history_ok"] & out["market_ok"] & (out["score"] >= enter_threshold)
    return out
