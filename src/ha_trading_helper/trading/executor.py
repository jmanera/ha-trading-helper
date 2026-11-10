"""Turn a confirmed buy or sell into OKX orders and keep the position record in sync."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Protocol

import structlog

from ha_trading_helper.okx.client import OkxError
from ha_trading_helper.okx.trade import LotRules, OrderFill
from ha_trading_helper.strategy.explain import fmt
from ha_trading_helper.trading.positions import OpenPosition, PositionStore

log = structlog.get_logger(__name__)

FILL_POLLS = 10
FILL_POLL_SECONDS = 0.5


class TradeClient(Protocol):
    async def lot_rules(self, inst_id: str) -> LotRules: ...
    async def market_order(self, inst_id: str, side: str, size: str, *, quote: bool) -> str: ...
    async def order(self, inst_id: str, order_id: str) -> OrderFill: ...
    async def stop_loss(self, inst_id: str, size: str, trigger_price: float) -> str: ...
    async def cancel_stop(self, inst_id: str, algo_id: str) -> None: ...
    async def available(self, ccy: str) -> float: ...


class Executor:
    def __init__(
        self,
        client: TradeClient,
        store: PositionStore,
        *,
        stop_on_exchange: bool = True,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.client = client
        self.store = store
        self.stop_on_exchange = stop_on_exchange
        self._sleep = sleep

    async def _wait_fill(self, pair: str, order_id: str) -> OrderFill:
        fill = await self.client.order(pair, order_id)
        for _ in range(FILL_POLLS):
            if fill.done:
                break
            await self._sleep(FILL_POLL_SECONDS)
            fill = await self.client.order(pair, order_id)
        if fill.filled <= 0:
            raise OkxError(f"la orden {order_id} no se ejecutó (estado {fill.state})")
        return fill

    async def buy(self, pair: str, amount: float, risk_per_unit: float | None) -> str:
        """Market buy for ``amount`` of the quote currency, then a stop below the fill."""
        base, quote = pair.split("-")
        order_id = await self.client.market_order(pair, "buy", f"{amount:.8g}", quote=True)
        fill = await self._wait_fill(pair, order_id)
        # Spot buys pay the fee in the base currency (negative = charged).
        held = fill.filled + (fill.fee if fill.fee_ccy == base else 0.0)
        cost = fill.filled * fill.avg_price
        log.info("buy_filled", pair=pair, order_id=order_id, qty=held, price=fill.avg_price)
        lines = [
            f"✅ Compra ejecutada: {fmt(held)} {base} de {pair} a {fmt(fill.avg_price)} "
            f"({fmt(cost, 2)} {quote})."
        ]

        position = OpenPosition(
            pair=pair,
            quantity=held,
            entry_price=fill.avg_price,
            cost=cost,
            entry_time=datetime.now(UTC),
        )
        if self.stop_on_exchange and risk_per_unit and risk_per_unit < fill.avg_price:
            stop_price = fill.avg_price - risk_per_unit
            try:
                rules = await self.client.lot_rules(pair)
                size = rules.floor(held)
                algo_id = await self.client.stop_loss(pair, str(size), stop_price)
            except OkxError as exc:
                log.warning("stop_failed", pair=pair, error=str(exc))
                lines.append(f"⚠️ No se pudo poner el stop en OKX: {exc}. Ponlo a mano.")
            else:
                position.stop_price, position.stop_algo_id = stop_price, algo_id
                lines.append(f"🛑 Stop en OKX a {fmt(stop_price)} para {size} {base}.")
        elif self.stop_on_exchange:
            lines.append("⚠️ Sin stop en OKX: la señal ya no da un nivel de stop.")
        self.store.add(position)
        return "\n".join(lines)

    async def sell(self, pair: str) -> str:
        """Cancel the exchange stop and sell the recorded quantity at market."""
        position = self.store.get(pair)
        if position is None:
            return f"No tengo registrada ninguna posición abierta en {pair}."
        base, quote = pair.split("-")
        notes: list[str] = []
        if position.stop_algo_id:
            try:
                await self.client.cancel_stop(pair, position.stop_algo_id)
            except OkxError as exc:
                # Usually it already triggered or was cancelled by hand.
                notes.append(f"(El stop no se pudo cancelar: {exc})")

        rules = await self.client.lot_rules(pair)
        size = rules.floor(min(position.quantity, await self.client.available(base)))
        if size < rules.min_size:
            self.store.remove(pair)
            return (
                f"No queda saldo de {base} suficiente para vender. Puede que el stop ya se "
                f"ejecutara en OKX; he dado la posición de {pair} por cerrada."
            )
        order_id = await self.client.market_order(pair, "sell", str(size), quote=False)
        fill = await self._wait_fill(pair, order_id)
        proceeds = fill.filled * fill.avg_price + (fill.fee if fill.fee_ccy == quote else 0.0)
        cost = position.cost * min(fill.filled / position.quantity, 1.0)
        pnl = proceeds - cost
        log.info("sell_filled", pair=pair, order_id=order_id, qty=fill.filled, pnl=pnl)
        self.store.remove(pair)
        sign = "+" if pnl >= 0 else ""
        lines = [
            f"✅ Venta ejecutada: {fmt(fill.filled)} {base} de {pair} a {fmt(fill.avg_price)} "
            f"({fmt(proceeds, 2)} {quote}).",
            f"Resultado: {sign}{fmt(pnl, 2)} {quote} "
            f"({sign}{fmt(pnl / cost * 100, 1)} %) desde la entrada a "
            f"{fmt(position.entry_price)}.",
            *notes,
        ]
        return "\n".join(lines)
