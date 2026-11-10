"""Short Spanish sentences that say why each evidence did or did not score."""

from __future__ import annotations

import math

import pandas as pd

from ha_trading_helper.strategy.rules import MOMENTUM_LOOKBACK, RSI_PULLBACK_LOOKBACK, VOLUME_SPIKE


def _decimals(value: float) -> int:
    mag = abs(value)
    return 1 if mag >= 100 else 2 if mag >= 1 else 4 if mag >= 0.01 else 8


def fmt(value: float, decimals: int | None = None) -> str:
    """Spanish number format with a precision that suits the magnitude."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "n/d"
    if decimals is None:
        decimals = _decimals(value)
    text = f"{value:,.{decimals}f}"
    return text.replace(",", "_").replace(".", ",").replace("_", ".")


def fmt_pct(value: float) -> str:
    return f"{'+' if value >= 0 else ''}{fmt(value * 100, 1)} %"


def describe(key: str, f: pd.DataFrame, i: int, passed: bool) -> str:
    row = f.iloc[i]
    if key == "trend":
        rel = ">" if passed else "vs"
        name = "alcista" if passed else "no alcista"
        return (
            f"{name}, cierre {fmt(row.close)} {rel} EMA50 {fmt(row.ema50)} "
            f"{rel} EMA200 {fmt(row.ema200)}"
        )
    if key == "momentum":
        window = f["macd_hist"].iloc[max(0, i - MOMENTUM_LOOKBACK) : i + 1]
        before = window.iloc[0] if len(window) else float("nan")
        if passed:
            digits = _decimals(max(abs(before), abs(row.macd_hist)))
            return f"histograma pasa de {fmt(before, digits)} a {fmt(row.macd_hist, digits)}"
        return f"histograma {fmt(row.macd_hist)}, sin cruce alcista reciente"
    if key == "rsi":
        prev = f["rsi"].iloc[max(0, i - RSI_PULLBACK_LOOKBACK) : i]
        low = prev.min() if len(prev) else float("nan")
        if passed:
            return f"sube de {fmt(low, 0)} a {fmt(row.rsi, 0)} tras zona de retroceso"
        return f"en {fmt(row.rsi, 0)} (mín. reciente {fmt(low, 0)}); no sale de 30-45"
    if key == "breakout":
        verb = "supera" if passed else "no supera"
        return f"cierre {verb} el máximo de 20 velas ({fmt(row.donchian_high)})"
    if key == "volume":
        cmp = "≥" if passed else "<"
        return f"{fmt(row.rel_volume, 1)}x la media de 20 velas ({cmp} {fmt(VOLUME_SPIKE, 1)}x)"
    if key == "relative_strength":
        diff = row.ret30 - row.btc_ret30
        return f"{fmt_pct(diff)} vs BTC en 30 velas"
    raise KeyError(key)


def market_text(f: pd.DataFrame, i: int, ok: bool) -> str:
    row = f.iloc[i]
    where = "sobre" if ok else "bajo"
    return f"BTC {where} su EMA200 ({fmt(row.btc_close)} vs {fmt(row.btc_ema200)})"
