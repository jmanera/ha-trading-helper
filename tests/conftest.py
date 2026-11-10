from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def make_candles(
    closes: np.ndarray | list[float],
    *,
    start: str = "2024-01-01",
    spread: float = 0.01,
    volume: float | np.ndarray = 1000.0,
) -> pd.DataFrame:
    """Candles whose open is the previous close and high/low wrap the body by ``spread``."""
    close = np.asarray(closes, dtype=float)
    open_ = np.concatenate([[close[0]], close[:-1]])
    high = np.maximum(open_, close) * (1 + spread)
    low = np.minimum(open_, close) * (1 - spread)
    vol = np.full(len(close), volume, dtype=float) if np.isscalar(volume) else volume
    idx = pd.date_range(start, periods=len(close), freq="4h", tz="UTC", name="ts")
    return pd.DataFrame(
        {
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": vol,
            "volume_quote": vol * close,
        },
        index=idx,
    )


def uptrend_with_pullback(n: int = 400, seed: int = 1) -> np.ndarray:
    """Steady uptrend, a pullback near the end, then a sharp rebound on the last bars."""
    rng = np.random.default_rng(seed)
    base = 100 * np.exp(np.linspace(0, 0.8, n - 15)) * (1 + rng.normal(0, 0.002, n - 15))
    pull = base[-1] * np.linspace(1, 0.94, 10)
    rebound = pull[-1] * np.array([1.02, 1.05, 1.09, 1.13, 1.17])
    return np.concatenate([base, pull, rebound])


@pytest.fixture
def fixture_json() -> object:
    def load(name: str) -> object:
        return json.loads((FIXTURES / name).read_text())

    return load
