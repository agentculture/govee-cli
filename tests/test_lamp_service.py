"""LampService (t7): one code path to the lamp, dry run by default, verified and logged writes."""

from __future__ import annotations

import json
import os
import socket
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from govee_cli.cli._errors import CliError
from govee_cli.govee import service as svc
from govee_cli.govee.cloud import (
    CloudUnavailable,
    MissingKey,
    RateLimited,
    Scene,
    Unauthorized,
)
from govee_cli.govee.fake import FakeTransport
from govee_cli.govee.lan import LanTransport, build_color, build_turn
from govee_cli.govee.service import ApplyResult, Change, LampService
from govee_cli.govee.types import Command, Device, LampState
from govee_cli.stats import store

LAN_DEV = Device(id="AA:BB", name="H6008", model="H6008", ip="192.0.2.10", transports=("lan",))
CLOUD_DEV = Device(id="AA:BB", name="Desk Lamp", model="H6008", transports=("cloud",))
OTHER_CLOUD = Device(id="CC:DD", name="Hall Lamp", model="H6008", transports=("cloud",))
OLD = LampState(on=False, brightness=10, color=(0, 0, 255))


# --- fixtures and fakes ----------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("GOVEE_TOKEN", raising=False)
    monkeypatch.delenv("GOVEE_API_KEY", raising=False)
    # These t7 tests pin instant on/off; default fades (d11) are covered in tests/test_fade.py.
    monkeypatch.setenv("GOVEE_FADE_DEFAULT", "0")
    # Never detect a real `govee serve` that may run on the developer's machine.
    monkeypatch.setenv("GOVEE_SERVE_URL", f"http://127.0.0.1:{_free_port()}")


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


class FakeCloud(FakeTransport):
    """Cloud fake with scenes."""

    def __init__(self, *args, scenes=None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._scenes = scenes if scenes is not None else [Scene("Sunset", {"paramId": 1, "id": 7})]

    def scenes(self, device):
        return list(self._scenes)


class RaisingCloud(FakeCloud):
    """Every cloud call raises ``exc`` (MissingKey, RateLimited, ...)."""

    def __init__(self, exc: Exception) -> None:
        super().__init__()
        self.exc = exc

    def discover(self):
        raise self.exc

    def get_state(self, device):
        raise self.exc

    def send(self, device, command):
        raise self.exc

    def scenes(self, device):
        raise self.exc


class SequenceLan(FakeTransport):
    """get_state returns the given states in order, then repeats the last one."""

    def __init__(self, devices, states) -> None:
        super().__init__(devices)
        self.states = list(states)
        self.polls = 0

    def get_state(self, device):
        self.polls += 1
        return self.states.pop(0) if len(self.states) > 1 else self.states[0]


class Untouchable:
    """A transport that fails the test if anything touches it."""

    def __getattr__(self, name):
        raise AssertionError(f"local transport touched: {name}")


def make(lan=None, cloud=None, *, source="cli", actor="alice", clock=None):
    clock = clock or FakeClock()
    return LampService.direct(
        source=source,
        actor=actor,
        lan=lan if lan is not None else FakeTransport([LAN_DEV]),
        cloud=cloud if cloud is not None else RaisingCloud(MissingKey()),
        clock=clock,
        sleep=clock.sleep,
    )


# --- Change ----------------------------------------------------------------------------------


def test_change_rejects_color_and_kelvin_together():
    with pytest.raises(ValueError):
        Change(color=(1, 2, 3), kelvin=3000)


def test_change_rejects_empty_and_out_of_range():
    with pytest.raises(ValueError):
        Change()
    with pytest.raises(ValueError):
        Change(brightness=0)
    with pytest.raises(ValueError):
        Change(kelvin=1000)
    with pytest.raises(ValueError):
        Change(color=(256, 0, 0))


def test_parse_hex_color():
    assert svc.parse_hex_color("FF6A2B") == (255, 106, 43)
    assert svc.parse_hex_color("#ff6a2b") == (255, 106, 43)
    with pytest.raises(ValueError):
        svc.parse_hex_color("nothex")


# --- dry run vs apply (c4 / h4) --------------------------------------------------------------


def test_dry_run_sends_nothing_and_returns_plan():
    lan, cloud = FakeTransport([LAN_DEV]), FakeCloud([CLOUD_DEV])
    result = make(lan, cloud).apply(Change(on=True, color=(255, 106, 43)))
    assert isinstance(result, ApplyResult)
    assert result.applied is False
    assert result.status == "dry_run"
    assert result.commands == [build_turn(True), build_color((255, 106, 43))]
    assert lan.sent == [] and cloud.sent == []
    assert result.cli == ["govee light on --apply", "govee light color FF6A2B --apply"]


def test_apply_sends_exactly_the_previewed_commands():
    lan = FakeTransport([LAN_DEV], state=LampState(on=True, brightness=40, color=(255, 106, 43)))
    service = make(lan)
    change = Change(on=True, brightness=40, color=(255, 106, 43))
    preview = service.apply(change)
    result = service.apply(change, apply=True)
    assert result.applied is True
    assert result.commands == preview.commands
    assert [c for _, c in lan.sent] == preview.commands
    assert all(d.id == LAN_DEV.id for d, _ in lan.sent)


def test_turning_off_sends_turn_last():
    result = make().apply(Change(on=False, brightness=20))
    assert [c.verb for c in result.commands] == ["brightness", "turn"]


# --- LAN first, cloud fallback, scenes always cloud ------------------------------------------


def test_lan_device_uses_lan_commands():
    result = make(FakeTransport([LAN_DEV]), FakeCloud([CLOUD_DEV])).apply(Change(brightness=50))
    assert [c.transport for c in result.commands] == ["lan"]


def test_cloud_fallback_when_lan_finds_nothing():
    cloud = FakeCloud([CLOUD_DEV])
    result = make(FakeTransport([]), cloud).apply(Change(kelvin=2700), apply=True)
    assert [c.transport for c in result.commands] == ["cloud"]
    assert result.commands[0].verb == "kelvin"
    assert len(cloud.sent) == 1
    assert result.status == "sent"  # cloud writes are not verified
    assert store.recent_events()[0]["status"] == "sent"


def test_scene_is_always_cloud_even_on_lan():
    lan, cloud = FakeTransport([LAN_DEV]), FakeCloud([CLOUD_DEV])
    service = make(lan, cloud)
    preview = service.set_scene("sunset")
    assert [c.transport for c in preview.commands] == ["cloud"]
    assert preview.commands[0].payload["value"] == {"paramId": 1, "id": 7}
    assert cloud.sent == []
    result = service.set_scene("Sunset", apply=True)
    assert [c for _, c in cloud.sent] == preview.commands
    assert lan.sent == []
    assert result.cli == ["govee light scene Sunset --apply"]


def test_unknown_scene_is_user_error():
    with pytest.raises(CliError) as exc:
        make(FakeTransport([LAN_DEV]), FakeCloud([CLOUD_DEV])).set_scene("Nope")
    assert exc.value.code == 1
    assert "Sunset" in exc.value.remediation


def test_scenes_listing():
    scenes = make(FakeTransport([LAN_DEV]), FakeCloud([CLOUD_DEV])).scenes()
    assert [(s.id, s.name) for s in scenes] == [("7", "Sunset")]


def test_devices_merge_lan_and_cloud_by_id():
    devices = make(FakeTransport([LAN_DEV]), FakeCloud([CLOUD_DEV, OTHER_CLOUD])).devices()
    by_id = {d.id: d for d in devices}
    assert by_id["AA:BB"].name == "Desk Lamp"
    assert by_id["AA:BB"].ip == "192.0.2.10"
    assert by_id["AA:BB"].transports == ("lan", "cloud")
    assert by_id["CC:DD"].transports == ("cloud",)


# --- logging (c17 / h11, d7) -----------------------------------------------------------------


def test_dry_run_is_logged_with_source_and_actor():
    result = make(source="mcp", actor="agent-7").apply(Change(on=True))
    events = store.recent_events()
    assert len(events) == 1
    assert events[0]["status"] == "dry_run"
    assert events[0]["source"] == "mcp" and events[0]["actor"] == "agent-7"
    assert result.event_ids == [events[0]["id"]]


def test_web_dry_run_is_not_logged_but_web_apply_is():
    lan = FakeTransport([LAN_DEV], state=LampState(on=True, brightness=1))
    service = make(lan, source="web", actor="phone")
    service.apply(Change(on=True))
    assert store.recent_events() == []
    service.apply(Change(on=True), apply=True)
    events = store.recent_events()
    assert [(e["source"], e["actor"]) for e in events] == [("web", "phone")]


def test_per_call_source_and_actor_override():
    lan = FakeTransport([LAN_DEV], state=LampState(on=True, brightness=1))
    service = make(lan, source="web", actor="serve")
    service.apply(Change(on=True), apply=True, source="rules", actor="sunset-rule")
    event = store.recent_events()[0]
    assert (event["source"], event["actor"]) == ("rules", "sunset-rule")
    assert event["payload"]["commands"][0]["verb"] == "turn"


def test_invalid_source_rejected():
    with pytest.raises(ValueError):
        make(source="email")


# --- verification (c30 / h21) ----------------------------------------------------------------


def test_lamp_that_applies_is_confirmed():
    lan = FakeTransport([LAN_DEV], state=LampState(on=True, brightness=40, color=(250, 110, 40)))
    result = make(lan).apply(Change(on=True, brightness=40, color=(255, 106, 43)), apply=True)
    assert result.status == "confirmed"
    assert store.recent_events()[0]["status"] == "confirmed"


def test_lamp_that_drops_the_packet_is_unconfirmed():
    lan = FakeTransport([LAN_DEV], state=OLD, drop=True)
    result = make(lan).apply(Change(on=True), apply=True)
    assert result.status == "unconfirmed"
    assert store.recent_events()[0]["status"] == "unconfirmed"


def test_verification_gives_up_within_three_seconds():
    clock = FakeClock()
    lan = FakeTransport([LAN_DEV], state=OLD)  # send "succeeds" but state never changes
    result = make(lan, clock=clock).apply(Change(on=True), apply=True)
    assert result.status == "unconfirmed"
    assert 0 < clock.t <= 3.0 + 1e-9


def test_verification_polls_until_state_matches():
    lan = SequenceLan([LAN_DEV], [OLD, None, LampState(on=True, brightness=60, kelvin=2710)])
    result = make(lan).apply(Change(kelvin=2700, brightness=60), apply=True)
    assert result.status == "confirmed"
    assert lan.polls == 3


def test_event_is_logged_sent_before_verification():
    seen = []

    class Spy(FakeTransport):
        def get_state(self, device):
            seen.append(store.recent_events()[0]["status"])
            return LampState(on=True, brightness=5)

    make(Spy([LAN_DEV])).apply(Change(on=True), apply=True)
    assert seen and seen[0] == "sent"


# --- device selection ------------------------------------------------------------------------


def test_device_selection_by_name_and_id():
    cloud = FakeCloud([CLOUD_DEV, OTHER_CLOUD])
    service = make(FakeTransport([]), cloud)
    service.apply(Change(on=True), apply=True, device="hall lamp")
    service.apply(Change(on=True), apply=True, device="AA:BB")
    assert [d.id for d, _ in cloud.sent] == ["CC:DD", "AA:BB"]


def test_several_devices_without_selector_is_user_error():
    service = make(FakeTransport([]), FakeCloud([CLOUD_DEV, OTHER_CLOUD]))
    with pytest.raises(CliError) as exc:
        service.apply(Change(on=True))
    assert exc.value.code == 1
    assert "Desk Lamp" in exc.value.message and "Hall Lamp" in exc.value.message


def test_unknown_device_is_user_error():
    with pytest.raises(CliError) as exc:
        make().apply(Change(on=True), device="garage")
    assert exc.value.code == 1


def test_device_selector_appears_in_cli_hint():
    result = make().apply(Change(brightness=30), device="H6008")
    assert result.cli == ["govee light brightness 30 --device H6008 --apply"]


# --- state -----------------------------------------------------------------------------------


def test_state_via_lan():
    lan = FakeTransport([LAN_DEV], state=LampState(on=True, brightness=70, kelvin=3000))
    status = make(lan).state()
    data = status.to_dict()
    assert data["device"]["transport"] == "lan"
    assert data["reachable"] is True
    assert data["mode"] == "white" and data["kelvin"] == 3000 and data["color"] is None


def test_state_falls_back_to_cloud_when_lan_silent():
    lan = FakeTransport([LAN_DEV], state=None)
    cloud = FakeCloud([CLOUD_DEV], state=LampState(on=True, brightness=5, color=(255, 0, 16)))
    data = make(lan, cloud).state().to_dict()
    assert data["device"]["transport"] == "cloud"
    assert data["color"] == "#FF0010" and data["mode"] == "color"


def test_state_unreachable():
    data = make(FakeTransport([LAN_DEV], state=None)).state().to_dict()
    assert data["reachable"] is False


# --- environment errors ----------------------------------------------------------------------


def test_missing_key_is_fine_while_lan_works():
    result = make(FakeTransport([LAN_DEV]), RaisingCloud(MissingKey())).apply(Change(on=True))
    assert result.commands == [build_turn(True)]


def test_no_device_found_is_env_error():
    with pytest.raises(CliError) as exc:
        make(FakeTransport([]), RaisingCloud(MissingKey())).apply(Change(on=True))
    assert exc.value.code == 2
    for hint in ("LAN Control", "4002", "GOVEE_LAN_INTERFACE", "GOVEE_API_KEY"):
        assert hint in exc.value.remediation


def test_failed_discovery_is_reused_briefly_then_retried():
    # An offline lamp must not make every console poll re-run a multi-second scan.
    class CountingLan(FakeTransport):
        def __init__(self, devices) -> None:
            super().__init__(devices)
            self.scans = 0

        def discover(self):
            self.scans += 1
            return super().discover()

    from govee_cli.govee.service_direct import MISS_TTL

    lan = CountingLan([])
    clock = FakeClock()
    service = make(lan, RaisingCloud(MissingKey()), clock=clock)
    assert service.devices() == [] and service.devices() == []
    assert lan.scans == 1
    assert service.devices(refresh=True) == [] and lan.scans == 2
    lan.devices = [LAN_DEV]
    clock.t += MISS_TTL + 1
    assert [d.id for d in service.devices()] == [LAN_DEV.id] and lan.scans == 3


def test_scene_without_key_is_env_error():
    with pytest.raises(CliError) as exc:
        make(FakeTransport([LAN_DEV]), RaisingCloud(MissingKey())).set_scene("Sunset")
    assert exc.value.code == 2
    assert "GOVEE_API_KEY" in exc.value.remediation


@pytest.mark.parametrize(
    "exc, needle",
    [
        (RateLimited(datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)), "12:00"),
        (Unauthorized("Govee API rejected the key (HTTP 401)"), "rejected"),
        (CloudUnavailable("Govee API unreachable (URLError)"), "unreachable"),
    ],
)
def test_cloud_errors_map_to_exit_2(monkeypatch, exc, needle):
    monkeypatch.setenv("GOVEE_API_KEY", "super-secret-key-value")
    with pytest.raises(CliError) as err:
        make(FakeTransport([]), RaisingCloud(exc)).apply(Change(on=True))
    assert err.value.code == 2
    assert needle in err.value.message
    assert err.value.remediation
    assert "super-secret-key-value" not in err.value.message + err.value.remediation


def test_map_error_passes_cli_error_through():
    original = CliError(1, "x", "y")
    assert svc.map_error(original) is original


# --- PortBusy without serve (c29 / h20, second half) -----------------------------------------


@pytest.fixture
def foreign_socket():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    yield sock.getsockname()[1]
    sock.close()


def test_port_busy_without_serve_exits_2(foreign_socket):
    lan = LanTransport("127.0.0.1", listen_host="127.0.0.1", listen_port=foreign_socket)
    service = LampService.connect("cli", "alice", lan=lan, cloud=RaisingCloud(MissingKey()))
    assert service.routed is False
    with pytest.raises(CliError) as exc:
        service.apply(Change(on=True), apply=True)
    assert exc.value.code == 2
    assert "already in use" in exc.value.message
    assert exc.value.remediation


def test_port_busy_falls_back_to_cloud_when_available(foreign_socket):
    lan = LanTransport("127.0.0.1", listen_host="127.0.0.1", listen_port=foreign_socket)
    cloud = FakeCloud([CLOUD_DEV])
    result = make(lan, cloud).apply(Change(on=True), apply=True)
    assert [c.transport for c in result.commands] == ["cloud"]


# --- routing through serve (c29 / h20 first half, d3) ----------------------------------------


class FakeServe:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.responses: dict[tuple[str, str], tuple[int, dict]] = {
            ("GET", "/api/health"): (200, {"ok": True, "service": "govee-serve"}),
            ("GET", "/api/devices"): (
                200,
                {
                    "devices": [
                        {
                            "id": "AA:BB",
                            "name": "Desk Lamp",
                            "model": "H6008",
                            "ip": "192.0.2.10",
                            "transports": ["lan", "cloud"],
                        }
                    ]
                },
            ),
            ("GET", "/api/state"): (
                200,
                {
                    "device": {
                        "id": "AA:BB",
                        "name": "Desk Lamp",
                        "model": "H6008",
                        "transport": "lan",
                    },
                    "reachable": True,
                    "on": True,
                    "brightness": 40,
                    "mode": "color",
                    "color": "#FF6A2B",
                    "kelvin": None,
                    "scene": None,
                },
            ),
            ("POST", "/api/light"): (
                200,
                {
                    "applied": True,
                    "commands": [
                        {
                            "verb": "colorwc",
                            "transport": "lan",
                            "payload": build_color((255, 106, 43)).payload,
                        }
                    ],
                    "cli": ["govee light color FF6A2B --apply"],
                    "status": "confirmed",
                },
            ),
            ("POST", "/api/scene"): (
                200,
                {
                    "applied": False,
                    "commands": [{"verb": "scene", "transport": "cloud", "payload": {}}],
                    "cli": ["govee light scene Sunset --apply"],
                    "status": "dry_run",
                },
            ),
            ("GET", "/api/scenes"): (200, {"scenes": [{"id": "7", "name": "Sunset"}]}),
        }
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _handle(self, method):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                path, _, query = self.path.partition("?")
                outer.requests.append(
                    {
                        "method": method,
                        "path": path,
                        "query": query,
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                        "body": json.loads(body) if body else None,
                    }
                )
                status, payload = outer.responses.get((method, path), (404, {}))
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):  # noqa: N802
                self._handle("GET")

            def do_POST(self):  # noqa: N802
                self._handle("POST")

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )

    def api(self):
        return [r for r in self.requests if r["path"] != "/api/health"]


@pytest.fixture
def serve(monkeypatch):
    fake = FakeServe()
    fake.thread.start()
    monkeypatch.setenv("GOVEE_SERVE_URL", fake.url)
    yield fake
    fake.server.shutdown()
    fake.server.server_close()


def routed(**kwargs):
    return LampService.connect(
        kwargs.pop("source", "cli"),
        kwargs.pop("actor", "alice"),
        lan=Untouchable(),
        cloud=Untouchable(),
    )


def test_routes_apply_through_serve_with_headers(serve, monkeypatch):
    monkeypatch.setenv("GOVEE_TOKEN", "client-secret")
    service = routed()
    assert service.routed is True
    result = service.apply(Change(color=(255, 106, 43)), apply=True)
    assert result.applied is True and result.status == "confirmed"
    assert result.commands == [build_color((255, 106, 43))]
    assert result.cli == ["govee light color FF6A2B --apply"]
    (req,) = serve.api()
    assert req["method"] == "POST" and req["path"] == "/api/light"
    assert req["body"] == {"color": "#FF6A2B", "apply": True}
    h = req["headers"]
    assert h["authorization"] == "Bearer client-secret"
    assert h["origin"] == serve.url
    assert h["x-govee-source"] == "cli" and h["x-govee-actor"] == "alice"
    assert store.recent_events() == []  # serve logs, not the client


def test_routed_reads_and_scene(serve, monkeypatch):
    monkeypatch.setenv("GOVEE_TOKEN", "client-secret")
    service = routed(source="mcp", actor="agent")
    devices = service.devices()
    assert devices == [
        Device("AA:BB", "Desk Lamp", "H6008", "192.0.2.10", ("lan", "cloud")),
    ]
    status = service.state(device="Desk Lamp")
    assert status.state.color == (255, 106, 43) and status.transport == "lan"
    assert [s.name for s in service.scenes()] == ["Sunset"]
    result = service.set_scene("Sunset", device="AA:BB")
    assert result.status == "dry_run" and result.applied is False
    paths = [(r["method"], r["path"]) for r in serve.api()]
    assert paths == [
        ("GET", "/api/devices"),
        ("GET", "/api/state"),
        ("GET", "/api/scenes"),
        ("POST", "/api/scene"),
    ]
    assert "device=Desk+Lamp" in serve.api()[1]["query"] or "Desk%20Lamp" in serve.api()[1]["query"]
    assert serve.api()[3]["body"] == {"scene": "Sunset", "apply": False, "device": "AA:BB"}
    assert all(r["headers"]["x-govee-source"] == "mcp" for r in serve.api())


def test_routed_with_client_token_file(serve, tmp_path):
    path = svc.client_token_path()
    assert path == tmp_path / "config" / "govee-cli" / "client-token"
    path.parent.mkdir(parents=True)
    path.write_text("file-secret\n")
    os.chmod(path, 0o600)
    routed().apply(Change(on=True))
    assert serve.api()[0]["headers"]["authorization"] == "Bearer file-secret"


def test_token_file_wider_than_0600_is_refused(serve):
    path = svc.client_token_path()
    path.parent.mkdir(parents=True)
    path.write_text("file-secret\n")
    os.chmod(path, 0o644)
    with pytest.raises(CliError) as exc:
        routed()
    assert exc.value.code == 2
    assert "chmod 600" in exc.value.remediation
    assert "file-secret" not in exc.value.message + exc.value.remediation


def test_serve_running_without_token_is_env_error(serve):
    with pytest.raises(CliError) as exc:
        routed()
    assert exc.value.code == 2
    assert "no token" in exc.value.message
    assert "govee token new --name <name> --save" in exc.value.remediation


def test_serve_error_body_maps_to_cli_error(serve, monkeypatch):
    monkeypatch.setenv("GOVEE_TOKEN", "bad-secret")
    serve.responses[("POST", "/api/light")] = (
        401,
        {"error": {"code": "unauthorized", "message": "invalid token", "remediation": "re-pair"}},
    )
    with pytest.raises(CliError) as exc:
        routed().apply(Change(on=True), apply=True)
    assert exc.value.code == 2
    assert "invalid token" in exc.value.message
    assert "bad-secret" not in exc.value.message + exc.value.remediation


def test_serve_bad_request_is_user_error(serve, monkeypatch):
    monkeypatch.setenv("GOVEE_TOKEN", "t")
    serve.responses[("POST", "/api/light")] = (
        400,
        {"error": {"code": "bad_request", "message": "several devices", "remediation": "pick"}},
    )
    with pytest.raises(CliError) as exc:
        routed().apply(Change(on=True))
    assert exc.value.code == 1


def test_foreign_http_service_is_not_mistaken_for_serve(serve):
    serve.responses[("GET", "/api/health")] = (200, {"ok": True, "service": "something-else"})
    service = LampService.connect("cli", "alice", lan=FakeTransport([LAN_DEV]), cloud=FakeCloud())
    assert service.routed is False


def test_non_loopback_http_serve_url_is_refused(monkeypatch):
    monkeypatch.setenv("GOVEE_SERVE_URL", "http://192.0.2.1:8742")
    with pytest.raises(CliError) as exc:
        LampService.connect("cli", "alice", lan=FakeTransport(), cloud=FakeCloud())
    assert exc.value.code == 2


def test_default_serve_url(monkeypatch):
    monkeypatch.delenv("GOVEE_SERVE_URL")
    assert svc.serve_base_url() == "http://127.0.0.1:8742"


def test_apply_result_to_dict_matches_api_shape():
    result = make().apply(Change(on=True))
    data = result.to_dict()
    assert set(data) >= {"applied", "commands", "cli", "status"}
    assert data["commands"][0] == {
        "verb": "turn",
        "transport": "lan",
        "payload": build_turn(True).payload,
    }
    assert isinstance(Command(**data["commands"][0]), Command)


# --- fades through serve (approved deviation d11) ---------------------------------------------


def test_routed_fade_sends_fade_seconds_and_parses_fade_fields(serve, monkeypatch):
    monkeypatch.setenv("GOVEE_TOKEN", "client-secret")
    fade = {"seconds": 30.0, "steps": 42, "interval_ms": 150, "id": "f1"}
    serve.responses[("POST", "/api/light")] = (
        200,
        {
            "applied": True,
            "commands": [{"verb": "brightness", "transport": "lan", "payload": {}}],
            "cli": ["govee light brightness 10 --fade 30 --apply"],
            "status": "sent",
            "fade": fade,
            "warnings": ["something"],
            "cancelled_fade": "f0",
        },
    )
    result = routed().apply(Change(brightness=10), apply=True, fade_seconds=30)
    (req,) = serve.api()
    assert req["body"] == {"brightness": 10, "apply": True, "fade_seconds": 30.0}
    assert result.fade is not None and result.fade.id == "f1" and result.fade.seconds == 30
    assert result.warnings == ["something"] and result.cancelled_fade == "f0"
    assert result.to_dict()["fade"] == fade


def test_routed_fade_omitted_when_not_given(serve, monkeypatch):
    monkeypatch.setenv("GOVEE_TOKEN", "client-secret")
    result = routed().apply(Change(on=True), apply=True)
    (req,) = serve.api()
    assert "fade_seconds" not in req["body"]
    assert result.fade is None and result.warnings == [] and result.cancelled_fade is None


def test_routed_active_and_cancel_fade(serve, monkeypatch):
    monkeypatch.setenv("GOVEE_TOKEN", "client-secret")
    active = {
        "id": "f1",
        "device": {"id": "AA:BB"},
        "started": "x",
        "seconds": 5,
        "target": {"on": True},
        "progress": 0.5,
    }
    serve.responses[("GET", "/api/fade")] = (200, {"active": active})
    serve.responses[("POST", "/api/fade/cancel")] = (200, {"cancelled": True})
    service = routed()
    assert service.active_fade() == active
    assert service.cancel_fade(device="Desk Lamp") is True
    get, post = serve.api()
    assert get["method"] == "GET" and get["path"] == "/api/fade"
    assert post["path"] == "/api/fade/cancel" and post["body"] == {"device": "Desk Lamp"}


def test_fade_seconds_validated_before_routing(serve, monkeypatch):
    monkeypatch.setenv("GOVEE_TOKEN", "client-secret")
    with pytest.raises(ValueError):
        routed().apply(Change(on=True), apply=True, fade_seconds=-1)
    assert serve.api() == []


def test_serve_backend_forwards_refresh(monkeypatch):
    from govee_cli.govee.service_serve import ServeBackend

    seen = []
    backend = ServeBackend("http://127.0.0.1:9", "t" * 32)
    monkeypatch.setattr(
        backend, "_request", lambda *a, **kw: seen.append(kw.get("query")) or {"devices": []}
    )
    backend.devices()
    backend.devices(refresh=True)
    assert seen == [None, {"refresh": "1"}]
