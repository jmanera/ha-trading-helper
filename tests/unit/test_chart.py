from __future__ import annotations

import pandas as pd

from ha_trading_helper.config import Settings
from ha_trading_helper.publish.chart import render_chart
from ha_trading_helper.strategy.actions import Action
from ha_trading_helper.strategy.signal import build_signal
from tests.unit.test_strategy import bull, flat  # noqa: F401  (fixtures)

PNG = b"\x89PNG\r\n\x1a\n"


def settings(**kw: object) -> Settings:
    return Settings(_env_file=None, **kw)  # type: ignore[call-arg]


def test_chart_of_an_entry_is_a_png(bull: pd.DataFrame) -> None:  # noqa: F811
    s = settings()
    sig = build_signal("SOL-USDT", bull, None, s)
    assert sig is not None and sig.action is Action.ENTER
    png = render_chart(sig, bull, None, s)
    assert png.startswith(PNG) and len(png) > 20_000


def test_chart_of_a_wait_and_short_history(flat: pd.DataFrame) -> None:  # noqa: F811
    s = settings(bar="1D")
    sig = build_signal("XRP-USDT", flat, flat, s)
    assert sig is not None and sig.action is Action.WAIT
    assert render_chart(sig, flat, flat, s, window=30).startswith(PNG)
    # Fewer candles than the window, and no ENTRAR anywhere in it.
    short = flat.iloc[-260:]
    sig = build_signal("XRP-USDT", short, None, s)
    assert sig is not None
    assert render_chart(sig, short, None, s, window=500).startswith(PNG)
