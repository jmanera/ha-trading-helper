"""One monitor cycle after every 5 min close: candles -> paper trades -> alerts -> MQTT.

The measurement only moves forward: a pair seen for the first time starts at its
latest closed candle, never with trades on its past. After a restart or a network cut
the missed candles are downloaded from OKX and processed in order, so the paper
trades see exactly the candles they would have seen live; the cut is still recorded.
"""

from __future__ import annotations

import asyncio
import random
import signal
from datetime import UTC, datetime, timedelta

import pandas as pd
import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from ha_trading_helper.data.store import candles_to_frame
from ha_trading_helper.intraday import payloads
from ha_trading_helper.intraday.alerts import (
    Alert,
    Cooldown,
    btc_regime,
    fifteen_minute,
    five_minute,
)
from ha_trading_helper.intraday.config import IntradaySettings
from ha_trading_helper.intraday.paper import Trade, stats, verdict
from ha_trading_helper.intraday.rules import (
    BAR_MINUTES,
    CONTROL_A,
    CONTROL_B,
    CONTROL_SEED,
    RULE_A,
    RULE_B,
    RULES,
    RULES_BY_KEY,
    RULES_VERSION,
    Rule,
    align,
    duration,
    features,
    hourly_trend,
    signal_a,
    signal_b,
)
from ha_trading_helper.intraday.store import MeasurementStore
from ha_trading_helper.okx.client import RECENT_LIMIT, OkxClient, OkxError
from ha_trading_helper.publish.mqtt import MqttPublisher
from ha_trading_helper.publish.payloads import Message
from ha_trading_helper.service import CONCURRENCY, resolve_universe

log = structlog.get_logger(__name__)

BARS = ("5m", "15m", "1H")
HISTORY = RECENT_LIMIT  # candles kept per series: enough warm-up for every indicator
MAX_CATCH_UP = 3000  # candles fetched at most to cover a long outage
UNIVERSE_TTL = timedelta(hours=1)
FRESH_ALERT = timedelta(minutes=15)  # older candles (catch-up) never raise alerts
GAP_AFTER = timedelta(minutes=15)
CONTROLS = {RULE_A.key: CONTROL_A, RULE_B.key: CONTROL_B}


def floor_time(now: datetime, bar: str) -> datetime:
    minutes = BAR_MINUTES[bar]
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    step = (now - midnight) // timedelta(minutes=minutes)
    return midnight + step * timedelta(minutes=minutes)


def last_closed_open(now: datetime, bar: str) -> datetime:
    """Open time of the newest candle of ``bar`` that has closed at ``now``."""
    return floor_time(now, bar) - duration(bar)


def control_pick(rule: Rule, pair: str, when: datetime, candidates: list[str]) -> str | None:
    """Random pair for the control of ``rule``: reproducible from the rule, pair and time."""
    if not candidates:
        return None
    # A reproducible draw, not security: the same inputs always pick the same pair.
    seed = f"{CONTROL_SEED}-{rule.key}-{pair}-{when.isoformat()}"
    rng = random.Random(seed)  # noqa: S311  # nosec B311
    return rng.choice(sorted(candidates))


class Monitor:
    def __init__(self, settings: IntradaySettings, store: MeasurementStore | None = None) -> None:
        self.settings = settings
        self.store = store or MeasurementStore(settings.data_dir / "intraday.db")
        self.publisher = MqttPublisher(settings)
        self.alert_settings = settings.alert_settings()
        self.cooldown = Cooldown(self.alert_settings.cooldown)
        self.open: dict[tuple[str, str], Trade] = {
            (t.rule, t.pair): t for t in self.store.trades(open_only=True)
        }
        # Latest 1H candles per pair: the hourly trend is needed in every 5 min cycle.
        self.hourly: dict[str, pd.DataFrame] = {}
        self.universe: list[str] = []
        self.universe_at: datetime | None = None
        self.last_cycle: datetime | None = None
        self.errors: list[str] = []
        self._discovered = False
        self._lock = asyncio.Lock()
        for a in self.store.alerts(datetime.now(UTC) - self.alert_settings.cooldown, 1000):
            self.cooldown.allow(a)

    # data -----------------------------------------------------------------------------
    async def refresh_universe(self, client: OkxClient, now: datetime) -> None:
        if self.universe_at is not None and now - self.universe_at < UNIVERSE_TTL:
            return
        try:
            self.universe = await resolve_universe(self.settings, client)
            self.universe_at = now
        except OkxError as exc:
            log.warning("universe_failed", error=str(exc))
            self.errors.append(f"universo: {exc}")

    def pairs(self) -> list[str]:
        """Universe, plus pairs with open trades, plus the benchmark."""
        held = {pair for _, pair in self.open}
        return list(dict.fromkeys([self.settings.benchmark, *self.universe, *sorted(held)]))

    async def fetch(
        self, client: OkxClient, pair: str, bar: str, now: datetime
    ) -> pd.DataFrame | None:
        last = self.store.last_open(pair, bar)
        newest = last_closed_open(now, bar)
        cached = bar != "1H" or pair in self.hourly
        if last is not None and last >= newest and cached:
            return None  # nothing new for this series
        missing = 0 if last is None else int((newest - last) / duration(bar))
        count = min(HISTORY + missing, MAX_CATCH_UP)
        try:
            if count <= RECENT_LIMIT:
                candles = await client.recent_candles(pair, bar, count)
            else:
                candles = await client.candles(pair, bar, count=count)
        except OkxError as exc:
            log.warning("candles_failed", pair=pair, bar=bar, error=str(exc))
            self.errors.append(f"{pair} {bar}: {exc}")
            return None
        frame = candles_to_frame(candles)
        return frame[frame.index <= newest]

    # measurement ----------------------------------------------------------------------
    def _step_series(
        self, rule: Rule, feats: dict[str, pd.DataFrame], signals: dict[str, pd.Series]
    ) -> None:
        """Advance ``rule`` and its control over the new candles of every pair, in time order."""
        control = CONTROLS[rule.key]
        new: dict[str, pd.DataFrame] = {}
        for pair, f in feats.items():
            last = self.store.last_open(pair, rule.bar)
            if last is None:  # first sight: the measurement starts after this candle
                if not f.empty:
                    self.store.set_last_open(pair, rule.bar, f.index[-1])
                continue
            new[pair] = f[f.index > last]
        cols = ["open", "high", "low", "close", "atr", "bb_mid"]
        rows = {p: f[cols].astype(float).to_dict("index") for p, f in new.items()}
        fired = {p: signals[p].reindex(f.index, fill_value=False).to_dict() for p, f in new.items()}
        for ts in sorted({t for f in new.values() for t in f.index}):
            when = ts.to_pydatetime()
            here = [p for p in new if ts in rows[p]]
            for pair in here:
                bar = {str(k): float(v) for k, v in rows[pair][ts].items()}
                for key in (rule.key, control.key):
                    trade = self.open.get((key, pair))
                    if trade is not None:
                        trade.step(RULES_BY_KEY[key], when, bar)
                        self._persist(trade)
            for pair in here:
                atr = float(rows[pair][ts]["atr"])
                if not fired[pair][ts] or not atr > 0 or (rule.key, pair) in self.open:
                    continue
                self._open(Trade(rule.key, pair, when, atr))
                candidates = [
                    p for p in here
                    if (control.key, p) not in self.open and float(rows[p][ts]["atr"]) > 0
                ]  # fmt: skip
                picked = control_pick(rule, pair, when, candidates)
                if picked is not None:
                    atr_c = float(rows[picked][ts]["atr"])
                    self._open(Trade(control.key, picked, when, atr_c))
        for pair, f in new.items():
            if not f.empty:
                self.store.set_last_open(pair, rule.bar, f.index[-1])

    def _open(self, trade: Trade) -> None:
        self.open[(trade.rule, trade.pair)] = trade
        self.store.save_trade(trade)

    def _persist(self, trade: Trade) -> None:
        self.store.save_trade(trade)
        if trade.closed:
            self.open.pop((trade.rule, trade.pair), None)

    def measure(self, frames: dict[tuple[str, str], pd.DataFrame]) -> None:
        bench = self.settings.benchmark
        for (pair, bar), frame in frames.items():
            if bar == "1H":
                self.hourly[pair] = frame
        hourly = self.hourly
        btc_up = hourly_trend(hourly[bench]) if bench in hourly else pd.Series(dtype=bool)
        for rule in (RULE_A, RULE_B):
            feats, signals = {}, {}
            for (pair, bar), frame in frames.items():
                if bar != rule.bar or frame.empty:
                    continue
                f = features(frame)
                if rule is RULE_A:
                    sig = signal_a(f, align(btc_up, frame, bar))
                else:
                    trend = hourly_trend(hourly[pair]) if pair in hourly else pd.Series(dtype=bool)
                    sig = signal_b(f, align(trend, frame, bar))
                feats[pair], signals[pair] = f, sig
            self._step_series(rule, feats, signals)

    # alerts ---------------------------------------------------------------------------
    def alerts(self, frames: dict[tuple[str, str], pd.DataFrame], now: datetime) -> list[Alert]:
        found: list[Alert] = []
        s = self.alert_settings
        for (pair, bar), frame in frames.items():
            if frame.empty or now - (frame.index[-1] + duration(bar)) > FRESH_ALERT:
                continue
            if bar == "5m":
                found += five_minute(pair, frame, s)
            elif bar == "15m":
                found += fifteen_minute(pair, frame, s)
            elif pair == self.settings.benchmark:
                found += btc_regime(pair, frame, s)
        fresh = [a for a in found if self.cooldown.allow(a)]
        for a in fresh:
            self.store.add_alert(a)
        return fresh

    # publishing -----------------------------------------------------------------------
    def state_messages(self, now: datetime, new_alerts: list[Alert]) -> list[Message]:
        base = self.settings.base_topic
        started = self.store.started_at(now)
        msgs: list[Message] = []
        if not self._discovered:
            msgs += payloads.discovery_messages(base, self.settings.discovery_prefix)
        msgs += [payloads.alert_event(a, base) for a in new_alerts]
        msgs += payloads.alerts_messages(
            self.store.alerts(now - timedelta(days=1)),
            self.store.count_alerts(now - timedelta(days=1)),
            base,
        )
        by_rule = {r.key: self.store.trades(r.key) for r in RULES}
        all_stats = {k: stats(v) for k, v in by_rule.items()}
        for rule in RULES:
            v = None
            if rule.control_of is None:
                v = verdict(all_stats[rule.key], all_stats[CONTROLS[rule.key].key], started, now)
            closed = [t for t in by_rule[rule.key] if t.closed]
            attrs = payloads.rule_attributes(rule, all_stats[rule.key], v, started, closed)
            msgs += payloads.rule_messages(rule, attrs, base)
        newest = self.store.last_open(self.settings.benchmark, "5m")
        gaps = self.store.gaps()
        system = {
            "ultima_ejecucion": now.isoformat(),
            "ultima_vela": (newest + duration("5m")).isoformat() if newest else None,
            "pares": self.universe,
            "errores": self.errors[:5],
            "reglas_version": RULES_VERSION,
            "midiendo_desde": started.isoformat(),
            "cortes": len(gaps),
            "ultimo_corte": (
                f"{gaps[-1][0]:%d/%m %H:%M}-{gaps[-1][1]:%d/%m %H:%M} UTC" if gaps else None
            ),
        }
        health = "error" if self.errors and not self.universe else (
            "degradada" if self.errors else "ok"
        )  # fmt: skip
        msgs += payloads.system_messages(health, system, base)
        return msgs

    # cycle ----------------------------------------------------------------------------
    def _record_gap(self, now: datetime) -> None:
        prev = self.last_cycle
        if prev is None:
            newest = self.store.last_open(self.settings.benchmark, "5m")
            prev = newest + 2 * duration("5m") if newest is not None else None
        if prev is not None and now - prev > GAP_AFTER:
            self.store.add_gap(prev, now, "app parada o sin conexión; velas recuperadas de OKX")

    async def cycle(self, now: datetime | None = None) -> list[Alert]:
        async with self._lock:
            now = now or datetime.now(UTC)
            self.errors = []
            self._record_gap(now)
            async with OkxClient() as client:
                await self.refresh_universe(client, now)
                wanted = [(p, b) for p in self.pairs() for b in BARS]
                gate = asyncio.Semaphore(CONCURRENCY)

                async def one(pair: str, bar: str) -> pd.DataFrame | None:
                    async with gate:
                        return await self.fetch(client, pair, bar, now)

                fetched = await asyncio.gather(*(one(p, b) for p, b in wanted))
            frames = {k: f for k, f in zip(wanted, fetched, strict=True) if f is not None}
            self.measure(frames)
            new_alerts = self.alerts(frames, now)
            for (pair, bar), frame in frames.items():
                if bar == "1H" and not frame.empty:
                    self.store.set_last_open(pair, bar, frame.index[-1])
            self.store.started_at(now)
            self.store.commit()
            self.last_cycle = now
            try:
                await self.publisher.send(self.state_messages(now, new_alerts))
                self._discovered = True
            except Exception:
                log.exception("publish_failed")
            log.info(
                "intraday_cycle", pairs=len(self.universe), alerts=len(new_alerts),
                open=len(self.open), errors=len(self.errors),
            )  # fmt: skip
            return new_alerts

    async def safe_cycle(self) -> None:
        try:
            await self.cycle()
        except Exception:
            log.exception("intraday_cycle_failed")

    async def run_forever(self) -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        scheduler = AsyncIOScheduler(timezone=UTC)
        scheduler.add_job(
            self.safe_cycle,
            CronTrigger(minute="*/5", second=self.settings.poll_delay_seconds, timezone=UTC),
            max_instances=1,
            coalesce=True,
            misfire_grace_time=120,
        )
        scheduler.start()
        log.info("intraday_started", rules=RULES_VERSION)
        await self.safe_cycle()
        await stop.wait()
        scheduler.shutdown(wait=False)
        await self.publisher.close()
        self.store.close()
