"""Telegram conversation for buying and selling, relayed by Home Assistant over MQTT.

Home Assistant owns the bot. It forwards button presses (``/hth ...`` callback data)
and plain text messages here; the replies go back as ``Reply`` objects that HA sends
with ``telegram_bot.send_message``. Every order needs an explicit Confirmar press.
With ``require_validation`` (the default) buying is refused while the walk-forward
validation is not positive; selling always works.

Callback data (Telegram limits it to 64 bytes):
    /hth buy SOL-USDC     ask for the amount to invest
    /hth sell SOL-USDC    ask to confirm selling the recorded position
    /hth ok <token>       execute the pending order
    /hth no <token>       drop it
"""

from __future__ import annotations

import asyncio
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from pydantic import BaseModel, Field

from ha_trading_helper.config import Settings
from ha_trading_helper.okx.client import OkxError
from ha_trading_helper.strategy.explain import fmt
from ha_trading_helper.strategy.signal import Signal
from ha_trading_helper.trading.executor import Executor
from ha_trading_helper.trading.positions import PositionStore

log = structlog.get_logger(__name__)

PENDING_TTL = timedelta(minutes=10)
PAIR_RE = re.compile(r"^[A-Z0-9]+-(USDT|USDC|EUR)$")
AMOUNT_RE = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s*(?:usdc|usdt|eur|€|\$)?\s*$", re.IGNORECASE)


class Reply(BaseModel):
    text: str
    # Rows for telegram_bot's inline_keyboard, e.g. "Confirmar:/hth ok ab12, Cancelar:/hth no ab12"
    keyboard: list[str] = Field(default_factory=list)


@dataclass
class Pending:
    kind: str  # buy | sell
    pair: str
    created: datetime
    amount: float = 0.0
    risk_per_unit: float | None = None


def parse_amount(text: str) -> float | None:
    match = AMOUNT_RE.match(text)
    return float(match.group(1).replace(",", ".")) if match else None


class Conversation:
    def __init__(
        self,
        settings: Settings,
        executor: Executor | None,
        store: PositionStore,
        latest_signal: Callable[[str], Signal | None],
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.settings = settings
        self.executor = executor
        self.store = store
        self.latest_signal = latest_signal
        self.now = now
        self.awaiting_amount: Pending | None = None
        self.pending: dict[str, Pending] = {}
        self._lock = asyncio.Lock()

    def _reply(self, text: str, keyboard: list[str] | None = None) -> Reply:
        prefix = "[DEMO] " if self.settings.trading_ready and self.settings.okx_demo else ""
        return Reply(text=f"{prefix}{text}", keyboard=keyboard or [])

    def _fresh(self, p: Pending | None) -> bool:
        return p is not None and self.now() - p.created <= PENDING_TTL

    def _confirm(self, pending: Pending, text: str) -> Reply:
        token = secrets.token_hex(4)
        self.pending = {t: p for t, p in self.pending.items() if self._fresh(p)}
        self.pending[token] = pending
        return self._reply(text, [f"Confirmar:/hth ok {token}, Cancelar:/hth no {token}"])

    async def handle(self, event: dict[str, Any]) -> list[Reply]:
        text = str(event.get("data") or event.get("text") or "").strip()
        if text == "/posiciones":
            return [self._positions()]
        parts = text.split()
        if parts[:1] == ["/hth"] and len(parts) == 3:
            return await self._command(parts[1], parts[2])
        if self.awaiting_amount is not None and parts and not text.startswith("/"):
            return [self._amount(self.awaiting_amount, text)]
        return []  # any other chat message is not for us

    async def _command(self, verb: str, arg: str) -> list[Reply]:
        if verb in ("buy", "sell"):
            if not self.settings.trading_ready:
                return [self._reply(
                    "La operativa está desactivada. Actívala y pon las claves de OKX en la "
                    "configuración de la app HA Trading Helper."
                )]  # fmt: skip
            if not PAIR_RE.match(arg):
                return [self._reply(f"Par no válido: {arg}")]
            return [self._ask_amount(arg) if verb == "buy" else self._ask_sell(arg)]
        if verb == "no":
            self.pending.pop(arg, None)
            return [self._reply("Cancelado. No se ha enviado ninguna orden.")]
        if verb == "ok":
            pending = self.pending.pop(arg, None)
            if pending is None or not self._fresh(pending):
                return [self._reply("Esa orden ya no está pendiente (caducada o ya procesada).")]
            return [await self._execute(pending)]
        return [self._reply(f"Orden desconocida: {verb}")]

    def _buy_blocked(self, pair: str) -> str | None:
        """Why buying ``pair`` is refused, or None. Selling is never blocked."""
        if not self.settings.require_validation:
            return None
        signal = self.latest_signal(pair)
        validation = signal.validation if signal is not None else None
        if validation is not None and validation.positive:
            return None
        detail = validation.text() if validation is not None else "sin backtest aún"
        return (
            f"No compro {pair}: la estrategia no está validada fuera de muestra ({detail}), "
            "así que sus señales son solo informativas. Puedes seguir vendiendo tus posiciones."
        )

    def _ask_amount(self, pair: str) -> Reply:
        if self.store.get(pair) is not None:
            return self._reply(f"Ya tienes una posición abierta en {pair}.")
        if blocked := self._buy_blocked(pair):
            return self._reply(blocked)
        signal = self.latest_signal(pair)
        risk = signal.levels.risk_per_unit if signal and signal.levels else None
        self.awaiting_amount = Pending("buy", pair, self.now(), risk_per_unit=risk)
        quote = pair.split("-")[1]
        return self._reply(
            f"¿Cuántos {quote} quieres invertir en {pair}? Escribe solo el importe "
            f"(máximo {fmt(self.settings.max_order_amount, 0)} {quote})."
        )

    def _amount(self, waiting: Pending, text: str) -> Reply:
        if not self._fresh(waiting):
            self.awaiting_amount = None
            return self._reply("La petición de compra caducó. Pulsa Comprar otra vez.")
        quote = waiting.pair.split("-")[1]
        amount = parse_amount(text)
        if amount is None or amount <= 0:
            return self._reply(f"No entiendo «{text}». Escribe solo el importe, p. ej. 150.")
        if amount > self.settings.max_order_amount:
            return self._reply(
                f"El máximo por orden es {fmt(self.settings.max_order_amount, 0)} {quote}. "
                "Escribe un importe menor o cambia el límite en la configuración."
            )
        self.awaiting_amount = None
        waiting.amount = amount
        signal = self.latest_signal(waiting.pair)
        lines = [f"¿Confirmas la compra a mercado de {fmt(amount, 2)} {quote} de {waiting.pair}?"]
        if signal is not None:
            base = waiting.pair.split("-")[0]
            lines.append(
                f"Último cierre {fmt(signal.close)} → unos {fmt(amount / signal.close)} {base}."
            )
        if self.settings.stop_loss_on_exchange and waiting.risk_per_unit:
            lines.append(
                f"Pondré un stop en OKX {fmt(waiting.risk_per_unit)} por debajo del precio "
                "de compra."
            )
        return self._confirm(waiting, "\n".join(lines))

    def _ask_sell(self, pair: str) -> Reply:
        position = self.store.get(pair)
        if position is None:
            return self._reply(
                f"No tengo registrada ninguna posición abierta en {pair}. Si la compraste "
                "fuera de Telegram, véndela en OKX."
            )
        return self._confirm(
            Pending("sell", pair, self.now()),
            f"¿Confirmas la venta a mercado de {fmt(position.quantity)} {position.base} de "
            f"{pair} (comprados a {fmt(position.entry_price)})?",
        )

    async def _execute(self, pending: Pending) -> Reply:
        if self.executor is None or not self.settings.trading_ready:
            return self._reply("La operativa está desactivada; no se ha enviado la orden.")
        if pending.kind == "buy" and (blocked := self._buy_blocked(pending.pair)):
            return self._reply(blocked)  # validation turned negative while confirming
        async with self._lock:
            try:
                if pending.kind == "buy":
                    text = await self.executor.buy(
                        pending.pair, pending.amount, pending.risk_per_unit
                    )
                else:
                    text = await self.executor.sell(pending.pair)
            except OkxError as exc:
                log.warning("order_rejected", pair=pending.pair, kind=pending.kind, error=str(exc))
                return self._reply(f"❌ OKX no ejecutó la orden de {pending.pair}: {exc}")
            except Exception as exc:
                log.exception("order_failed", pair=pending.pair, kind=pending.kind)
                return self._reply(
                    f"❌ Error inesperado con {pending.pair}: {exc}. Revisa la orden en OKX."
                )
        return self._reply(text)

    def _positions(self) -> Reply:
        positions = self.store.load()
        if not positions:
            return self._reply("No hay posiciones abiertas registradas.")
        lines = ["Posiciones abiertas:"]
        for p in positions.values():
            stop = f" · stop {fmt(p.stop_price)}" if p.stop_price else ""
            lines.append(
                f"{p.pair}: {fmt(p.quantity)} {p.base} a {fmt(p.entry_price)}"
                f" ({fmt(p.cost, 2)} {p.pair.split('-')[1]}){stop}"
            )
        return self._reply("\n".join(lines))
