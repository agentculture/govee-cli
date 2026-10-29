"""Cloud transport tests against a local fake http.server (never the real API)."""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from govee_cli.govee import cloud
from govee_cli.govee.cloud import (
    CloudError,
    CloudTransport,
    CloudUnavailable,
    MissingKey,
    RateLimited,
    Unauthorized,
)
from govee_cli.govee.types import Device, Transport

FAKE_KEY = "test" + "-" + "value" + "-" + "xyzzy"
DEV = Device(id="AA:BB", name="Lamp", model="H6008", transports=("cloud",))


class _Fake:
    def __init__(self):
        self.requests = []
        self.routes = {}  # (method, path) -> (status, headers, body)


@pytest.fixture
def fake():
    state = _Fake()

    class Handler(BaseHTTPRequestHandler):
        def _handle(self):
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            body = json.loads(raw) if raw else None
            state.requests.append((self.command, self.path, dict(self.headers), body))
            status, headers, out = state.routes.get(
                (self.command, self.path), (404, {}, {"code": 404})
            )
            data = json.dumps(out).encode()
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = _handle

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    state.url = f"http://127.0.0.1:{srv.server_address[1]}/router/api/v1/"
    yield state
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setenv("GOVEE_API_KEY", FAKE_KEY)


def _t(fake):
    return CloudTransport(base_url=fake.url, timeout=5)


def test_is_transport(fake):
    assert isinstance(_t(fake), Transport)


def test_missing_key(fake, monkeypatch):
    monkeypatch.delenv("GOVEE_API_KEY", raising=False)
    with pytest.raises(MissingKey):
        _t(fake).discover()
    assert fake.requests == []
    monkeypatch.setenv("GOVEE_API_KEY", "")
    with pytest.raises(MissingKey):
        _t(fake).discover()


def test_discover(fake, key):
    fake.routes[("GET", "/router/api/v1/user/devices")] = (
        200,
        {},
        {
            "code": 200,
            "data": [
                {"sku": "H6008", "device": "AA:BB", "deviceName": "Desk", "capabilities": []},
                {"sku": "H6009", "device": "CC:DD", "capabilities": []},
            ],
        },
    )
    devs = _t(fake).discover()
    assert devs[0] == Device("AA:BB", "Desk", "H6008", None, ("cloud",))
    assert devs[1].name == "H6009"
    hdrs = {k.lower(): v for k, v in fake.requests[0][2].items()}
    assert hdrs["govee-api-key"] == FAKE_KEY


def test_get_state(fake, key):
    caps = [
        ("online", "devices.capabilities.online", True),
        ("powerSwitch", "devices.capabilities.on_off", 1),
        ("brightness", "devices.capabilities.range", 42),
        ("colorRgb", "devices.capabilities.color_setting", (1 << 16) | (2 << 8) | 3),
        ("colorTemperatureK", "devices.capabilities.color_setting", 0),
    ]
    fake.routes[("POST", "/router/api/v1/device/state")] = (
        200,
        {},
        {
            "code": 200,
            "payload": {
                "capabilities": [
                    {"type": t, "instance": i, "state": {"value": v}} for i, t, v in caps
                ]
            },
        },
    )
    st = _t(fake).get_state(DEV)
    assert st.on is True and st.brightness == 42 and st.color == (1, 2, 3)
    assert st.kelvin is None
    body = fake.requests[0][3]
    assert body["payload"] == {"sku": "H6008", "device": "AA:BB"}
    assert body["requestId"]


def test_get_state_offline_is_none(fake, key):
    fake.routes[("POST", "/router/api/v1/device/state")] = (
        200,
        {},
        {
            "code": 200,
            "payload": {
                "capabilities": [
                    {
                        "type": "devices.capabilities.online",
                        "instance": "online",
                        "state": {"value": False},
                    }
                ]
            },
        },
    )
    assert _t(fake).get_state(DEV) is None


def test_send_builders(fake, key):
    fake.routes[("POST", "/router/api/v1/device/control")] = (200, {}, {"code": 200})
    t = _t(fake)
    cmds = [
        (cloud.power_command(True), "devices.capabilities.on_off", "powerSwitch", 1),
        (cloud.power_command(False), "devices.capabilities.on_off", "powerSwitch", 0),
        (cloud.brightness_command(50), "devices.capabilities.range", "brightness", 50),
        (cloud.color_command(1, 2, 3), "devices.capabilities.color_setting", "colorRgb", 66051),
        (
            cloud.kelvin_command(4000),
            "devices.capabilities.color_setting",
            "colorTemperatureK",
            4000,
        ),
    ]
    for cmd, typ, inst, val in cmds:
        assert cmd.transport == "cloud"
        assert t.send(DEV, cmd) is True
        cap = fake.requests[-1][3]["payload"]["capability"]
        assert cap == {"type": typ, "instance": inst, "value": val}
    p = fake.requests[-1][3]["payload"]
    assert p["sku"] == "H6008" and p["device"] == "AA:BB"


def test_send_rejects_non_cloud_command(fake, key):
    from govee_cli.govee.types import Command

    with pytest.raises(CloudError):
        _t(fake).send(DEV, Command("power", "lan", {}))


def test_send_api_error_code(fake, key):
    fake.routes[("POST", "/router/api/v1/device/control")] = (200, {}, {"code": 400, "msg": "bad"})
    assert _t(fake).send(DEV, cloud.power_command(True)) is False


def test_scenes_and_set_scene(fake, key):
    fake.routes[("POST", "/router/api/v1/device/scenes")] = (
        200,
        {},
        {
            "code": 200,
            "payload": {
                "capabilities": [
                    {
                        "type": "devices.capabilities.dynamic_scene",
                        "instance": "lightScene",
                        "parameters": {
                            "dataType": "ENUM",
                            "options": [
                                {"name": "Sunrise", "value": {"paramId": 4280, "id": 3853}},
                                {"name": "Candle", "value": {"paramId": 1, "id": 2}},
                            ],
                        },
                    }
                ]
            },
        },
    )
    fake.routes[("POST", "/router/api/v1/device/control")] = (200, {}, {"code": 200})
    t = _t(fake)
    scenes = t.scenes(DEV)
    assert [s.name for s in scenes] == ["Sunrise", "Candle"]
    assert t.set_scene(DEV, "sunrise") is True
    cap = fake.requests[-1][3]["payload"]["capability"]
    assert cap == {
        "type": "devices.capabilities.dynamic_scene",
        "instance": "lightScene",
        "value": {"paramId": 4280, "id": 3853},
    }
    with pytest.raises(CloudError):
        t.set_scene(DEV, "nope")


def test_rate_limited_epoch_header(fake, key):
    fake.routes[("GET", "/router/api/v1/user/devices")] = (
        429,
        {"API-RateLimit-Reset": "1800000000"},
        {"code": 429},
    )
    with pytest.raises(RateLimited) as ei:
        _t(fake).discover()
    assert ei.value.reset_at == datetime.fromtimestamp(1800000000, tz=timezone.utc)


def test_rate_limited_retry_after_and_none(fake, key):
    fake.routes[("GET", "/router/api/v1/user/devices")] = (429, {"Retry-After": "30"}, {})
    with pytest.raises(RateLimited) as ei:
        _t(fake).discover()
    delta = (ei.value.reset_at - datetime.now(timezone.utc)).total_seconds()
    assert 20 < delta <= 31
    fake.routes[("GET", "/router/api/v1/user/devices")] = (429, {}, {})
    with pytest.raises(RateLimited) as ei:
        _t(fake).discover()
    assert ei.value.reset_at is None


def test_parse_reset_http_date_and_relative():
    h = {"Retry-After": "Wed, 21 Oct 2037 07:28:00 GMT"}
    assert cloud.parse_reset(h) == datetime(2037, 10, 21, 7, 28, tzinfo=timezone.utc)
    assert cloud.parse_reset({"X-RateLimit-Reset": "garbage"}) is None


@pytest.mark.parametrize("status", [401, 403])
def test_unauthorized(fake, key, status):
    fake.routes[("GET", "/router/api/v1/user/devices")] = (status, {}, {})
    with pytest.raises(Unauthorized):
        _t(fake).discover()


def test_5xx_and_network(fake, key):
    fake.routes[("GET", "/router/api/v1/user/devices")] = (503, {}, {})
    with pytest.raises(CloudUnavailable):
        _t(fake).discover()
    dead = CloudTransport(base_url="http://127.0.0.1:1/router/api/v1/", timeout=2)
    with pytest.raises(CloudUnavailable):
        dead.discover()


def test_rejects_unsafe_base_url():
    for bad in ("http://example.com/", "file:///etc/passwd", "ftp://x/"):
        with pytest.raises(ValueError):
            CloudTransport(base_url=bad)


def test_default_base_url_is_https_constant():
    assert CloudTransport().base_url == cloud.DEFAULT_BASE_URL
    assert cloud.DEFAULT_BASE_URL.startswith("https://")


def test_key_never_leaks(fake, key, caplog):
    caplog.set_level(logging.DEBUG)
    errors = []
    t = _t(fake)
    for status, hdrs in [(401, {}), (429, {"Retry-After": "5"}), (500, {})]:
        fake.routes[("GET", "/router/api/v1/user/devices")] = (status, hdrs, {"echo": FAKE_KEY})
        try:
            t.discover()
        except CloudError as e:
            errors.append(e)
    try:
        CloudTransport(base_url="http://127.0.0.1:1/router/api/v1/", timeout=1).discover()
    except CloudError as e:
        errors.append(e)
    assert len(errors) == 4
    for e in errors:
        assert FAKE_KEY not in str(e) and FAKE_KEY not in repr(e)
    assert FAKE_KEY not in caplog.text
    assert FAKE_KEY not in repr(t)
    try:
        import os

        os.environ.pop("GOVEE_API_KEY")
        t.discover()
    except MissingKey as e:
        assert FAKE_KEY not in str(e)
