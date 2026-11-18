from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from ha_trading_helper.journal.equity import (
    DayValue,
    account_bills,
    bot_value_on,
    daily_values,
    day_return,
    flows,
    performance,
    period_range,
)
from ha_trading_helper.journal.ledger import build_book, run_fifo
from ha_trading_helper.journal.models import Bill, Fill
from ha_trading_helper.journal.prices import PriceBook, from_tickers
from tests.unit.journal_data import (
    BALANCES,
    BILLS,
    BOTS,
    CLOSES,
    D1,
    D2,
    D3,
    D4,
    FILLS,
    LIVE,
    PROFIT,
    SOLD_COST,
    bill,
    ts,
)


def prices() -> PriceBook:
    return PriceBook(closes=CLOSES, live=LIVE).learn(FILLS, BILLS)


def account() -> tuple[list[Bill], dict[str, float]]:
    return account_bills(BILLS, {b.ccy: b.cash_bal for b in BALANCES}, {f.ord_id for f in FILLS})


def test_account_bills_drop_the_bot_books_and_start_empty() -> None:
    kept, opening = account()
    kept_ids = {b.bill_id for b in kept}
    bot_side = [b for b in BILLS if b.sub_type == 200 or b.ord_id == "bot-1"]
    assert not kept_ids & {b.bill_id for b in bot_side}
    assert len(kept) == len(BILLS) - len(bot_side)
    assert opening == {"USDC": 0.0, "SOL": 0.0, "DOOD": 0.0}


def test_account_bills_find_what_was_held_before_the_first_entry() -> None:
    bills = [bill(ts(D1, 1), "BTC", 2, 0.5, 1.5, ord_id="x")]
    _, opening = account_bills(bills, {"BTC": 1.5}, {"x"})
    assert opening == {"BTC": pytest.approx(1.0)}


def test_fifo_closes_oldest_lots_with_fees_inside() -> None:
    kept, opening = account()
    book = build_book(FILLS, kept, prices(), opening=opening, opening_ts=kept[0].ts)
    sale = next(d for d in book.disposals if d.kind == "trade")
    assert sale.qty == pytest.approx(1.5)
    assert sale.proceeds == pytest.approx(89.91)
    assert sale.cost == pytest.approx(SOLD_COST)
    assert sale.pnl == pytest.approx(PROFIT)
    # 0.05 + share of 0.032 bought, plus 0.09 sold
    assert sale.fee == pytest.approx(0.05 + 0.032 * 0.501 / 0.9992 + 0.09)
    assert sale.unknown_qty == 0
    sol = book.holdings["SOL"]
    assert sol.qty == pytest.approx(0.4982)
    assert sol.cost == pytest.approx(90.0 - SOLD_COST)
    assert sol.lots[0].exec_type == "M"
    assert sol.lots[0].fee_rate == pytest.approx(0.0008)
    assert sol.lots[0].price == pytest.approx(40 / 0.9992)


def test_selling_more_than_was_bought_claims_no_profit_on_the_rest() -> None:
    fill = Fill("1", "t", "o", "XRP-USDC", "sell", 2.0, 10.0, -0.02, "USDC", -0.001, "T", 5)
    book = run_fifo(e for e in build_book([fill], [], PriceBook()).events)
    sale = book.disposals[0]
    assert sale.unknown_qty == pytest.approx(10.0)
    assert sale.pnl == pytest.approx(0.0)
    assert sale.estimated


def test_conversions_and_transfers_become_trades_at_market() -> None:
    p = PriceBook(closes={"EUR": {D1: 1.1}})
    bills = [
        bill(ts(D1, 9), "EUR", 1, 10.0, 10.0, sub=11),
        bill(ts(D1, 10), "EUR", 27, -10.0, 0.0, px=0.9, inst="USDC-EUR", trade="c1"),
        bill(ts(D1, 10), "USDC", 27, 11.2, 11.2, px=0.9, inst="USDC-EUR", trade="c1"),
    ]
    book = build_book([], bills, p.learn([], bills))
    kinds = [e.kind for e in book.events]
    assert kinds == ["transfer_in", "convert"]
    sale = book.disposals[0]
    assert sale.kind == "convert"
    assert sale.proceeds == pytest.approx(11.2)
    # bought at 1/0.9 USDC per EUR (the conversion's own price), sold for 11.2
    assert sale.cost == pytest.approx(10 / 0.9)
    assert flows(bills, p)[0].value == pytest.approx(10 / 0.9)


def test_cross_pair_fill_disposes_the_quote_and_fee_in_third_currency() -> None:
    p = PriceBook(closes={"BTC": {D1: 100.0}, "ETH": {D1: 10.0}, "OKB": {D1: 5.0}})
    buy_btc = Fill("1", "t", "o0", "BTC-USDC", "buy", 100.0, 1.0, 0.0, "BTC", 0.0, "T", ts(D1, 1))
    cross = Fill("2", "t", "o1", "ETH-BTC", "buy", 0.1, 5.0, -0.2, "OKB", -0.001, "M", ts(D1, 2))
    book = build_book([buy_btc, cross], [], p)
    btc_sale = next(d for d in book.disposals if d.ccy == "BTC")
    assert btc_sale.qty == pytest.approx(0.5)
    assert btc_sale.proceeds == pytest.approx(50.0)
    assert book.holdings["ETH"].cost == pytest.approx(50.0)
    fee = next(e for e in book.events if e.kind == "fee")
    assert fee.fee == pytest.approx(1.0)


def test_other_credits_cost_nothing() -> None:
    p = PriceBook(closes={"ABC": {D1: 2.0}})
    book = build_book([], [bill(ts(D1, 1), "ABC", 99, 5.0, 5.0)], p)
    lot = book.holdings["ABC"].lots[0]
    assert lot.cost == 0.0 and lot.kind == "other"


def test_prices_prefer_own_trades_then_closes_then_live() -> None:
    p = prices()
    assert p.at("SOL", ts(D1, 11)) == 50.0  # own fill
    assert p.on("SOL", D2) == 45.0
    assert p.on("SOL", date(2026, 11, 6)) == 62.0  # nearest close within a week
    assert p.on("USDC", D1) == 1.0
    assert p.on("USDT", D1) == 1.0
    assert p.on("NOPE", D1) is None
    assert p.now("SOL") == 70.0 and p.now("NOPE") is None
    assert from_tickers("EUR", {"USDC-EUR": 0.9}) == pytest.approx(1 / 0.9)
    assert from_tickers("ABC", {"ABC-USDT": 3.0}) == 3.0
    assert from_tickers("ABC", {}) is None


def test_daily_values_rebuild_the_account_value() -> None:
    kept, opening = account()
    values = daily_values(kept, opening, prices(), BOTS, [], D4, live_equity=None)
    assert [v.day for v in values] == [D1, D2, D3, D4]
    by_day = {v.day: v for v in values}
    # Day 1: 50 USDC + 0.999 SOL at the close of 50
    assert by_day[D1].equity == pytest.approx(50 + 0.999 * 50)
    assert by_day[D1].flow == pytest.approx(100)
    # Day 3: 70 USDC + 0.4982 SOL at 62 + the bot at its investment
    assert by_day[D3].equity == pytest.approx(70 + 0.4982 * 62 + 20)
    assert by_day[D3].flow == pytest.approx(-9.91)
    assert all(v.estimated for v in values)


def test_snapshots_replace_rebuilt_values() -> None:
    kept, opening = account()
    late = ts(D3, 23, 30)
    early = ts(D2, 8)
    values = daily_values(
        kept,
        opening,
        prices(),
        BOTS,
        [(early, 1.0, 5.0), (late, 123.0, 19.0)],
        D4,
        live_equity=111.0,
    )
    by_day = {v.day: v for v in values}
    assert by_day[D2].equity == pytest.approx(10 + 1.9982 * 45 + 5.0)
    assert by_day[D3].equity == 123.0 and not by_day[D3].estimated
    assert by_day[D4].equity == 111.0 and not by_day[D4].estimated


def test_bots_count_only_while_running() -> None:
    stopped = replace(BOTS[0], stop_time=ts(D3, 18))
    assert bot_value_on(BOTS, D2) == 0
    assert bot_value_on(BOTS, D3) == 20
    assert bot_value_on([stopped], D3) == 0


def test_performance_separates_deposits_from_profit() -> None:
    values = [
        DayValue(D1, 100.0, 100.0, False),  # deposit, no gain
        DayValue(D2, 110.0, 0.0, False),  # +10 %
        DayValue(D3, 1110.0, 1000.0, False),  # big deposit, no gain
        DayValue(D4, 999.0, 0.0, False),  # -10 %
    ]
    p = performance(values, D1, D4)
    assert p is not None
    assert p.profit == pytest.approx(999 - 0 - 1100)
    assert p.deposits == 1100 and p.withdrawals == 0
    assert p.twr == pytest.approx(1.1 * 0.9 - 1)
    assert p.max_drawdown == pytest.approx(-0.1)
    assert p.best is not None and p.best.day == D2
    assert p.worst is not None and p.worst.day == D4
    assert [m.month for m in p.months] == ["2026-11"]
    later = performance(values, D2, D4)
    assert later is not None and later.start_equity == 100.0
    assert later.profit_pct_of_capital == pytest.approx(later.profit / 1100)
    assert performance(values, date(2027, 1, 1), date(2027, 1, 2)) is None


def test_day_return_without_capital_is_zero() -> None:
    assert day_return(0.0, 0.0, 0.0) == 0.0


@pytest.mark.parametrize(
    ("key", "start"),
    [
        ("7d", date(2026, 11, 14)),
        ("30d", date(2026, 10, 22)),
        ("mes", date(2026, 11, 1)),
        ("ytd", date(2026, 1, 1)),
        ("90d", date(2026, 8, 23)),
        ("1a", date(2025, 11, 21)),
        ("todo", date(2020, 1, 1)),
    ],
)
def test_period_range(key: str, start: date) -> None:
    assert period_range(key, date(2026, 11, 20), date(2020, 1, 1)) == (start, date(2026, 11, 20))
    assert period_range(key, date(2026, 11, 20), date(2026, 11, 19))[0] == date(2026, 11, 19)
