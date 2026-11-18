"""The journal's store on a real MariaDB in Docker: upserts, ids and a full sync + report."""

from __future__ import annotations

import time
from collections.abc import Iterator

import pytest
import sqlalchemy
from testcontainers.core.container import DockerContainer

from ha_trading_helper.journal import report
from ha_trading_helper.journal.config import JournalSettings
from ha_trading_helper.journal.store import Store, bills, fills
from ha_trading_helper.journal.sync import Syncer
from tests.unit.journal_data import BILLS, NOW, PROFIT
from tests.unit.test_journal_app import FakeOkx

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def mariadb_url() -> Iterator[str]:
    container = (
        DockerContainer("mariadb:11")
        .with_exposed_ports(3306)
        .with_env("MARIADB_DATABASE", "okx_journal")
        .with_env("MARIADB_USER", "journal")
        .with_env("MARIADB_PASSWORD", "journal")
        .with_env("MARIADB_RANDOM_ROOT_PASSWORD", "1")
    )
    with container:
        settings = JournalSettings(
            db_host=container.get_container_host_ip(),
            db_port=int(container.get_exposed_port(3306)),
            db_user="journal",
            db_password="journal",  # noqa: S106 - throwaway container
        )
        url = settings.db_url()
        deadline = time.monotonic() + 90
        while True:
            try:
                sqlalchemy.create_engine(url).connect().close()
                break
            except sqlalchemy.exc.OperationalError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(1)
        yield url


async def test_sync_and_report_on_mariadb(mariadb_url: str) -> None:
    store = Store(mariadb_url)
    fake = FakeOkx()
    await Syncer(store, fake).run(NOW)  # type: ignore[arg-type]
    await Syncer(store, fake).run(NOW)  # type: ignore[arg-type]  # upserts, no duplicates
    assert len(store.fills()) == 3
    assert len(store.bills()) == len(BILLS)
    assert store.last_id(fills, "bill_id") == "2003"
    assert store.last_id(bills, "bill_id") == max((b.bill_id for b in BILLS), key=int)
    store.save_note("order:o3", "nota con acentos: salí en resistencia ✓", "ruptura", 1)
    assert store.notes()["order:o3"][0].endswith("✓")
    d = report.Data.load(store, NOW)
    assert next(r for r in report.journal(d) if r.ref == "order:o3").pnl == pytest.approx(PROFIT)
    store.close()
