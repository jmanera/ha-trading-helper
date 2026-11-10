from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest
import respx

from ha_trading_helper.config import Settings
from ha_trading_helper.publish.payloads import Message
from ha_trading_helper.scheduler import Runner
from tests.unit.test_backtest import cyclical_market
from tests.unit.test_service import frames as frames_fixture
from tests.unit.test_service import mock_okx

frames = frames_fixture


class FakePublisher:
    def __init__(self) -> None:
        self.sent: list[list[Message]] = []
        self.fail = False

    async def send(self, messages: list[Message]) -> None:
        if self.fail:
            raise RuntimeError("broker down")
        self.sent.append(messages)

    async def close(self) -> None:
        return None


def runner(tmp_path: Path, **kw: object) -> tuple[Runner, FakePublisher]:
    s = Settings(_env_file=None, data_dir=tmp_path, min_volume_24h_usdt=1, **kw)  # type: ignore[call-arg]
    r = Runner(s)
    fake = FakePublisher()
    r.publisher = fake  # type: ignore[assignment]
    return r, fake


@respx.mock
async def test_cycle_publishes_and_logs(tmp_path: Path, frames: dict[str, pd.DataFrame]) -> None:
    mock_okx(frames)
    r, fake = runner(tmp_path)
    await r.cycle()
    topics = {m.topic: m.payload for m in fake.sent[0]}
    assert topics["ha_trading_helper/best/state"] == "SOL-USDT"
    assert "homeassistant/sensor/ha_trading_helper/sol_usdt_puntuacion/config" in topics
    assert (tmp_path / "signals.jsonl").read_text()

    # Publisher failure is logged, not raised, and the cycle still records signals.
    fake.fail = True
    await r.safe_cycle()


@respx.mock
async def test_removed_pairs_get_empty_configs(
    tmp_path: Path, frames: dict[str, pd.DataFrame]
) -> None:
    mock_okx(frames)
    r, fake = runner(tmp_path)
    await r.cycle()
    r.settings = r.settings.model_copy(update={"pairs": ["SOL-USDT"]})
    await r.cycle()
    removed = [m for m in fake.sent[1] if m.payload == "" and "xrp_usdt" in m.topic]
    assert len(removed) == 2


async def test_safe_cycle_swallows_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    r, _ = runner(tmp_path)

    async def boom() -> None:
        raise RuntimeError("x")

    monkeypatch.setattr(r, "cycle", boom)
    await r.safe_cycle()


async def test_revalidate_writes_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import ha_trading_helper.scheduler as sched

    async def fake_history(
        *_: object, **__: object
    ) -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
        return {"A-USDT": cyclical_market(seed=3)}, cyclical_market(seed=9)

    cycles: list[int] = []

    async def fake_cycle() -> None:
        cycles.append(1)

    monkeypatch.setattr(sched, "load_history", fake_history)
    r, _ = runner(tmp_path)
    monkeypatch.setattr(r, "cycle", fake_cycle)
    assert r.validation_is_stale()
    await r.safe_revalidate(only_if_stale=True)
    report = json.loads((tmp_path / "validation.json").read_text())
    assert "validation" in report
    assert not r.validation_is_stale()
    await r.safe_revalidate(only_if_stale=True)  # fresh: nothing to do
    assert cycles == [1]

    report["validation"]["generated_at"] = "2020-01-01T00:00:00Z"
    (tmp_path / "validation.json").write_text(json.dumps(report))
    assert r.validation_is_stale()

    async def broken(*_: object, **__: object) -> None:
        raise RuntimeError("okx down")

    monkeypatch.setattr(sched, "load_history", broken)
    await r.safe_revalidate()  # logged, not raised
