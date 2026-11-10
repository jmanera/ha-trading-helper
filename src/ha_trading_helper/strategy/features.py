"""Indicator columns the rules read. Pure: no network, no clock."""

from __future__ import annotations

import pandas as pd

from ha_trading_helper import indicators as ind

EMA_FAST = 50
EMA_SLOW = 200
RS_PERIOD = 30
BREAKOUT_PERIOD = 20
VOLUME_PERIOD = 20
ATR_PERIOD = 14
RSI_PERIOD = 14


def compute_features(candles: pd.DataFrame, benchmark: pd.DataFrame | None) -> pd.DataFrame:
    """Return one row per candle with every value a rule or an explanation needs.

    ``benchmark`` is the BTC candle frame; when it is None (or the pair *is* the
    benchmark) the market filter uses the pair itself and relative strength is 0.
    """
    close = candles["close"].astype(float)
    feat = pd.DataFrame(index=candles.index)
    feat["open"] = candles["open"].astype(float)
    feat["high"] = candles["high"].astype(float)
    feat["low"] = candles["low"].astype(float)
    feat["close"] = close
    feat["ema50"] = ind.ema(close, EMA_FAST)
    feat["ema200"] = ind.ema(close, EMA_SLOW)
    feat["rsi"] = ind.rsi(close, RSI_PERIOD)
    feat["macd_hist"] = ind.macd(close)["hist"]
    feat["atr"] = ind.atr(feat["high"], feat["low"], close, ATR_PERIOD)
    feat["donchian_high"] = ind.donchian_high(feat["high"], BREAKOUT_PERIOD)
    feat["rel_volume"] = ind.relative_volume(candles["volume"].astype(float), VOLUME_PERIOD)
    feat["ret30"] = ind.pct_return(close, RS_PERIOD)
    feat["bars"] = range(1, len(feat) + 1)

    if benchmark is not None and not benchmark.empty:
        bclose = benchmark["close"].astype(float).reindex(candles.index).ffill()
        bema = ind.ema(benchmark["close"].astype(float), EMA_SLOW).reindex(candles.index).ffill()
        feat["btc_close"] = bclose
        feat["btc_ema200"] = bema
        feat["btc_ret30"] = ind.pct_return(benchmark["close"].astype(float), RS_PERIOD).reindex(
            candles.index
        )
    else:
        feat["btc_close"] = close
        feat["btc_ema200"] = feat["ema200"]
        feat["btc_ret30"] = feat["ret30"]
    return feat
