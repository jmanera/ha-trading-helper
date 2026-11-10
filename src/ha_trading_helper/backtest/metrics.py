"""Summary statistics of a list of trades, in R multiples."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from ha_trading_helper.backtest.engine import Trade


@dataclass(frozen=True)
class Metrics:
    trades: int
    win_rate: float
    expectancy_r: float
    profit_factor: float | None
    total_r: float
    max_drawdown_r: float
    avg_bars_held: float

    def as_dict(self) -> dict[str, float | int | None]:
        return asdict(self)


def compute_metrics(trades: list[Trade]) -> Metrics:
    if not trades:
        return Metrics(0, 0.0, 0.0, None, 0.0, 0.0, 0.0)
    ordered = sorted(trades, key=lambda t: t.exit_time)
    rs = [t.r_multiple for t in ordered]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    gross_loss = -sum(losses)
    equity = peak = drawdown = 0.0
    for r in rs:
        equity += r
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return Metrics(
        trades=len(rs),
        win_rate=len(wins) / len(rs),
        expectancy_r=sum(rs) / len(rs),
        profit_factor=(sum(wins) / gross_loss) if gross_loss > 0 else None,
        total_r=sum(rs),
        max_drawdown_r=drawdown,
        avg_bars_held=sum(t.bars_held for t in ordered) / len(ordered),
    )
