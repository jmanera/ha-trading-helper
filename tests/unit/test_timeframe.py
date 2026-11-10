from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
import respx

from ha_trading_helper.okx.client import BASE_URL, OkxClient
from ha_trading_helper.timeframe import HOURS, bars_per_year, close_hours, duration, okx_bar


@pytest.mark.parametrize(
    ("bar", "hours", "cron", "okx", "per_year"),
    [
        ("1H", 1, ",".join(str(h) for h in range(24)), "1H", 8760),
        ("2H", 2, "0,2,4,6,8,10,12,14,16,18,20,22", "2H", 4380),
        ("4H", 4, "0,4,8,12,16,20", "4H", 2190),
        ("6H", 6, "0,6,12,18", "6Hutc", 1460),
        ("12H", 12, "0,12", "12Hutc", 730),
        ("1D", 24, "0", "1Dutc", 365),
    ],
)
def test_timeframe_table(bar: str, hours: int, cron: str, okx: str, per_year: int) -> None:
    assert duration(bar) == timedelta(hours=hours)
    assert close_hours(bar) == cron
    assert okx_bar(bar) == okx
    assert bars_per_year(bar) == per_year


def test_every_timeframe_closes_on_whole_utc_hours() -> None:
    assert all(24 % h == 0 for h in HOURS.values())


@respx.mock
async def test_client_requests_utc_aligned_daily_candles() -> None:
    route = respx.get(f"{BASE_URL}/api/v5/market/candles").mock(
        return_value=httpx.Response(200, json={"code": "0", "data": []})
    )
    async with OkxClient() as client:
        await client.recent_candles("BTC-USDC", "1D")
    assert route.calls.last.request.url.params["bar"] == "1Dutc"
