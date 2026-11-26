"""Store, sync, pages and MQTT payloads over the hand-checked history of journal_data."""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ha_trading_helper.journal import payloads, report
from ha_trading_helper.journal.config import JournalSettings
from ha_trading_helper.journal.models import Balance, Bill, Bot, Fill, OpenOrder, Order
from ha_trading_helper.journal.store import Store, bills, fills
from ha_trading_helper.journal.sync import ORDERS_REREAD, Syncer, value_now
from ha_trading_helper.journal.web import create_app, duration, money, pct, qty, signed, tone
from ha_trading_helper.okx.client import OkxError
from tests.unit.journal_data import (
    BALANCES,
    BILLS,
    BOTS,
    CLOSES,
    D1,
    D2,
    D3,
    FILLS,
    LIVE,
    NOW,
    PROFIT,
    ts,
)


class FakeOkx:
    """Answers like OkxReadClient, from journal_data."""

    def __init__(self) -> None:
        self.since: dict[str, str | None] = {}
        self.fail_prices = False

    async def balances(self) -> list[Balance]:
        return BALANCES

    async def last_prices(self) -> dict[str, float]:
        return {"SOL-USDC": 70.0, "DOOD-USDC": 0.1}

    async def fills(self, since: str | None = None) -> list[Fill]:
        self.since["fills"] = since
        return [f for f in FILLS if since is None or int(f.bill_id) > int(since)]

    async def bills(self, since: str | None = None) -> list[Bill]:
        self.since["bills"] = since
        return [b for b in BILLS if since is None or int(b.bill_id) > int(since)]

    async def orders(self, since: str | None = None) -> list[Order]:
        return [
            Order(
                "o3",
                "SOL-USDC",
                "sell",
                "limit",
                "filled",
                60.0,
                1.5,
                60.0,
                1.5,
                -0.09,
                "USDC",
                "",
                ts(D3, 9),
                ts(D3, 9),
            ),
            Order(
                "o4",
                "SOL-USDC",
                "buy",
                "post_only",
                "canceled",
                45.0,
                0.5,
                None,
                0.0,
                0.0,
                "",
                "",
                ts(D3, 8),
                ts(D3, 8, 30),
            ),
        ]

    async def open_orders(self) -> list[OpenOrder]:
        return [
            OpenOrder(
                "a1", "algo", "SOL-USDC", "sell", "conditional", None, 0.4982, 0.0, 55.0, ts(D3, 12)
            ),
            OpenOrder(
                "4001", "order", "SOL-USDC", "buy", "limit", 63.0, 1.0, 0.25, None, ts(D3, 13)
            ),
        ]

    async def bots(self) -> list[Bot]:
        return BOTS

    async def fee_rates(self) -> dict[str, tuple[float, float]]:
        return {"1": (0.0008, 0.001)}

    async def fee_groups(self) -> dict[str, str]:
        return {"SOL-USDC": "1"}

    async def daily_closes(self, inst_id: str, since: date) -> dict[date, float]:
        if self.fail_prices:
            raise OkxError("timeout")
        ccy = inst_id.split("-", maxsplit=1)[0]
        if inst_id.startswith("USDC-") or ccy not in CLOSES:
            raise OkxError(f"/api/v5/market/history-candles code 51001: {inst_id}")
        return {d: c for d, c in CLOSES[ccy].items() if d >= since}


@pytest.fixture
def store() -> Store:
    return Store("sqlite://")


async def synced(store: Store) -> FakeOkx:
    fake = FakeOkx()
    await Syncer(store, fake).run(NOW)  # type: ignore[arg-type]
    return fake


async def test_sync_fills_the_store_and_resumes(store: Store) -> None:
    fake = await synced(store)
    assert len(store.fills()) == 3 and len(store.bills()) == len(BILLS)
    assert store.last_id(fills, "bill_id") == "2003"
    snaps = store.snapshots()
    assert snaps[0][1] == pytest.approx(80 + 0.4982 * 70 + 10)
    assert snaps[0][2] == pytest.approx(10 + 10)
    assert store.prices()["SOL"][D3] == 62.0
    assert store.get_json("price_sources", {})["SOL"] == "SOL-USDC"
    assert store.get_json("last_sync", {})["errors"] == []
    await Syncer(store, fake).run(NOW)  # type: ignore[arg-type]
    assert fake.since["fills"] == "2003"
    assert fake.since["bills"] == store.last_id(bills, "bill_id")
    assert len(store.fills()) == 3


def test_sync_resumes_orders_below_those_left_resting(store: Store) -> None:
    syncer = Syncer(store, FakeOkx())  # type: ignore[arg-type]
    assert syncer._orders_since() is None
    store.save_orders(
        Order(i, "SOL-USDC", "buy", "limit", "filled", 60.0, 1.0, 60.0, 1.0, 0.0, "", "", 0, 0)
        for i in ("900", "1200")
    )
    # Saved by an older version, which may have skipped some: read it all once more.
    assert syncer._orders_since() is None
    store.set_meta(ORDERS_REREAD, "1")
    assert syncer._orders_since() == "1200"
    # A limit placed before the last stored order and still on the book at the last sync:
    # when it fills it keeps its lower ordId, so the history must be read from below it
    # (paging stops at ``since`` and leaves it out).
    # Algo ids are a different sequence and do not count.
    store.replace_open_orders(
        OpenOrder(i, kind, "SOL-USDC", "buy", "limit", 60.0, 1.0, 0.0, None, 0)
        for i, kind in (("1100", "order"), ("950", "order"), ("5", "algo"))
    )
    assert syncer._orders_since() == "949"


async def test_sync_reports_failed_steps(store: Store) -> None:
    fake = FakeOkx()
    fake.fail_prices = True
    result = await Syncer(store, fake).run(NOW)  # type: ignore[arg-type]
    assert result.errors and result.errors[0].startswith("precios")
    assert store.get_json("last_sync", {})["errors"] == result.errors


def test_value_now_falls_back_to_okx_usd_value() -> None:
    equity, bots, used = value_now([Balance("ABC", 2.0, 2.0, 0.0, 6.0)], {})
    assert equity == 6.0 and bots == 0 and used == {"ABC": 3.0}


def test_store_notes_and_ids(store: Store) -> None:
    store.save_note("order:1", " entré por ruptura ", "ruptura", 1)
    assert store.notes() == {"order:1": ("entré por ruptura", "ruptura")}
    store.save_note("order:1", "", "", 2)
    assert store.notes() == {}
    store.save_bills([BILLS[0], Bill("999", 1, "X", 1, 1, 1.0, 1.0, None, "", "", "", "")])
    assert store.last_id(bills, "bill_id") == BILLS[0].bill_id  # longer id is newer
    store.set_meta("k", "v")
    store.set_meta("k", "w")
    assert store.get_meta("k") == "w"


async def test_report_views(store: Store) -> None:
    await synced(store)
    store.set_json("live_prices", LIVE)
    d = report.Data.load(store, NOW)
    pos = report.positions(d)
    assert [p.ccy for p in pos.rows] == ["SOL"]
    sol = pos.rows[0]
    assert sol.qty == pytest.approx(0.4982)
    assert sol.value == pytest.approx(0.4982 * 70)
    assert sol.exit_fee == pytest.approx(0.4982 * 70 * 0.001)
    assert sol.orders[0].trigger_px == 55.0
    assert pos.bots[0].value == pytest.approx(19.5)
    assets = {r.ccy: r for r in report.assets(d)}
    assert assets["SOL"].realized == pytest.approx(PROFIT)
    assert assets["SOL"].closed == 1 and assets["SOL"].wins == 1
    assert assets["SOL"].orders == 3
    assert "Bot DOOD-USDC" in assets
    rows = report.journal(d)
    sale = next(r for r in rows if r.ref == "order:o3")
    assert sale.pnl == pytest.approx(PROFIT) and sale.liquidity == "Taker"
    assert sale.ord_type == "limit"
    assert {r.kind for r in rows} >= {"trade", "transfer_in", "transfer_out"}
    assert report.journal(d, ccy="SOL", kind="trade", start=D2, end=D3)[0].ref == "order:o3"
    view = report.period_view(d, "todo")
    assert view.perf is not None
    assert view.trades == 3
    assert view.maker_fees == pytest.approx(0.0008 * 40)
    assert view.realized == pytest.approx(PROFIT)
    custom = report.period_view(d, "7d", start=D1, end=D1)
    assert custom.key == "custom" and custom.end == D1
    ov = report.overview(d)
    assert ov.equity == pytest.approx(80 + 0.4982 * 70 + 10)
    assert ov.realized_all == pytest.approx(PROFIT)


async def test_orders_view(store: Store) -> None:
    await synced(store)
    store.set_json("live_prices", LIVE)
    d = report.Data.load(store, NOW)
    view = report.orders_view(d)
    stop, limit = sorted(view.open, key=lambda o: o.order.c_time)
    assert stop.type_label == "Stop / TP" and not stop.maker
    assert stop.px == 55.0 and stop.distance == pytest.approx(55 / 70 - 1)
    assert stop.value == pytest.approx(0.4982 * 55) and stop.fee == pytest.approx(
        stop.value * 0.001
    )
    assert stop.has_position
    assert limit.maker and limit.remaining == pytest.approx(0.75)
    assert limit.value == pytest.approx(0.75 * 63) and limit.fee == pytest.approx(
        limit.value * 0.0008
    )
    assert limit.filled_pct == pytest.approx(0.25)
    assert view.buying == pytest.approx(0.75 * 63) and view.selling == pytest.approx(0.4982 * 55)
    sale, canceled = view.history
    assert sale.state_label == "Ejecutada" and sale.liquidity == "Taker"
    assert sale.value == pytest.approx(90.0) and sale.fee == pytest.approx(0.09)
    assert sale.pnl == pytest.approx(PROFIT)
    assert canceled.state_label == "Cancelada" and canceled.value == 0 and canceled.pnl is None
    assert [r.order.ord_id for r in report.orders_view(d, state="canceladas").history] == ["o4"]
    assert report.orders_view(d, ccy="DOOD").history == []
    assert report.orders_view(d, start=D1, end=D2).history == []
    assert view.ccys == ["SOL"] and view.total == 2


async def test_pages_render_under_ingress(store: Store) -> None:
    await synced(store)
    calls: list[int] = []

    async def sync_now() -> None:
        calls.append(1)

    app = create_app(store, sync_now=sync_now, clock=lambda: NOW)
    web = TestClient(app, headers={"X-Ingress-Path": "/api/hassio_ingress/tok"})
    for path in (
        "/",
        "/posiciones",
        "/rendimiento",
        "/rendimiento?periodo=todo",
        "/rendimiento?desde=2026-11-02&hasta=2026-11-03",
        "/activos",
        "/bitacora?activo=sol&tipo=trade&restos=true",
        "/bitacora?desde=nope",
        "/ordenes",
        "/ordenes?activo=sol&estado=canceladas&desde=2026-11-01&hasta=2026-11-03",
    ):
        page = web.get(path)
        assert page.status_code == 200, path
        assert 'href="/api/hassio_ingress/tok/posiciones"' in page.text
    assert "SOL" in web.get("/posiciones").text
    saved = web.post(
        "/nota",
        data={"ref": "order:o3", "text": "salí en resistencia", "back": "bitacora?activo=SOL"},
        follow_redirects=False,
    )
    assert saved.status_code == 303
    assert saved.headers["location"] == "/api/hassio_ingress/tok/bitacora?activo=SOL"
    assert "salí en resistencia" in web.get("/bitacora").text
    evil = web.post("/sincronizar", data={"back": "https://evil.example"}, follow_redirects=False)
    assert evil.headers["location"] == "/api/hassio_ingress/tok/"
    assert calls == [1]
    assert web.get("/salud").text == "ok"


def test_empty_database_still_renders(store: Store) -> None:
    web = TestClient(create_app(store, clock=lambda: NOW))
    for path in ("/", "/posiciones", "/ordenes", "/rendimiento", "/activos", "/bitacora"):
        assert web.get(path).status_code == 200


async def test_mqtt_payloads(store: Store) -> None:
    await synced(store)
    d = report.Data.load(store, NOW)
    disc = payloads.discovery_messages("okx_journal", "homeassistant")
    assert len(disc) == len(payloads.SENSORS)
    first: dict[str, Any] = json.loads(disc[0].payload)
    assert first["state_topic"] == "okx_journal/summary/state"
    assert first["unit_of_measurement"] == "USDC"
    second = json.loads(disc[1].payload)
    assert second["value_template"] == "{{ value_json.resultado_hoy }}"
    state = payloads.state_messages(d, "okx_journal")
    attrs = json.loads(state[1].payload)
    assert float(state[0].payload) == attrs["valor"]
    assert attrs["posiciones"] == 2  # SOL + the bot


def test_formatting() -> None:
    assert money(1234.5) == "1.234,50"
    assert money(None) == "—"
    assert signed(2.0) == "+2,00" and signed(-0.0004) == "0,00" and signed(None) == "—"
    assert pct(0.1234) == "+12,34 %" and pct(-0.05, 1) == "-5,0 %" and pct(None) == "—"
    assert qty(0.000123456) == "0,000123456" and qty(1500.0) == "1.500" and qty(0) == "0"
    assert tone(1.0) == "pos" and tone(-1.0) == "neg" and tone(0.001) == ""
    assert duration(60_000) == "1 min" and duration(3_600_000 * 5) == "5,0 h"
    assert duration(3_600_000 * 72) == "3,0 d"


def test_settings_build_database_urls(tmp_path: Any) -> None:
    s = JournalSettings(data_dir=tmp_path, db_host="")
    assert s.db_url() == f"sqlite:///{tmp_path / 'journal.db'}"
    secret = "p@ss"  # noqa: S105 - a URL-escaping case, not a credential
    m = JournalSettings(db_host="core-mariadb", db_user="u", db_password=secret, db_name="j")
    assert m.db_url() == "mysql+pymysql://u:p%40ss@core-mariadb:3306/j?charset=utf8mb4"
    assert JournalSettings(okx_domain="eea.okx.com").okx_base_url == "https://eea.okx.com"


class FakePublisher:
    def __init__(self) -> None:
        self.sent: list[list[Any]] = []

    async def send(self, messages: list[Any]) -> None:
        self.sent.append(messages)

    async def close(self) -> None:
        return None


async def test_service_without_keys_records_the_problem(store: Store) -> None:
    from ha_trading_helper.journal.service import JournalService

    service = JournalService(JournalSettings(mqtt_enabled=False), store=store)
    assert await service.sync() is None
    assert store.get_json("last_sync", {})["errors"] == ["faltan las claves de OKX"]


async def test_service_publishes_discovery_once(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ha_trading_helper.journal import service as service_module

    fake = FakeOkx()

    class Client:
        async def __aenter__(self) -> FakeOkx:
            return fake

        async def __aexit__(self, *exc: object) -> None:
            return None

    keys = {"okx_api_key": "k", "okx_api_secret": "s", "okx_passphrase": "p"}
    settings = JournalSettings.model_validate(keys)
    service = service_module.JournalService(settings, store=store)
    publisher = FakePublisher()
    service.publisher = publisher  # type: ignore[assignment]
    monkeypatch.setattr(service, "client", Client)
    result = await service.sync()
    await service.sync()
    assert result is not None and result.new_fills == 3
    assert len(publisher.sent) == 2
    assert len(publisher.sent[0]) == len(payloads.SENSORS) + 2
    assert len(publisher.sent[1]) == 2
