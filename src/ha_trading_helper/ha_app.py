"""Glue for running as a Home Assistant app (formerly "add-on")."""

from __future__ import annotations

import os

import httpx
import structlog

from ha_trading_helper.config import Settings

SUPERVISOR_URL = "http://supervisor"
log = structlog.get_logger(__name__)


def with_supervisor_mqtt(settings: Settings, *, http: httpx.Client | None = None) -> Settings:
    """Use the broker the Supervisor announces (Mosquitto app) unless one was configured.

    Needs ``services: [mqtt:need]`` in the app's config.yaml.
    """
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token or "mqtt_host" in settings.model_fields_set:
        return settings
    client = http or httpx.Client(base_url=SUPERVISOR_URL, timeout=10.0)
    try:
        response = client.get("/services/mqtt", headers={"Authorization": f"Bearer {token}"})
        response.raise_for_status()
        data = response.json().get("data", {})
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("supervisor_mqtt_unavailable", error=str(exc))
        return settings
    finally:
        if http is None:
            client.close()
    if not data.get("host"):
        return settings
    log.info("supervisor_mqtt", host=data["host"], port=data.get("port"))
    return settings.model_copy(
        update={
            "mqtt_host": data["host"],
            "mqtt_port": int(data.get("port", 1883)),
            "mqtt_username": data.get("username") or None,
            "mqtt_password": data.get("password") or None,
        }
    )
