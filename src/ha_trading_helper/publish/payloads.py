"""MQTT Discovery configs and state/attribute payloads (pure, no I/O).

All entities hang from one device "HA Trading Helper". Topics::

    <base>/status                      availability (online/offline, retained)
    <base>/system/state|attributes     health sensor
    <base>/best/state|attributes       best opportunity
    <base>/<slug>/state|attributes     per-pair score (attributes carry the reasons)
    <base>/<slug>/entry                ON/OFF entry flag
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ha_trading_helper import __version__
from ha_trading_helper.strategy.explain import fmt
from ha_trading_helper.strategy.signal import Action, Signal

MAX_ATTRIBUTES_BYTES = 4096
ICONS = {Action.ENTER: "mdi:arrow-up-bold-circle", Action.WAIT: "mdi:pause-circle",
         Action.EXIT: "mdi:arrow-down-bold-circle"}  # fmt: skip


@dataclass(frozen=True)
class Message:
    topic: str
    payload: str
    retain: bool = True


def slug(pair: str) -> str:
    return pair.lower().replace("-", "_")


def device(base: str) -> dict[str, Any]:
    return {
        "identifiers": [base],
        "name": "HA Trading Helper",
        "manufacturer": "ha-trading-helper",
        "model": "OKX spot 4H signals",
        "sw_version": __version__,
    }


def _config(
    component: str, object_id: str, name: str, base: str, prefix: str, **extra: Any
) -> Message:
    unique = f"{base}_{object_id}"
    body: dict[str, Any] = {
        "name": name,
        "unique_id": unique,
        "default_entity_id": f"{component}.{unique}",
        "availability_topic": f"{base}/status",
        "device": device(base),
        **extra,
    }
    return Message(f"{prefix}/{component}/{base}/{object_id}/config", json.dumps(body))


def discovery_messages(pairs: list[str], base: str, prefix: str) -> list[Message]:
    msgs = [
        _config(
            "sensor", "estado", "Estado", base, prefix,
            state_topic=f"{base}/system/state",
            json_attributes_topic=f"{base}/system/attributes",
            icon="mdi:heart-pulse", entity_category="diagnostic",
        ),
        _config(
            "sensor", "mejor_oportunidad", "Mejor oportunidad", base, prefix,
            state_topic=f"{base}/best/state",
            json_attributes_topic=f"{base}/best/attributes",
            icon="mdi:trophy",
        ),
    ]  # fmt: skip
    for pair in pairs:
        s = slug(pair)
        msgs.append(
            _config(
                "sensor", f"{s}_puntuacion", f"{pair} puntuación", base, prefix,
                state_topic=f"{base}/{s}/state",
                json_attributes_topic=f"{base}/{s}/attributes",
                unit_of_measurement="pts", state_class="measurement",
                icon="mdi:chart-line",
            )
        )  # fmt: skip
        msgs.append(
            _config(
                "binary_sensor", f"{s}_entrada", f"{pair} entrada", base, prefix,
                state_topic=f"{base}/{s}/entry",
                payload_on="ON", payload_off="OFF",
                icon="mdi:bell-ring",
            )
        )  # fmt: skip
    return msgs


def removal_messages(pairs: list[str], base: str, prefix: str) -> list[Message]:
    """Empty retained configs remove entities of pairs that left the universe."""
    out = []
    for pair in pairs:
        s = slug(pair)
        out.append(Message(f"{prefix}/sensor/{base}/{s}_puntuacion/config", ""))
        out.append(Message(f"{prefix}/binary_sensor/{base}/{s}_entrada/config", ""))
    return out


def signal_attributes(sig: Signal, wait_threshold: int = 40) -> dict[str, Any]:
    attrs: dict[str, Any] = {
        "par": sig.pair,
        "accion": sig.action.value,
        "puntuacion": sig.score,
        # ESPERAR with a score >= wait_threshold is worth watching.
        "vigilar": sig.action is not Action.WAIT or sig.score >= wait_threshold,
        "alerta": sig.alert,
        "icono": ICONS[sig.action],
        "vela_cierre": sig.candle_close.isoformat(),
        "cierre": sig.close,
        "motivos": [
            {"e": r.label, "p": r.points, "max": r.max_points, "d": r.detail} for r in sig.reasons
        ],
        "mercado": sig.market,
        "mercado_ok": sig.market_ok,
        "validacion": sig.validation.text() if sig.validation else "Sin backtest aún",
    }
    if sig.exit_reason:
        attrs["salida"] = sig.exit_reason
    if sig.levels:
        lv = sig.levels
        attrs["niveles"] = {
            "entrada": round(lv.entry, 8),
            "stop": round(lv.stop, 8),
            "objetivo1": round(lv.target1, 8),
            "objetivo2": round(lv.target2, 8),
            "tamano": round(lv.size, 8),
            "importe": round(lv.notional, 2),
        }
        attrs["resumen_niveles"] = (
            f"entrada {fmt(lv.entry)} · stop {fmt(lv.stop)} · "
            f"objetivos {fmt(lv.target1)} / {fmt(lv.target2)}"
        )
    return attrs


def ranking(signals: list[Signal]) -> list[Signal]:
    order = {Action.ENTER: 0, Action.EXIT: 1, Action.WAIT: 2}
    return sorted(signals, key=lambda s: (order[s.action], -s.score, s.pair))


def state_messages(
    signals: list[Signal],
    base: str,
    *,
    now: datetime,
    errors: list[str],
    universe: list[str],
    wait_threshold: int = 40,
) -> list[Message]:
    msgs: list[Message] = []
    ranked = ranking(signals)
    for sig in signals:
        s = slug(sig.pair)
        msgs.append(Message(f"{base}/{s}/state", str(sig.score)))
        msgs.append(
            Message(f"{base}/{s}/attributes", json.dumps(signal_attributes(sig, wait_threshold)))
        )
        msgs.append(Message(f"{base}/{s}/entry", "ON" if sig.action is Action.ENTER else "OFF"))

    if ranked:
        best = ranked[0]
        best_attrs = signal_attributes(best, wait_threshold)
        best_attrs["ranking"] = [
            {"par": s.pair, "accion": s.action.value, "puntuacion": s.score} for s in ranked[:10]
        ]
        msgs.append(Message(f"{base}/best/state", best.pair))
        msgs.append(Message(f"{base}/best/attributes", json.dumps(best_attrs)))
    else:
        msgs.append(Message(f"{base}/best/state", "ninguna"))
        msgs.append(Message(f"{base}/best/attributes", json.dumps({"ranking": []})))

    if errors and not signals:
        health = "error"
    elif errors:
        health = "degradada"
    else:
        health = "ok"
    last_candle = max((s.candle_close for s in signals), default=None)
    validation = next((s.validation for s in signals if s.validation), None)
    system = {
        "ultima_ejecucion": now.isoformat(),
        "ultima_vela": last_candle.isoformat() if last_candle else None,
        "pares_analizados": len(signals),
        "universo": universe,
        "errores": errors[:5],
        "validacion": validation.text() if validation else "Sin backtest aún",
        "validacion_positiva": bool(validation and validation.positive),
        "version": __version__,
    }
    msgs.append(Message(f"{base}/system/state", health))
    msgs.append(Message(f"{base}/system/attributes", json.dumps(system)))
    return msgs
