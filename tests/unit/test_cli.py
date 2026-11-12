from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from typer.testing import CliRunner

from ha_trading_helper import cli
from ha_trading_helper.config import Settings
from ha_trading_helper.service import ScanResult, ValidationHistory
from ha_trading_helper.strategy.signal import build_signal
from tests.conftest import make_candles, uptrend_with_pullback
from tests.unit.test_backtest import cyclical_market

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HTH_DATA_DIR", str(tmp_path / "data"))


def test_scan_prints_ranking_and_reasons(monkeypatch: pytest.MonkeyPatch) -> None:
    vol = np.full(400, 1000.0)
    vol[-1] = 5000.0
    sig = build_signal(
        "SOL-USDT",
        make_candles(uptrend_with_pullback(400), volume=vol),
        None,
        Settings(_env_file=None),  # type: ignore[call-arg]
    )
    assert sig is not None

    async def fake_scan(*_: object, **__: object) -> ScanResult:
        return ScanResult([sig], ["SOL-USDT"], ["XRP-USDT: boom"])

    monkeypatch.setattr(cli, "scan", fake_scan)
    result = runner.invoke(cli.app, ["scan", "--once"])
    assert result.exit_code == 0, result.output
    assert "1. SOL-USDT" in result.output
    assert "Motivos:" in result.output
    assert "XRP-USDT: boom" in result.output

    as_json = runner.invoke(cli.app, ["scan", "--json"])
    assert json.loads(as_json.stdout)[0]["pair"] == "SOL-USDT"


def _fake_history(monkeypatch: pytest.MonkeyPatch) -> None:
    async def load(
        settings: Settings, pairs: list[str], years: float
    ) -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
        return {"A-USDT": cyclical_market(seed=3), "B-USDT": cyclical_market(seed=5)}, (
            cyclical_market(seed=9)
        )

    async def load_validation(
        settings: Settings, pairs: list[str], years: float
    ) -> ValidationHistory:
        data, benchmark = await load(settings, pairs, years)
        return ValidationHistory(data, benchmark, None)

    monkeypatch.setattr(cli, "_load_history", load)
    monkeypatch.setattr(cli, "_load_validation_history", load_validation)


def test_backtest_command(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_history(monkeypatch)
    result = runner.invoke(cli.app, ["backtest", "--trades"])
    assert result.exit_code == 0, result.output
    assert "A-USDT" in result.output and "TOTAL: n=" in result.output


def test_validate_command_writes_report(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _fake_history(monkeypatch)
    result = runner.invoke(cli.app, ["validate", "--folds", "2"])
    assert result.exit_code == 0, result.output
    assert "FUERA DE MUESTRA" in result.output
    report = json.loads((tmp_path / "data" / "validation-usdc-4h.json").read_text())
    assert len(report["folds"]) == 2
