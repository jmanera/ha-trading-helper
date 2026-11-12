"""MQTT Discovery and state payloads of the intraday monitor (pure, no I/O).

Topics::

    <base>/status                    availability
    <base>/system/state|attributes   health of the monitor
    <base>/alerts/state|attributes   alerts in the last 24 h and the latest ones
    <base>/alerts/event              one message per new alert (event entity, not retained)
    <base>/rule_<key>/state|attributes  measurement of each rule (closed trades + stats)
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from ha_trading_helper import __version__
from ha_trading_helper.intraday.alerts import KINDS, Alert
from ha_trading_helper.intraday.paper import Stats, Trade, signed
from ha_trading_helper.intraday.rules import RULES, RULES_VERSION, Rule
from ha_trading_helper.publish.payloads import Message
from ha_trading_helper.strategy.explain import fmt


def device(base: str) -> dict[str, Any]:
    return {
        "identifiers": [base],
        "name": "Monitor intradía",
        "manufacturer": "ha-trading-helper",
        "model": "OKX intraday monitor",
        "sw_version": __version__,
    }


def _config(component: str, object_id: str, name: str, base: str, prefix: str,
            **extra: Any) -> Message:  # fmt: skip
    unique = f"{base}_{object_id}"
    body = {
        "name": name,
        "unique_id": unique,
        "default_entity_id": f"{component}.{unique}",
        "availability_topic": f"{base}/status",
        "device": device(base),
        **extra,
    }
    return Message(f"{prefix}/{component}/{base}/{object_id}/config", json.dumps(body))


def discovery_messages(base: str, prefix: str) -> list[Message]:
    msgs = [
        _config("sensor", "estado", "Estado", base, prefix,
                state_topic=f"{base}/system/state",
                json_attributes_topic=f"{base}/system/attributes",
                icon="mdi:heart-pulse", entity_category="diagnostic"),
        _config("sensor", "alertas_24h", "Alertas 24 h", base, prefix,
                state_topic=f"{base}/alerts/state",
                json_attributes_topic=f"{base}/alerts/attributes",
                icon="mdi:bell-alert"),
        _config("event", "alerta", "Alerta", base, prefix,
                state_topic=f"{base}/alerts/event", event_types=list(KINDS),
                icon="mdi:bell-ring"),
    ]  # fmt: skip
    for rule in RULES:
        msgs.append(
            _config("sensor", f"regla_{rule.key}", f"Regla {rule.name}", base, prefix,
                    state_topic=f"{base}/rule_{rule.key}/state",
                    json_attributes_topic=f"{base}/rule_{rule.key}/attributes",
                    unit_of_measurement="operaciones", state_class="total",
                    icon="mdi:dice-5" if rule.control_of else "mdi:flask")
        )  # fmt: skip
    return msgs


def alert_event(a: Alert, base: str) -> Message:
    body = {"event_type": a.kind, "par": a.pair, "texto": a.text, "precio": a.price,
            "vela": a.time.isoformat()}  # fmt: skip
    return Message(f"{base}/alerts/event", json.dumps(body), retain=False)


def alerts_messages(recent: list[Alert], count_24h: int, base: str) -> list[Message]:
    attrs = {
        "alertas": [
            {"hora": a.time.isoformat(), "par": a.pair, "tipo": a.kind, "texto": a.text}
            for a in recent
        ]
    }
    return [
        Message(f"{base}/alerts/state", str(count_24h)),
        Message(f"{base}/alerts/attributes", json.dumps(attrs)),
    ]


def rule_attributes(rule: Rule, st: Stats, verdict: tuple[str, str] | None, started: datetime,
                    closed: list[Trade]) -> dict[str, Any]:  # fmt: skip
    attrs: dict[str, Any] = {
        "nombre": rule.name,
        "descripcion": rule.description,
        "velas": rule.bar,
        "version": RULES_VERSION,
        "desde": started.isoformat(),
        "cerradas": st.closed,
        "abiertas": st.open,
        "aciertos": round(st.win_rate * 100, 1),
        "media_pct": round(st.mean_pct, 4),
        "media_r": round(st.mean_r, 3),
        "ic95_pct": [round(st.ci_pct[0], 4), round(st.ci_pct[1], 4)],
        "total_pct": round(st.total_pct, 3),
        "resumen": (
            f"{st.closed} cerradas · {fmt(st.win_rate * 100, 0)} % aciertos · "
            f"media {signed(st.mean_pct)} % tras costes"
        ),
        "ultimas": [
            {
                "par": t.pair,
                "entrada": t.entry_time.isoformat() if t.entry_time else None,
                "resultado_pct": round(t.ret_pct, 3),
                "motivo": t.exit_reason,
            }
            for t in closed[-5:]
        ],
    }
    if verdict is not None:
        attrs["veredicto"], attrs["veredicto_texto"] = verdict
    else:
        attrs["veredicto"], attrs["veredicto_texto"] = "control", "Referencia al azar."
    return attrs


def rule_messages(rule: Rule, attrs: dict[str, Any], base: str) -> list[Message]:
    return [
        Message(f"{base}/rule_{rule.key}/state", str(attrs["cerradas"])),
        Message(f"{base}/rule_{rule.key}/attributes", json.dumps(attrs)),
    ]


def system_messages(health: str, attrs: dict[str, Any], base: str) -> list[Message]:
    return [
        Message(f"{base}/system/state", health),
        Message(f"{base}/system/attributes", json.dumps(attrs)),
    ]
