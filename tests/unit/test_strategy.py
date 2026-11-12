from __future__ import annotations

from datetime import timedelta

import numpy as np
import pandas as pd
import pytest

from ha_trading_helper.config import Position, Settings, Weights
from ha_trading_helper.strategy.explain import fmt, fmt_pct
from ha_trading_helper.strategy.features import compute_features
from ha_trading_helper.strategy.risk import compute_levels
from ha_trading_helper.strategy.scoring import score_frame
from ha_trading_helper.strategy.signal import Action, Validation, build_signal
from tests.conftest import make_candles, uptrend_with_pullback


def settings(**kw: object) -> Settings:
    return Settings(_env_file=None, **kw)  # type: ignore[call-arg]


@pytest.fixture
def bull() -> pd.DataFrame:
    vol = np.full(400, 1000.0)
    vol[-1] = 4000.0
    return make_candles(uptrend_with_pullback(400), volume=vol)


@pytest.fixture
def flat() -> pd.DataFrame:
    rng = np.random.default_rng(7)
    return make_candles(100 + rng.normal(0, 0.3, 400))


@pytest.fixture
def crash() -> pd.DataFrame:
    up = 100 * np.exp(np.linspace(0, 0.6, 360))
    down = up[-1] * np.exp(np.linspace(0, -0.25, 40))
    return make_candles(np.concatenate([up, down]))


def test_clean_uptrend_rebound_is_enter(bull: pd.DataFrame) -> None:
    sig = build_signal("SOL-USDT", bull, None, settings())
    assert sig is not None
    assert sig.action is Action.ENTER, sig.render()
    assert sig.score >= 70
    assert sig.market_ok
    assert sig.levels is not None and sig.levels.stop < sig.close < sig.levels.target1


def test_sideways_market_is_wait(flat: pd.DataFrame) -> None:
    sig = build_signal("XRP-USDT", flat, None, settings())
    assert sig is not None
    assert sig.action is Action.WAIT
    assert sig.score < 70


def test_marked_position_in_crash_is_exit(crash: pd.DataFrame) -> None:
    entry_time = crash.index[-30].to_pydatetime()
    pos = Position(
        pair="ETH-USDT", entry_price=float(crash["close"].iloc[-30]), entry_time=entry_time
    )
    sig = build_signal("ETH-USDT", crash, None, settings(positions=[pos]))
    assert sig is not None
    assert sig.action is Action.EXIT
    assert sig.exit_reason and "trailing stop" in sig.exit_reason
    assert sig.alert  # exits always alert
    assert sig.levels is None


def test_exit_on_ema50_loss_when_trailing_is_far(crash: pd.DataFrame) -> None:
    # Entry inside the decline: highest close is close to the current one, so the
    # trailing stop is not hit, but price stays under the EMA 50.
    pos = Position(
        pair="ETH-USDT",
        entry_price=float(crash["close"].iloc[-2]),
        entry_time=crash.index[-2].to_pydatetime(),
    )
    sig = build_signal("ETH-USDT", crash, None, settings(positions=[pos]))
    assert sig is not None and sig.action is Action.EXIT
    assert sig.exit_reason and "EMA50" in sig.exit_reason


def test_position_in_the_future_is_ignored(bull: pd.DataFrame) -> None:
    pos = Position(
        pair="SOL-USDT",
        entry_price=1.0,
        entry_time=bull.index[-1].to_pydatetime() + timedelta(days=1),
    )
    sig = build_signal("SOL-USDT", bull, None, settings(positions=[pos]))
    assert sig is not None and sig.action is Action.WAIT


def test_market_filter_caps_at_wait(bull: pd.DataFrame, crash: pd.DataFrame) -> None:
    sig = build_signal("SOL-USDT", bull, crash, settings())
    assert sig is not None
    assert not sig.market_ok
    assert sig.action is Action.WAIT
    assert "bajo" in sig.market


def test_not_enough_history_returns_none(bull: pd.DataFrame) -> None:
    assert build_signal("SOL-USDT", bull.iloc[:200], None, settings()) is None
    assert build_signal("SOL-USDT", bull.iloc[:0], None, settings()) is None


def test_reasons_add_up_to_score(bull: pd.DataFrame, flat: pd.DataFrame) -> None:
    weights = Weights()
    assert weights.total() == 100
    for frame in (bull, flat):
        sig = build_signal("X-USDT", frame, None, settings())
        assert sig is not None
        assert sum(r.points for r in sig.reasons) == sig.score
        assert {r.key for r in sig.reasons} == set(weights.model_dump())
        for r in sig.reasons:
            assert r.points in (0, r.max_points)
            assert r.detail


def test_alert_requires_positive_validation(bull: pd.DataFrame) -> None:
    from datetime import UTC, datetime

    good = Validation(trades=50, win_rate=0.45, expectancy_r=0.2, profit_factor=1.4,
                      max_drawdown_r=5, threshold=70, generated_at=datetime.now(UTC))  # fmt: skip
    bad = good.model_copy(update={"expectancy_r": -0.1})
    assert not build_signal("S-USDT", bull, None, settings()).alert  # type: ignore[union-attr]
    assert build_signal("S-USDT", bull, None, settings(), validation=good).alert  # type: ignore[union-attr]
    assert not build_signal("S-USDT", bull, None, settings(), validation=bad).alert  # type: ignore[union-attr]
    loose = settings(require_validation=False)
    assert build_signal("S-USDT", bull, None, loose).alert  # type: ignore[union-attr]
    assert "esperanza +0,20R" in good.text()
    assert "-0,10R" in bad.text()


def test_render_contains_every_section(bull: pd.DataFrame) -> None:
    sig = build_signal("SOL-USDT", bull, None, settings())
    assert sig is not None
    text = sig.render()
    for part in (
        "SOL-USDT · ENTRAR",
        "Motivos:",
        "Tendencia",
        "Mercado:",
        "Niveles:",
        "Validación",
    ):
        assert part in text
    assert len(sig.top_reasons(3)) == 3


def test_no_look_ahead_in_scores(bull: pd.DataFrame) -> None:
    """The score at t is the same whether or not later candles exist."""
    full = score_frame(compute_features(bull, None), Weights(), min_history=250, enter_threshold=70)
    for cut in (260, 300, 350, 399):
        part = bull.iloc[:cut]
        sig = build_signal("S-USDT", part, None, settings())
        assert sig is not None
        assert sig.score == int(full["score"].iloc[cut - 1])


def test_trend_is_required_to_enter(flat: pd.DataFrame) -> None:
    """Points from the other evidences never open an entry below the trend."""
    features = compute_features(flat, None)
    no_trend = Weights(trend=0, momentum=20, rsi=15, breakout=15, volume=10, relative_strength=40)
    scored = score_frame(features, no_trend, min_history=250, enter_threshold=1)
    assert (scored["enter"] <= scored["trend_ok"]).all()
    below_trend = scored["history_ok"] & scored["market_ok"] & ~scored["trend_ok"]
    assert (below_trend & (scored["score"] >= 1)).any()
    assert not scored.loc[below_trend, "enter"].any()


def test_benchmark_relative_strength(bull: pd.DataFrame, flat: pd.DataFrame) -> None:
    sig = build_signal("SOL-USDT", bull, flat, settings())
    assert sig is not None
    rs = next(r for r in sig.reasons if r.key == "relative_strength")
    assert rs.points == 15
    assert "vs BTC" in rs.detail


def test_levels_and_size() -> None:
    lv = compute_levels(100.0, 2.0, capital=10_000, risk_pct=1, max_positions=3)
    assert lv.stop == 96.0
    assert lv.target1 == 106.0 and lv.target2 == 112.0
    assert lv.size == pytest.approx(25.0)  # 100 risk / 4 per unit
    assert lv.risk_amount == pytest.approx(100.0)
    capped = compute_levels(100.0, 0.1, capital=10_000, risk_pct=1, max_positions=3)
    assert capped.notional == pytest.approx(10_000 / 3)
    with pytest.raises(ValueError):
        compute_levels(0, 1, capital=1, risk_pct=1, max_positions=1)


def test_number_formatting() -> None:
    assert fmt(73205.4) == "73.205,4"
    assert fmt(3.3456) == "3,35"
    assert fmt(0.0512) == "0,0512"
    assert fmt(0.00000525) == "0,00000525"
    assert fmt(float("nan")) == "n/d"
    assert fmt_pct(0.094) == "+9,4 %"
    assert fmt_pct(-0.02) == "-2,0 %"


def test_render_exit_signal(crash: pd.DataFrame) -> None:
    pos = Position(
        pair="ETH-USDT",
        entry_price=float(crash["close"].iloc[-30]),
        entry_time=crash.index[-30].to_pydatetime(),
    )
    sig = build_signal("ETH-USDT", crash, None, settings(positions=[pos]))
    assert sig is not None
    text = sig.render()
    assert "Salida: cierre" in text and "Niveles" not in text


def test_describe_unknown_key(bull: pd.DataFrame) -> None:
    from ha_trading_helper.strategy.explain import describe

    with pytest.raises(KeyError):
        describe("nope", compute_features(bull, None), 0, False)
