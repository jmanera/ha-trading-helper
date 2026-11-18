"""Command line: scan, backtest, validate, publish, serve."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Annotated

import pandas as pd
import structlog
import typer

from ha_trading_helper.backtest.engine import CostModel, EngineParams, backtest_pair
from ha_trading_helper.backtest.metrics import Metrics, compute_metrics
from ha_trading_helper.backtest.walkforward import save_report, walk_forward
from ha_trading_helper.config import Settings
from ha_trading_helper.okx.client import OkxClient
from ha_trading_helper.service import (
    ValidationHistory,
    load_history,
    load_validation_history,
    scan,
    validation_path,
)

app = typer.Typer(no_args_is_help=True, add_completion=False, help=__doc__)


def setup_logging(level: str, *, json_logs: bool = False) -> None:
    logging.basicConfig(level=level.upper(), format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    renderer: structlog.typing.Processor = (
        structlog.processors.JSONRenderer() if json_logs else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[level.upper()]
        ),
    )


def _settings() -> Settings:
    settings = Settings()
    setup_logging(settings.log_level)
    return settings


def _engine_params(s: Settings) -> EngineParams:
    return EngineParams(
        enter_threshold=s.enter_threshold,
        min_history=s.min_history,
        stop_atr_mult=s.stop_atr_mult,
        trailing_atr_mult=s.trailing_atr_mult,
        weights=s.weights,
    )


def _print_metrics(title: str, m: Metrics) -> None:
    pf = f"{m.profit_factor:.2f}" if m.profit_factor is not None else "n/d"
    typer.echo(
        f"{title}: n={m.trades} aciertos={m.win_rate:.0%} esperanza={m.expectancy_r:+.3f}R "
        f"total={m.total_r:+.1f}R PF={pf} maxDD={m.max_drawdown_r:.1f}R "
        f"velas/op={m.avg_bars_held:.1f}"
    )


@app.command("scan")
def scan_cmd(
    once: Annotated[bool, typer.Option("--once", help="Run a single cycle (default)")] = True,
    top: Annotated[int, typer.Option(help="Pairs to show in detail")] = 3,
    as_json: Annotated[bool, typer.Option("--json", help="Print signals as JSON")] = False,
) -> None:
    """Score the universe and print the ranking with the reasons of each signal."""
    settings = _settings()

    async def run() -> None:
        async with OkxClient() as client:
            result = await scan(settings, client)
        ranked = result.ranked()
        if as_json:
            typer.echo(json.dumps([s.model_dump(mode="json") for s in ranked], indent=2))
            return
        typer.echo(f"Ranking ({len(ranked)} pares):")
        for n, s in enumerate(ranked, 1):
            typer.echo(f"{n:>2}. {s.pair:<14} {s.action.value:<8} {s.score:>3}/100")
        for s in ranked[:top]:
            typer.echo("\n" + s.render())
        for err in result.errors:
            typer.echo(f"! {err}", err=True)

    asyncio.run(run())


async def _load_history(
    settings: Settings, pairs: list[str], years: float
) -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    async with OkxClient() as client:
        return await load_history(
            settings,
            client,
            pairs,
            years,
            progress=lambda p: typer.echo(f"  datos {p} ...", err=True),
        )


async def _load_validation_history(
    settings: Settings, pairs: list[str], years: float
) -> ValidationHistory:
    async with OkxClient() as client:
        return await load_validation_history(
            settings,
            client,
            years,
            pairs=pairs,
            progress=lambda p: typer.echo(f"  datos {p} ...", err=True),
        )


@app.command()
def backtest(
    pair: Annotated[list[str] | None, typer.Option(help="Pair(s); default = universe")] = None,
    years: Annotated[float, typer.Option(help="Years of history")] = 2.0,
    trades: Annotated[bool, typer.Option(help="List every trade")] = False,
) -> None:
    """Run the backtest with the configured threshold, fees and slippage."""
    settings = _settings()
    data, benchmark = asyncio.run(_load_history(settings, pair or [], years))
    costs = CostModel(settings.fee_pct, settings.slippage_pct)
    params = _engine_params(settings)
    every = []
    for name, frame in data.items():
        result = backtest_pair(name, frame, benchmark, params, costs)
        every += result
        _print_metrics(f"{name:<14}", compute_metrics(result))
        if trades:
            for t in result:
                typer.echo(
                    f"    {t.entry_time:%Y-%m-%d %H:%M} -> {t.exit_time:%Y-%m-%d %H:%M} "
                    f"{t.r_multiple:+.2f}R {t.exit_reason} (score {t.score})"
                )
    _print_metrics("TOTAL", compute_metrics(every))


@app.command()
def validate(
    pair: Annotated[list[str] | None, typer.Option(help="Pair(s); default = universe")] = None,
    years: Annotated[float, typer.Option(help="Years of history")] = 2.0,
    folds: Annotated[int, typer.Option(help="Walk-forward test windows")] = 4,
) -> None:
    """Walk-forward validation; writes validation-<quote>-<bar>.json used by live signals."""
    settings = _settings()
    history = asyncio.run(_load_validation_history(settings, pair or [], years))
    typer.echo(f"Universo: {history.universe_text}")
    result = walk_forward(
        history.data,
        history.benchmark,
        _engine_params(settings),
        CostModel(settings.fee_pct, settings.slippage_pct),
        folds=folds,
        max_positions=settings.max_positions,
        members=history.members,
    )
    for th, m in result.full_sample.items():
        _print_metrics(f"Muestra completa umbral {th}", m)
    for f in result.folds:
        _print_metrics(f"Ventana hasta {f.test_end:%Y-%m-%d} (umbral {f.threshold})", f.test)
    _print_metrics("FUERA DE MUESTRA", result.oos)
    path = validation_path(settings)
    save_report(result, path)
    verdict = "POSITIVA" if result.to_validation().positive else "NO positiva (solo informativa)"
    typer.echo(f"Validación {verdict}. Informe: {path}")


@app.command()
def publish() -> None:
    """Run one cycle and publish it to Home Assistant over MQTT."""
    from ha_trading_helper.scheduler import Runner

    settings = _settings()

    async def run() -> None:
        runner = Runner(settings)
        await runner.cycle()
        await runner.publisher.close()

    asyncio.run(run())


@app.command()
def serve() -> None:
    """Run forever: one cycle now and one after every candle close."""
    from ha_trading_helper.ha_app import with_supervisor_mqtt
    from ha_trading_helper.scheduler import Runner

    settings = with_supervisor_mqtt(Settings())
    setup_logging(settings.log_level, json_logs=True)
    asyncio.run(Runner(settings).run_forever())


intraday_app = typer.Typer(no_args_is_help=True, help="Intraday monitor (5m / 15m candles).")
app.add_typer(intraday_app, name="intraday")


@intraday_app.command("serve")
def intraday_serve() -> None:
    """Run forever: one cycle now and one after every 5 min close."""
    from ha_trading_helper.ha_app import with_supervisor_mqtt
    from ha_trading_helper.intraday.config import IntradaySettings
    from ha_trading_helper.intraday.monitor import Monitor

    settings = with_supervisor_mqtt(IntradaySettings())
    setup_logging(settings.log_level, json_logs=True)
    asyncio.run(Monitor(settings).run_forever())


@intraday_app.command("status")
def intraday_status() -> None:
    """Print the measurement so far, read from the local database (no network)."""
    from ha_trading_helper.intraday.config import IntradaySettings
    from ha_trading_helper.intraday.monitor import CONTROLS
    from ha_trading_helper.intraday.paper import stats, verdict
    from ha_trading_helper.intraday.rules import RULES, RULES_VERSION
    from ha_trading_helper.intraday.store import MeasurementStore

    settings = IntradaySettings()
    store = MeasurementStore(settings.data_dir / "intraday.db")
    now = datetime.now(UTC)
    started = store.started_at(now)
    typer.echo(f"Reglas {RULES_VERSION} · midiendo desde {started:%Y-%m-%d %H:%M} UTC")
    all_stats = {r.key: stats(store.trades(r.key)) for r in RULES}
    for rule in RULES:
        st = all_stats[rule.key]
        typer.echo(
            f"{rule.name:<26} cerradas={st.closed} abiertas={st.open} "
            f"aciertos={st.win_rate:.0%} media={st.mean_pct:+.3f}% total={st.total_pct:+.2f}%"
        )
        if rule.control_of is None:
            typer.echo("  " + verdict(st, all_stats[CONTROLS[rule.key].key], started, now)[1])
    for start, end, note in store.gaps():
        typer.echo(f"Corte {start:%d/%m %H:%M} - {end:%d/%m %H:%M} UTC: {note}")


journal_app = typer.Typer(no_args_is_help=True, help="Bitácora OKX (read-only account journal).")
app.add_typer(journal_app, name="journal")


@journal_app.command("serve")
def journal_serve() -> None:
    """Run forever: web panel, a sync every few minutes and MQTT sensors."""
    from ha_trading_helper.ha_app import with_supervisor_mqtt
    from ha_trading_helper.journal.config import JournalSettings
    from ha_trading_helper.journal.service import JournalService

    settings = JournalSettings()
    if settings.mqtt_enabled:
        settings = with_supervisor_mqtt(settings)
    setup_logging(settings.log_level, json_logs=True)
    asyncio.run(JournalService(settings).run_forever())


@journal_app.command("sync")
def journal_sync() -> None:
    """Bring the database up to date with OKX once and print the summary."""
    from ha_trading_helper.journal.config import JournalSettings
    from ha_trading_helper.journal.service import JournalService

    settings = JournalSettings(mqtt_enabled=False)
    setup_logging(settings.log_level)
    result = asyncio.run(JournalService(settings).sync())
    if result is None:
        typer.echo("Faltan las claves de OKX (HTH_OKX_API_KEY, _SECRET, _PASSPHRASE).", err=True)
        raise typer.Exit(1)
    typer.echo(
        f"Valor {result.equity:.2f} USDC · fills nuevos {result.new_fills} · "
        f"movimientos {result.new_bills} · órdenes {result.new_orders} · "
        f"cierres diarios {result.prices}"
    )
    for err in result.errors:
        typer.echo(f"! {err}", err=True)


@journal_app.command("status")
def journal_status() -> None:
    """Print the journal summary from the database (no network)."""
    from ha_trading_helper.journal import payloads
    from ha_trading_helper.journal.config import JournalSettings
    from ha_trading_helper.journal.report import Data
    from ha_trading_helper.journal.store import Store

    settings = JournalSettings()
    data = Data.load(Store(settings.db_url()))
    for key, value in payloads.summary(data).items():
        typer.echo(f"{key:<22} {value}")
