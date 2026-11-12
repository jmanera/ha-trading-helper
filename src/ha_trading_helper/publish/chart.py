"""Candlestick chart with the indicators the rules read, as a PNG for Home Assistant.

Four panels share the time axis: price (with the EMAs, the breakout level and the
levels of an ENTRAR), volume, RSI and the MACD histogram. Every panel carries a
one-line caption so the picture can be read next to the plain explanation.
"""

from __future__ import annotations

import io

import matplotlib

matplotlib.use("Agg")  # no display in the container

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.axes import Axes

from ha_trading_helper.config import Settings
from ha_trading_helper.strategy.actions import Action
from ha_trading_helper.strategy.explain import fmt
from ha_trading_helper.strategy.features import (
    BREAKOUT_PERIOD,
    EMA_FAST,
    EMA_SLOW,
    VOLUME_PERIOD,
    compute_features,
)
from ha_trading_helper.strategy.plain import span
from ha_trading_helper.strategy.rules import RSI_ZONE, VOLUME_SPIKE
from ha_trading_helper.strategy.scoring import score_frame
from ha_trading_helper.strategy.signal import Signal

WINDOW = 120  # candles shown
UP, DOWN = "#1a9850", "#d73027"
FAST_COLOR, SLOW_COLOR, VOLUME_COLOR = "#2166ac", "#762a83", "#f46d43"


def _candles(ax: Axes, f: pd.DataFrame) -> None:
    x = np.arange(len(f))
    color = np.where(f["close"] >= f["open"], UP, DOWN)
    ax.vlines(x, f["low"], f["high"], color=color, linewidth=0.8)
    bottom = np.minimum(f["open"], f["close"])
    # A flat candle still gets a visible body.
    height = np.maximum((f["close"] - f["open"]).abs(), (f["high"] - f["low"]).max() * 0.002)
    ax.bar(x, height, bottom=bottom, width=0.7, color=color, linewidth=0)


def _caption(ax: Axes, text: str) -> None:
    ax.text(0.01, 0.82, text, transform=ax.transAxes, fontsize=7, color="#333")


def render_chart(
    signal: Signal,
    candles: pd.DataFrame,
    benchmark: pd.DataFrame | None,
    settings: Settings,
    *,
    window: int = WINDOW,
) -> bytes:
    """PNG of the last ``window`` closed candles that produced ``signal``."""
    features = compute_features(candles, benchmark)
    scored = score_frame(
        features,
        settings.weights,
        min_history=settings.min_history,
        enter_threshold=settings.enter_threshold,
    )
    f, sc = features.iloc[-window:], scored.iloc[-window:]
    x = np.arange(len(f))
    bar = settings.bar

    fig, axes = plt.subplots(
        4, 1, figsize=(10, 9), sharex=True, gridspec_kw={"height_ratios": [4, 1, 1.2, 1.2]}
    )
    ax, axv, axr, axm = axes

    _candles(ax, f)
    ax.plot(x, f["ema50"], color=FAST_COLOR, lw=1.4,
            label=f"Media de {EMA_FAST} velas ({span(EMA_FAST, bar)})")  # fmt: skip
    ax.plot(x, f["ema200"], color=SLOW_COLOR, lw=1.4,
            label=f"Media de {EMA_SLOW} velas ({span(EMA_SLOW, bar)})")  # fmt: skip
    ax.plot(x, f["donchian_high"], color="#555", lw=1, ls="--",
            label=f"Máximo de {BREAKOUT_PERIOD} velas")  # fmt: skip
    enters = np.flatnonzero(sc["enter"].to_numpy())
    if len(enters):
        ax.scatter(enters, f["low"].iloc[enters] * 0.985, marker="^", color=UP, s=40,
                   zorder=5, label="Velas con señal ENTRAR")  # fmt: skip
    if signal.action is Action.ENTER and signal.levels is not None:
        lv = signal.levels
        for y, color, name in ((lv.stop, DOWN, "stop"), (lv.target1, UP, "objetivo 1"),
                               (lv.target2, UP, "objetivo 2")):  # fmt: skip
            ax.axhline(y, color=color, lw=0.9, ls=":", xmin=0.8)
            ax.annotate(f"{name} {fmt(y)}", (1, y), xycoords=("axes fraction", "data"),
                        xytext=(-4, 3), textcoords="offset points", ha="right",
                        fontsize=7, color=color)  # fmt: skip
    ax.annotate("última vela", (len(f) - 1, f["high"].iloc[-1]), xytext=(-60, 25),
                textcoords="offset points", fontsize=8,
                arrowprops={"arrowstyle": "->", "color": "#333"})  # fmt: skip
    ax.set_title(
        f"{signal.pair} · {signal.action.value} · {signal.score}/100 · velas de {bar}",
        loc="left", fontsize=12, fontweight="bold",
    )  # fmt: skip
    ax.legend(loc="upper left", fontsize=7, frameon=False)
    ax.grid(alpha=0.2)

    volume = candles["volume"].astype(float)
    normal = volume.shift(1).rolling(VOLUME_PERIOD).mean()
    spike = (f["rel_volume"] >= VOLUME_SPIKE).to_numpy()
    axv.bar(x, volume.iloc[-window:], color=np.where(spike, VOLUME_COLOR, "#bbb"), width=0.8)
    axv.plot(x, normal.iloc[-window:] * VOLUME_SPIKE, color=VOLUME_COLOR, lw=0.8, ls="--")
    axv.set_ylabel("Volumen", fontsize=8)
    _caption(axv, f"naranja = más de {fmt(VOLUME_SPIKE, 1)} veces lo normal")

    axr.plot(x, f["rsi"], color="#4d4d4d", lw=1)
    axr.axhspan(*RSI_ZONE, color="#fee090", alpha=0.6)
    axr.set_ylim(10, 90)
    axr.set_ylabel("RSI", fontsize=8)
    _caption(axr, "zona amarilla = descanso tras subir")

    hist = f["macd_hist"]
    axm.bar(x, hist, color=np.where(hist > 0, UP, DOWN), width=0.8)
    axm.axhline(0, color="#999", lw=0.6)
    axm.set_ylabel("MACD", fontsize=8)
    _caption(axm, "verde = la subida gana fuerza · rojo = la pierde")

    ticks = x[:: max(1, len(x) // 8)]
    axm.set_xticks(ticks, [f.index[i].strftime("%d/%m") for i in ticks])
    for a in axes:
        a.tick_params(labelsize=8)
        a.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100)
    plt.close(fig)
    return buf.getvalue()
