"""One analysis cycle: universe -> candles -> signals -> (publish) -> signal log."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import structlog

from ha_trading_helper.backtest.walkforward import load_validation
from ha_trading_helper.config import Settings
from ha_trading_helper.data.store import CandleStore
from ha_trading_helper.data.universe import UniverseRules, select_universe
from ha_trading_helper.okx.client import OkxClient, OkxError
from ha_trading_helper.publish import payloads
from ha_trading_helper.strategy.signal import Action, Signal, build_signal

log = structlog.get_logger(__name__)

# Enough for EMA 200 warm-up plus a margin.
LIVE_HISTORY = 300
CONCURRENCY = 4
BARS_PER_YEAR = {"1H": 24 * 365, "4H": 6 * 365, "1D": 365}


@dataclass
class ScanResult:
    signals: list[Signal]
    universe: list[str]
    errors: list[str] = field(default_factory=list)
    finished_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def ranked(self) -> list[Signal]:
        return payloads.ranking(self.signals)


def validation_path(settings: Settings) -> Path:
    """One report per quote currency: the universe, and so the validation, depends on it."""
    return settings.data_dir / f"validation-{settings.quote_currency.lower()}.json"


async def resolve_universe(settings: Settings, client: OkxClient) -> list[str]:
    if settings.pairs:
        return list(dict.fromkeys(p.upper() for p in settings.pairs))
    tickers = await client.tickers("SPOT")
    rules = UniverseRules(
        quotes=(settings.quote_currency,),
        min_volume_24h_usdt=settings.min_volume_24h_usdt,
        max_spread_pct=settings.max_spread_pct,
        max_pairs=settings.max_pairs,
    )
    eur = next((t.last for t in tickers if t.inst_id == "EUR-USDT"), None)
    instruments = await client.instruments("SPOT")
    allowed = {i.inst_id for i in instruments if i.is_crypto_live}
    pairs = select_universe(tickers, rules, eur_usdt=eur or 1.0, allowed=allowed)
    # Pairs with a marked position are always analysed so SALIR can fire.
    for p in settings.positions:
        if p.pair not in pairs:
            pairs.append(p.pair)
    return pairs


async def scan(
    settings: Settings, client: OkxClient, store: CandleStore | None = None
) -> ScanResult:
    store = store or CandleStore(settings.data_dir)
    errors: list[str] = []
    try:
        universe = await resolve_universe(settings, client)
    except OkxError as exc:
        log.error("universe_failed", error=str(exc))
        return ScanResult([], [], [f"universo: {exc}"])

    semaphore = asyncio.Semaphore(CONCURRENCY)

    async def fetch(pair: str) -> tuple[str, pd.DataFrame | None]:
        async with semaphore:
            try:
                return pair, await store.sync(client, pair, settings.bar, count=LIVE_HISTORY)
            except OkxError as exc:
                log.warning("candles_failed", pair=pair, error=str(exc))
                errors.append(f"{pair}: {exc}")
                return pair, None

    wanted = list(dict.fromkeys([settings.benchmark, *universe]))
    frames = dict(await asyncio.gather(*(fetch(p) for p in wanted)))
    benchmark = frames.get(settings.benchmark)
    validation = load_validation(validation_path(settings))

    signals: list[Signal] = []
    for pair in universe:
        frame = frames.get(pair)
        if frame is None:
            continue
        try:
            sig = build_signal(pair, frame, benchmark, settings, validation=validation)
        except Exception as exc:  # one bad pair must not stop the cycle
            log.exception("signal_failed", pair=pair)
            errors.append(f"{pair}: {exc}")
            continue
        if sig is not None:
            signals.append(sig)
    return ScanResult(signals, universe, errors)


async def load_history(
    settings: Settings,
    client: OkxClient,
    pairs: list[str],
    years: float,
    *,
    progress: Callable[[str], None] | None = None,
) -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    """Candles for a backtest: ``years`` of history plus the indicator warm-up."""
    count = int(BARS_PER_YEAR.get(settings.bar, 2190) * years) + settings.min_history
    store = CandleStore(settings.data_dir)
    if not pairs:
        pairs = await resolve_universe(settings, client)
    benchmark = await store.sync(client, settings.benchmark, settings.bar, count=count)
    data: dict[str, pd.DataFrame] = {}
    for pair in pairs:
        if progress:
            progress(pair)
        try:
            frame = await store.sync(client, pair, settings.bar, count=count)
        except OkxError as exc:
            log.warning("history_failed", pair=pair, error=str(exc))
            continue
        data[pair] = frame.iloc[-count:]
    return data, benchmark.iloc[-count:]


class SignalLog:
    """Append-only record of action changes (phase 7: compare live vs backtest)."""

    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / "signals.jsonl"
        self.state_path = data_dir / "last_actions.json"

    def _last(self) -> dict[str, str]:
        if not self.state_path.is_file():
            return {}
        data: dict[str, str] = json.loads(self.state_path.read_text(encoding="utf-8"))
        return data

    def record(self, signals: list[Signal]) -> list[Signal]:
        """Store signals whose action changed; return them (the ones worth notifying)."""
        last = self._last()
        changed = [s for s in signals if last.get(s.pair) != s.action.value]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            for s in changed:
                if s.action is Action.WAIT and s.pair not in last:
                    continue  # first sighting of a quiet pair is not an event
                row = {
                    "pair": s.pair,
                    "action": s.action.value,
                    "previous": last.get(s.pair),
                    "score": s.score,
                    "close": s.close,
                    "candle_close": s.candle_close.isoformat(),
                    "alert": s.alert,
                    "levels": s.levels.model_dump() if s.levels else None,
                }
                fh.write(json.dumps(row) + "\n")
        last.update({s.pair: s.action.value for s in signals})
        self.state_path.write_text(json.dumps(last), encoding="utf-8")
        return changed
