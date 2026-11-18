"""Chart image of the signals app for the README, from synthetic candles (no network).

uv run python docs/demo/demo_chart.py docs/img/signal-chart.png
"""

from __future__ import annotations

import pathlib
import sys

import numpy as np
import pandas as pd

from ha_trading_helper.config import Settings
from ha_trading_helper.publish.chart import render_chart
from ha_trading_helper.strategy.signal import build_signal


def candles(seed: int = 3, n: int = 400) -> pd.DataFrame:
    """A trend with noise, a pullback and a rebound on volume: an ENTRAR setup."""
    rng = np.random.default_rng(seed)
    trend = 140 * np.exp(np.cumsum(rng.normal(0.0012, 0.011, n - 18)))
    pull = trend[-1] * np.cumprod(1 + rng.normal(-0.006, 0.006, 12))
    rebound = pull[-1] * np.cumprod([1.012, 1.018, 1.015, 1.022, 1.02, 1.025])
    close = np.concatenate([trend, pull, rebound])
    open_ = np.concatenate([[close[0]], close[:-1]])
    wick = np.abs(rng.normal(0, 0.006, n))
    vol = rng.uniform(800, 1400, n)
    vol[-3:] *= 2.6
    idx = pd.date_range("2026-08-20", periods=n, freq="4h", tz="UTC", name="ts")
    return pd.DataFrame(
        {
            "open": open_,
            "high": np.maximum(open_, close) * (1 + wick),
            "low": np.minimum(open_, close) * (1 - wick),
            "close": close,
            "volume": vol,
            "volume_quote": vol * close,
        },
        index=idx,
    )


def main() -> None:
    out = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "docs/img/signal-chart.png")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    frame = candles()
    benchmark = candles(seed=9)
    signal = build_signal("SOL-USDC", frame, benchmark, settings)
    if signal is None:
        raise SystemExit("not enough history")
    out.write_bytes(render_chart(signal, frame, benchmark, settings))
    print(out, signal.action.value, signal.score)


if __name__ == "__main__":
    main()
