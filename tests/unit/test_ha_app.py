from __future__ import annotations

import httpx
import pytest

from ha_trading_helper.config import Settings
from ha_trading_helper.ha_app import with_supervisor_mqtt


def base() -> Settings:
    return Settings(_env_file=None)  # type: ignore[call-arg]


def client(handler: httpx.MockTransport) -> httpx.Client:
    return httpx.Client(base_url="http://supervisor", transport=handler)


def test_without_supervisor_token_settings_are_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    s = base()
    assert with_supervisor_mqtt(s) is s


def test_uses_supervisor_broker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPERVISOR_TOKEN", "t0ken")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer t0ken"
        assert request.url.path == "/services/mqtt"
        data = {
            "host": "core-mosquitto",
            "port": 1883,
            "username": "addons",
            "password": "pw",
        }  # pragma: allowlist secret
        return httpx.Response(200, json={"data": data})

    s = with_supervisor_mqtt(base(), http=client(httpx.MockTransport(handler)))
    assert (s.mqtt_host, s.mqtt_port, s.mqtt_username, s.mqtt_password) == (
        "core-mosquitto", 1883, "addons", "pw",
    )  # fmt: skip


def test_explicit_host_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPERVISOR_TOKEN", "t")
    s = Settings(_env_file=None, mqtt_host="10.0.0.2")  # type: ignore[call-arg]
    assert with_supervisor_mqtt(s).mqtt_host == "10.0.0.2"


@pytest.mark.parametrize(
    "response",
    [httpx.Response(503), httpx.Response(200, json={"data": {}}), httpx.Response(200, text="x")],
)
def test_supervisor_failures_keep_defaults(
    monkeypatch: pytest.MonkeyPatch, response: httpx.Response
) -> None:
    monkeypatch.setenv("SUPERVISOR_TOKEN", "t")
    s = with_supervisor_mqtt(base(), http=client(httpx.MockTransport(lambda _: response)))
    assert s.mqtt_host == "localhost"
