from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from ha_trading_helper.config import Settings
from ha_trading_helper.okx.client import OkxError
from ha_trading_helper.okx.trade import LotRules, OrderFill
from ha_trading_helper.strategy.risk import Levels
from ha_trading_helper.strategy.signal import Action, Signal
from ha_trading_helper.trading.executor import Executor
from ha_trading_helper.trading.positions import OpenPosition, PositionStore
from ha_trading_helper.trading.telegram import Conversation, Pending, parse_amount

T0 = datetime(2026, 11, 10, 16, tzinfo=UTC)
FAKE_KEYS = {"okx_api_key": "k", "okx_api_secret": "s", "okx_passphrase": "p"}


async def no_sleep(_: float) -> None:
    return None


class FakeOkx:
    """In-memory stand-in for OkxTradeClient."""

    def __init__(self) -> None:
        self.orders: list[tuple[str, str, str, bool]] = []
        self.stops: list[tuple[str, str, float]] = []
        self.cancelled: list[str] = []
        self.balance = 0.0
        self.price = 180.0
        self.fail_stop = False
        self.fail_cancel = False
        self.states = ["live", "filled"]

    async def lot_rules(self, inst_id: str) -> LotRules:
        return LotRules(lot_size=Decimal("0.0001"), min_size=Decimal("0.001"))

    async def market_order(self, inst_id: str, side: str, size: str, *, quote: bool) -> str:
        self.orders.append((inst_id, side, size, quote))
        return str(len(self.orders))

    async def order(self, inst_id: str, order_id: str) -> OrderFill:
        _, side, size, quote = self.orders[int(order_id) - 1]
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        qty = float(size) / self.price if quote else float(size)
        if side == "buy":
            fee, ccy = -qty * 0.001, inst_id.split("-", maxsplit=1)[0]
        else:
            fee, ccy = -qty * self.price * 0.001, inst_id.split("-")[1]
        return OrderFill(
            order_id=order_id, state=state, avg_price=self.price,
            filled=qty if state == "filled" else 0.0, fee=fee, fee_ccy=ccy,
        )  # fmt: skip

    async def stop_loss(self, inst_id: str, size: str, trigger_price: float) -> str:
        if self.fail_stop:
            raise OkxError("precio de stop inválido")
        self.stops.append((inst_id, size, trigger_price))
        return "algo1"

    async def cancel_stop(self, inst_id: str, algo_id: str) -> None:
        if self.fail_cancel:
            raise OkxError("algo ya ejecutado")
        self.cancelled.append(algo_id)

    async def available(self, ccy: str) -> float:
        return self.balance


@pytest.fixture
def okx() -> FakeOkx:
    return FakeOkx()


@pytest.fixture
def store(tmp_path: Path) -> PositionStore:
    return PositionStore(tmp_path)


def executor(okx: FakeOkx, store: PositionStore, **kw: object) -> Executor:
    return Executor(okx, store, sleep=no_sleep, **kw)  # type: ignore[arg-type]


async def test_buy_records_position_net_of_fee_and_sets_stop(
    okx: FakeOkx, store: PositionStore
) -> None:
    text = await executor(okx, store).buy("SOL-USDC", 180.0, risk_per_unit=9.0)
    assert okx.orders == [("SOL-USDC", "buy", "180", True)]
    pos = store.get("SOL-USDC")
    assert pos is not None and pos.quantity == pytest.approx(0.999)
    assert pos.cost == pytest.approx(180.0) and pos.stop_algo_id == "algo1"
    assert okx.stops == [("SOL-USDC", "0.9990", 171.0)]
    assert "Compra ejecutada" in text and "Stop en OKX a 171" in text


async def test_buy_without_stop_level_or_when_stop_fails(
    okx: FakeOkx, store: PositionStore
) -> None:
    text = await executor(okx, store).buy("SOL-USDC", 90.0, risk_per_unit=None)
    assert "Sin stop" in text and not okx.stops
    okx.fail_stop = True
    text = await executor(okx, store).buy("ETH-USDC", 90.0, risk_per_unit=5.0)
    assert "No se pudo poner el stop" in text
    eth = store.get("ETH-USDC")
    assert eth is not None and eth.stop_algo_id is None
    text = await executor(okx, store, stop_on_exchange=False).buy("XRP-USDC", 50.0, 1.0)
    assert "stop" not in text.lower()


async def test_unfilled_order_raises(okx: FakeOkx, store: PositionStore) -> None:
    okx.states = ["live"]
    with pytest.raises(OkxError, match="no se ejecutó"):
        await executor(okx, store).buy("SOL-USDC", 100.0, 5.0)
    assert store.get("SOL-USDC") is None


async def test_sell_cancels_stop_caps_quantity_and_reports_pnl(
    okx: FakeOkx, store: PositionStore
) -> None:
    store.add(OpenPosition(pair="SOL-USDC", quantity=1.0, entry_price=150.0, cost=150.0,
                           entry_time=T0, stop_algo_id="algo1"))  # fmt: skip
    okx.balance = 5.0  # more SOL than the position: sell only the position
    text = await executor(okx, store).sell("SOL-USDC")
    assert okx.cancelled == ["algo1"]
    assert okx.orders[-1] == ("SOL-USDC", "sell", "1.0000", False)
    assert store.get("SOL-USDC") is None
    assert "Venta ejecutada" in text and "+29,82 USDC" in text  # 180 - 0.18 fee - 150


async def test_sell_edge_cases(okx: FakeOkx, store: PositionStore) -> None:
    ex = executor(okx, store)
    assert "No tengo registrada" in await ex.sell("SOL-USDC")
    store.add(OpenPosition(pair="SOL-USDC", quantity=1.0, entry_price=150.0, cost=150.0,
                           entry_time=T0, stop_algo_id="algo1"))  # fmt: skip
    okx.fail_cancel = True
    okx.balance = 0.0
    assert "stop ya se" in await ex.sell("SOL-USDC")
    assert store.get("SOL-USDC") is None and not okx.orders

    store.add(OpenPosition(pair="SOL-USDC", quantity=1.0, entry_price=200.0, cost=200.0,
                           entry_time=T0, stop_algo_id="algo1"))  # fmt: skip
    okx.balance = 0.5
    text = await ex.sell("SOL-USDC")
    assert (
        "El stop no se pudo cancelar" in text and "-10,09 USDC" in text
    )  # 0.5 x 180 - 0.09 fee - 100


def test_parse_amount() -> None:
    assert parse_amount("150") == 150.0
    assert parse_amount(" 150,5 usdc ") == 150.5
    assert parse_amount("75€") == 75.0
    assert parse_amount("hola") is None and parse_amount("-5") is None


def signal(pair: str = "SOL-USDC") -> Signal:
    return Signal(
        pair=pair, action=Action.ENTER, score=80, candle_open=T0, candle_close=T0,
        close=180.0, reasons=[], market_ok=True, market="ok",
        levels=Levels(entry=180, stop=171, target1=193.5, target2=207, risk_per_unit=9.0,
                      size=1, notional=180, risk_amount=9),
    )  # fmt: skip


class Clock:
    def __init__(self) -> None:
        self.t = T0

    def __call__(self) -> datetime:
        return self.t


def conversation(
    store: PositionStore, okx: FakeOkx, clock: Clock, **settings: object
) -> Conversation:
    s = Settings(  # type: ignore[call-arg]
        _env_file=None, trading_enabled=True, max_order_amount=500, **FAKE_KEYS, **settings
    )
    signals = {"SOL-USDC": signal()}
    return Conversation(s, executor(okx, store), store, signals.get, now=clock)


async def test_buy_flow_asks_amount_confirms_and_executes(
    okx: FakeOkx, store: PositionStore
) -> None:
    clock = Clock()
    conv = conversation(store, okx, clock)
    [ask] = await conv.handle({"type": "callback", "data": "/hth buy SOL-USDC"})
    assert ask.text.startswith("[DEMO] ¿Cuántos USDC") and "máximo 500" in ask.text
    [bad] = await conv.handle({"type": "text", "text": "mucho"})
    assert "No entiendo" in bad.text
    [too_much] = await conv.handle({"type": "text", "text": "900"})
    assert "máximo por orden es 500" in too_much.text
    [confirm] = await conv.handle({"type": "text", "text": "180"})
    assert "¿Confirmas la compra a mercado de 180,00 USDC" in confirm.text
    assert "unos 1,0" in confirm.text and "stop en OKX 9" in confirm.text
    token = confirm.keyboard[0].split("/hth ok ")[1].split(",")[0]
    assert await conv.handle({"type": "text", "text": "hola"}) == []  # no longer waiting
    [done] = await conv.handle({"type": "callback", "data": f"/hth ok {token}"})
    assert "Compra ejecutada" in done.text and store.get("SOL-USDC") is not None
    [again] = await conv.handle({"type": "callback", "data": f"/hth ok {token}"})
    assert "ya no está pendiente" in again.text and len(okx.orders) == 1
    [dup] = await conv.handle({"type": "callback", "data": "/hth buy SOL-USDC"})
    assert "Ya tienes una posición" in dup.text
    [listing] = await conv.handle({"type": "text", "text": "/posiciones"})
    assert "SOL-USDC: 0,9990 SOL" in listing.text and "stop 171" in listing.text


async def test_sell_flow_and_cancel(okx: FakeOkx, store: PositionStore) -> None:
    clock = Clock()
    conv = conversation(store, okx, clock)
    [none] = await conv.handle({"type": "callback", "data": "/hth sell SOL-USDC"})
    assert "No tengo registrada" in none.text
    assert "No hay posiciones" in (await conv.handle({"text": "/posiciones"}))[0].text
    store.add(OpenPosition(pair="SOL-USDC", quantity=1.0, entry_price=150.0, cost=150.0,
                           entry_time=T0))  # fmt: skip
    okx.balance = 1.0
    [confirm] = await conv.handle({"type": "callback", "data": "/hth sell SOL-USDC"})
    assert "venta a mercado de 1,00 SOL" in confirm.text
    token = confirm.keyboard[0].split("/hth no ")[1]
    [cancelled] = await conv.handle({"type": "callback", "data": f"/hth no {token}"})
    assert "Cancelado" in cancelled.text and not okx.orders
    [confirm] = await conv.handle({"type": "callback", "data": "/hth sell SOL-USDC"})
    token = confirm.keyboard[0].split("/hth ok ")[1].split(",")[0]
    [done] = await conv.handle({"type": "callback", "data": f"/hth ok {token}"})
    assert "Venta ejecutada" in done.text and store.get("SOL-USDC") is None


async def test_expiry_errors_and_guards(okx: FakeOkx, store: PositionStore) -> None:
    clock = Clock()
    conv = conversation(store, okx, clock)
    await conv.handle({"data": "/hth buy SOL-USDC"})
    clock.t += timedelta(minutes=11)
    [late] = await conv.handle({"text": "100"})
    assert "caducó" in late.text
    await conv.handle({"data": "/hth buy ETH-USDC"})
    [confirm] = await conv.handle({"text": "100"})
    assert "Pondré un stop" not in confirm.text  # no signal for ETH -> no stop level
    token = confirm.keyboard[0].split("/hth ok ")[1].split(",")[0]
    clock.t += timedelta(minutes=11)
    [stale] = await conv.handle({"data": f"/hth ok {token}"})
    assert "ya no está pendiente" in stale.text

    okx.states = ["live"]  # OKX never fills
    await conv.handle({"data": "/hth buy SOL-USDC"})
    [confirm] = await conv.handle({"text": "100"})
    token = confirm.keyboard[0].split("/hth ok ")[1].split(",")[0]
    [failed] = await conv.handle({"data": f"/hth ok {token}"})
    assert failed.text.startswith("[DEMO] ❌ OKX no ejecutó la orden de SOL-USDC")

    assert "Par no válido" in (await conv.handle({"data": "/hth buy sol"}))[0].text
    assert "Orden desconocida" in (await conv.handle({"data": "/hth foo bar"}))[0].text
    assert await conv.handle({"text": "/start"}) == []


async def test_unexpected_error_is_reported(okx: FakeOkx, store: PositionStore) -> None:
    conv = conversation(store, okx, Clock())

    async def boom(*_: object) -> str:
        raise RuntimeError("kaput")

    conv.executor.buy = boom  # type: ignore[method-assign,union-attr]
    await conv.handle({"data": "/hth buy SOL-USDC"})
    [confirm] = await conv.handle({"text": "100"})
    token = confirm.keyboard[0].split("/hth ok ")[1].split(",")[0]
    [failed] = await conv.handle({"data": f"/hth ok {token}"})
    assert "Error inesperado" in failed.text and "kaput" in failed.text


async def test_disabled_trading_never_orders(okx: FakeOkx, store: PositionStore) -> None:
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert not s.trading_ready
    conv = Conversation(s, None, store, {}.get, now=Clock())
    [reply] = await conv.handle({"data": "/hth buy SOL-USDC"})
    assert reply.text.startswith("La operativa está desactivada")
    conv.pending["t"] = Pending("buy", "SOL-USDC", T0, amount=10)
    [reply] = await conv.handle({"data": "/hth ok t"})
    assert "no se ha enviado" in reply.text and not okx.orders


def test_position_store_roundtrip(store: PositionStore) -> None:
    assert store.load() == {}
    pos = OpenPosition(pair="SOL-USDC", quantity=1, entry_price=1, cost=1, entry_time=T0)
    store.add(pos)
    assert store.load()["SOL-USDC"].as_signal_position().entry_price == 1
    store.remove("SOL-USDC")
    assert store.get("SOL-USDC") is None
