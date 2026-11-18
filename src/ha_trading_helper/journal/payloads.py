"""MQTT Discovery and state payloads of the journal (pure, no I/O).

A few figures for Home Assistant cards and automations; the detail lives in the web panel.

Topics::

    <base>/status               availability
    <base>/summary/state        account value (USDC)
    <base>/summary/attributes   today's result, 30 days, open positions, last sync
"""

from __future__ import annotations

import json
from typing import Any

from ha_trading_helper import __version__
from ha_trading_helper.journal import report
from ha_trading_helper.publish.payloads import Message

SENSORS = (
    # object_id, name, attribute of the summary (None = the state itself), unit, icon
    ("valor", "Valor de la cuenta", None, "USDC", "mdi:wallet"),
    ("resultado_hoy", "Resultado de hoy", "resultado_hoy", "USDC", "mdi:chart-line"),
    ("resultado_hoy_pct", "Resultado de hoy (%)", "resultado_hoy_pct", "%", "mdi:percent"),
    ("resultado_30d", "Resultado 30 días", "resultado_30d", "USDC", "mdi:calendar-month"),
    ("rentabilidad_30d", "Rentabilidad 30 días", "rentabilidad_30d_pct", "%", "mdi:finance"),
    ("posiciones", "Posiciones abiertas", "posiciones", None, "mdi:format-list-bulleted"),
    ("comisiones_30d", "Comisiones 30 días", "comisiones_30d", "USDC", "mdi:cash-minus"),
)


def device(base: str) -> dict[str, Any]:
    return {
        "identifiers": [base],
        "name": "Bitácora OKX",
        "manufacturer": "ha-trading-helper",
        "model": "OKX spot journal",
        "sw_version": __version__,
    }


def discovery_messages(base: str, prefix: str) -> list[Message]:
    msgs = []
    for object_id, name, attr, unit, icon in SENSORS:
        unique = f"{base}_{object_id}"
        body: dict[str, Any] = {
            "name": name,
            "unique_id": unique,
            "default_entity_id": f"sensor.{unique}",
            "availability_topic": f"{base}/status",
            "device": device(base),
            "icon": icon,
            "state_class": "measurement",
        }
        if attr is None:
            body["state_topic"] = f"{base}/summary/state"
            body["json_attributes_topic"] = f"{base}/summary/attributes"
        else:
            body["state_topic"] = f"{base}/summary/attributes"
            body["value_template"] = f"{{{{ value_json.{attr} }}}}"
        if unit:
            body["unit_of_measurement"] = unit
        msgs.append(Message(f"{prefix}/sensor/{base}/{object_id}/config", json.dumps(body)))
    return msgs


def summary(d: report.Data) -> dict[str, Any]:
    ov = report.overview(d)
    month = report.period_view(d, "30d")
    perf = month.perf
    return {
        "valor": round(ov.equity, 2),
        "resultado_hoy": round(ov.pnl_today, 2),
        "resultado_hoy_pct": round(ov.pct_today * 100, 2),
        "resultado_30d": round(perf.profit, 2) if perf else 0.0,
        "rentabilidad_30d_pct": round(perf.twr * 100, 2) if perf else 0.0,
        "comisiones_30d": round(month.fees, 2),
        "posiciones": len(report.positions(d).rows) + sum(b.running for b in d.bots),
        "sincronizado": d.last_sync.get("ts"),
        "errores": d.last_sync.get("errors", []),
    }


def state_messages(d: report.Data, base: str) -> list[Message]:
    data = summary(d)
    return [
        Message(f"{base}/summary/state", str(data["valor"])),
        Message(f"{base}/summary/attributes", json.dumps(data)),
    ]
