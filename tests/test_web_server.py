"""govee serve web server (t10): token-gated API, same-origin writes, static console."""

from __future__ import annotations

import http.client
import io
import json
import logging
import threading
import time
from datetime import timedelta

import pytest

from govee_cli.auth import tokens
from govee_cli.govee.cloud import MissingKey, RateLimited, Scene
from govee_cli.govee.fake import FakeTransport
from govee_cli.govee.service import LampService
from govee_cli.govee.types import Device, LampState
from govee_cli.stats import store
from govee_cli.web import server as web

LAN_DEV = Device(id="AA:BB", name="Floor Lamp", model="H6076", ip="192.0.2.10", transports=("lan",))
STATE = LampState(on=True, brightness=40, color=(255, 106, 43))


class FakeCloud(FakeTransport):
    def __init__(self, *args, scenes=None, exc=None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._scenes = scenes if scenes is not None else [Scene("Sunset", {"paramId": 1, "id": 7})]
        self.exc = exc

    def discover(self):
        if self.exc:
            raise self.exc
        return super().discover()

    def get_state(self, device):
        if self.exc:
            raise self.exc
        return super().get_state(device)

    def scenes(self, device):
        if self.exc:
            raise self.exc
        return list(self._scenes)


def make_service(lan=None, cloud=None):
    return LampService.direct(
        source="web",
        lan=lan if lan is not None else FakeTransport([LAN_DEV], state=STATE),
        cloud=cloud if cloud is not None else FakeCloud(),
        background_fades=True,
        verify_timeout=0.05,
        poll_interval=0.01,
    )


class Running:
    def __init__(self, service):
        self.service = service
        self.httpd = web.make_server("127.0.0.1", 0, service)
        self.port = self.httpd.server_address[1]
        self.origin = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, args=(0.02,), daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(5)

    def raw(self, method, path, *, headers=None, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            resp = conn.getresponse()
            data = resp.read()
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}, data
        finally:
            conn.close()

    def call(self, method, path, body=None, *, token=None, origin="same", headers=None):
        h = dict(headers or {})
        if token:
            h["Authorization"] = f"Bearer {token}"
        if origin == "same":
            origin = self.origin
        if origin is not None and method == "POST":
            h["Origin"] = origin
        raw = None
        if body is not None:
            raw = body if isinstance(body, (bytes, str)) else json.dumps(body)
            h["Content-Type"] = "application/json"
        status, hdrs, data = self.raw(method, path, headers=h, body=raw)
        try:
            parsed = json.loads(data) if data else None
        except ValueError:
            parsed = data
        return status, hdrs, parsed


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("GOVEE_FADE_DEFAULT", "0")


@pytest.fixture
def token():
    return tokens.new("phone")


@pytest.fixture
def srv():
    s = Running(make_service())
    yield s
    s.close()


def _error_code(body):
    return body["error"]["code"]


# --- auth: every /api route needs a token (c20, h13) -----------------------------------------

READS = ["/api/state", "/api/scenes", "/api/events", "/api/stats", "/api/devices", "/api/fade"]
WRITES = ["/api/light", "/api/scene", "/api/fade/cancel"]


@pytest.mark.parametrize("path", READS)
def test_reads_without_token_are_401(srv, path):
    status, hdrs, body = srv.call("GET", path)
    assert status == 401
    assert _error_code(body) == "unauthorized"
    assert set(body["error"]) == {"code", "message", "remediation"}
    assert hdrs["content-type"].startswith("application/json")


@pytest.mark.parametrize("path", WRITES)
def test_writes_without_token_are_401(srv, path):
    status, _, body = srv.call("POST", path, {"on": True, "apply": True})
    assert status == 401 and _error_code(body) == "unauthorized"


@pytest.mark.parametrize(
    "auth", ["Bearer nope", "Bearer ", "Basic Zm9vOmJhcg==", "bearer", "Token abc"]
)
def test_invalid_token_is_401(srv, token, auth):
    status, _, body = srv.call("GET", "/api/state", headers={"Authorization": auth})
    assert status == 401 and _error_code(body) == "unauthorized"


def test_rejected_write_sends_nothing(srv):
    lan = srv.service.backend.lan
    status, _, _ = srv.call("POST", "/api/light", {"on": False, "apply": True}, token="wrong")
    assert status == 401
    assert lan.sent == []


def test_unknown_api_route_needs_token_then_404(srv, token):
    assert srv.call("GET", "/api/nope")[0] == 401
    status, _, body = srv.call("GET", "/api/nope", token=token)
    assert status == 404 and _error_code(body) == "not_found"


def test_health_needs_no_token_and_is_exact(srv):
    status, _, body = srv.call("GET", "/api/health")
    assert status == 200
    assert body == {"ok": True, "service": "govee-serve"}


def test_token_in_query_string_is_not_accepted(srv, token):
    assert srv.call("GET", f"/api/state?token={token}")[0] == 401


def test_token_file_is_0600(token):
    assert (tokens.default_path().stat().st_mode & 0o777) == 0o600


# --- same-origin writes (c32) ----------------------------------------------------------------


@pytest.mark.parametrize("origin", [None, "http://evil.example", "null", "https://127.0.0.1"])
def test_write_with_wrong_or_missing_origin_is_403(srv, token, origin):
    lan = srv.service.backend.lan
    status, _, body = srv.call(
        "POST", "/api/light", {"on": False, "apply": True}, token=token, origin=origin
    )
    assert status == 403 and _error_code(body) == "forbidden"
    assert lan.sent == []


def test_origin_must_match_host_port(srv, token):
    status, _, _ = srv.call(
        "POST",
        "/api/light",
        {"on": False, "apply": False},
        token=token,
        origin=f"http://127.0.0.1:{srv.port + 1}",
    )
    assert status == 403


def test_reads_do_not_need_origin(srv, token):
    assert srv.call("GET", "/api/state", token=token)[0] == 200


# --- body limits ------------------------------------------------------------------------------


def test_oversize_body_is_413_without_reading_it(srv, token):
    conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
    try:
        conn.putrequest("POST", "/api/light")
        conn.putheader("Authorization", f"Bearer {token}")
        conn.putheader("Origin", srv.origin)
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", str(16 * 1024 + 1))
        conn.endheaders()  # no body sent: the server must answer without waiting for it
        resp = conn.getresponse()
        body = json.loads(resp.read())
    finally:
        conn.close()
    assert resp.status == 413 and _error_code(body) == "too_large"


def test_body_of_exactly_16k_is_accepted_size_wise(srv, token):
    payload = {"on": True, "apply": False, "device": "AA:BB"}
    raw = json.dumps(payload)
    raw = raw[:-1] + " " * (16 * 1024 - len(raw)) + "}"
    assert len(raw) == 16 * 1024
    assert srv.call("POST", "/api/light", raw, token=token)[0] == 200


@pytest.mark.parametrize("raw", ["not json", "[1, 2]", '"x"', "null"])
def test_body_must_be_a_json_object(srv, token, raw):
    status, _, body = srv.call("POST", "/api/light", raw, token=token)
    assert status == 400 and _error_code(body) == "bad_request"


def test_chunked_body_is_refused(srv, token):
    status, _, _ = srv.raw(
        "POST",
        "/api/light",
        headers={
            "Authorization": f"Bearer {token}",
            "Origin": srv.origin,
            "Transfer-Encoding": "chunked",
        },
        body=iter([b'{"on": true}']),
    )
    assert status in (400, 411)


# --- rotate (h23) -----------------------------------------------------------------------------


def test_rotate_revokes_all_tokens_and_prints_fresh_link(srv, token):
    other = tokens.new("laptop")
    out = io.StringIO()
    link = web.rotate_tokens(host="127.0.0.1", port=srv.port, out=out)
    assert out.getvalue().count(link) == 1
    assert link.startswith(f"http://127.0.0.1:{srv.port}/#token=")
    assert "?" not in link
    fresh = link.split("#token=", 1)[1]
    assert srv.call("GET", "/api/state", token=token)[0] == 401
    assert srv.call("GET", "/api/state", token=other)[0] == 401
    assert srv.call("GET", "/api/state", token=fresh)[0] == 200
    names = [t["name"] for t in tokens.list_tokens()]
    assert len(names) == 1 and names[0].startswith("console-")


def test_rotate_falls_back_to_loopback_when_no_route(monkeypatch):
    from govee_cli.govee import lan

    def boom():
        raise lan.LanError("no route")

    monkeypatch.setattr(lan, "default_route_ip", boom)
    out = io.StringIO()
    link = web.rotate_tokens(host="0.0.0.0", port=8742, out=out)  # nosec B104
    assert link.startswith("http://127.0.0.1:8742/#token=")


def test_run_with_rotate_prints_link_once_and_shuts_down(monkeypatch, capsys):
    started = []

    class FakePoller:
        def __init__(self, service):
            started.append("init")

        def start(self):
            started.append("start")

        def stop(self, timeout=5.0):
            started.append("stop")

    monkeypatch.setattr(web, "Poller", FakePoller)
    monkeypatch.setattr(web, "_build_service", lambda: make_service())
    monkeypatch.setattr(web, "_pairing_host", lambda host: "127.0.0.1")

    def fake_serve_forever(self, poll_interval=0.5):
        raise KeyboardInterrupt

    monkeypatch.setattr(web.GoveeServer, "serve_forever", fake_serve_forever)
    old = tokens.new("phone")
    assert web.run(host="127.0.0.1", port=0, rotate_token=True) == 0
    out = capsys.readouterr().out
    assert out.count("#token=") == 1
    assert tokens.verify(old) is None
    assert started == ["init", "start", "stop"]


def test_sigterm_takes_the_clean_shutdown_path_and_restores_the_handler(monkeypatch):
    # Found in the live test (t14): systemd's SIGTERM killed serve in the default handler,
    # skipping the poller stop and fade cancel. run() now routes SIGTERM like Ctrl-C.
    import os
    import signal

    stopped = []

    class FakePoller:
        def __init__(self, service):
            pass

        def start(self):
            pass

        def stop(self, timeout=5.0):
            stopped.append("poller")

    service = make_service()
    monkeypatch.setattr(web, "Poller", FakePoller)
    monkeypatch.setattr(web, "_build_service", lambda: service)
    monkeypatch.setattr(service, "cancel_fade", lambda *a, **k: stopped.append("fades") or False)

    def fake_serve_forever(self, poll_interval=0.5):
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(5)  # the handler must interrupt this
        raise AssertionError("SIGTERM did not interrupt serve_forever")

    monkeypatch.setattr(web.GoveeServer, "serve_forever", fake_serve_forever)
    before = signal.getsignal(signal.SIGTERM)
    assert web.run(host="127.0.0.1", port=0) == 0
    assert stopped == ["poller", "fades"]
    assert signal.getsignal(signal.SIGTERM) is before


# --- attribution headers honoured only from loopback (d4) -------------------------------------


def _last_event():
    return store.recent_events(limit=1)[0]


def test_loopback_client_may_set_source_and_actor(srv, token):
    status, _, body = srv.call(
        "POST",
        "/api/light",
        {"on": True, "apply": False},
        token=token,
        headers={"X-Govee-Source": "cli", "X-Govee-Actor": "bob"},
    )
    assert status == 200 and body["status"] == "dry_run"
    ev = _last_event()
    assert (ev["source"], ev["actor"], ev["status"]) == ("cli", "bob", "dry_run")


def test_non_loopback_client_attribution_headers_are_ignored(srv, token, monkeypatch):
    monkeypatch.setattr(web.GoveeHandler, "_client_is_loopback", lambda self: False)
    status, _, _ = srv.call(
        "POST",
        "/api/light",
        {"on": True, "apply": True},
        token=token,
        headers={"X-Govee-Source": "mcp", "X-Govee-Actor": "mallory"},
    )
    assert status == 200
    ev = _last_event()
    assert (ev["source"], ev["actor"]) == ("web", "phone")


def test_default_attribution_is_web_and_token_name(srv, token):
    srv.call("POST", "/api/light", {"brightness": 70, "apply": True}, token=token)
    ev = _last_event()
    assert (ev["source"], ev["actor"]) == ("web", "phone")


def test_invalid_source_header_is_400(srv, token):
    status, _, body = srv.call("GET", "/api/state", token=token, headers={"X-Govee-Source": "root"})
    assert status == 400 and _error_code(body) == "bad_request"


@pytest.mark.parametrize(
    ("addr", "expected"),
    [("127.0.0.1", True), ("127.5.6.7", True), ("::1", True), ("::ffff:127.0.0.1", True)]
    + [("192.168.1.20", False), ("100.64.0.3", False), ("::ffff:10.0.0.1", False), ("x", False)],
)
def test_loopback_detection(addr, expected):
    assert web.is_loopback(addr) is expected


# --- lamp routes ------------------------------------------------------------------------------


def test_state(srv, token):
    status, hdrs, body = srv.call("GET", "/api/state", token=token)
    assert status == 200
    assert body["device"] == {
        "id": "AA:BB",
        "name": "Floor Lamp",
        "model": "H6076",
        "transport": "lan",
    }
    assert body["reachable"] is True and body["on"] is True and body["brightness"] == 40
    assert body["color"] == "#FF6A2B" and body["mode"] == "color"
    assert hdrs["cache-control"] == "no-store"


def test_unreachable_lamp_is_200_with_reachable_false(token):
    s = Running(make_service(lan=FakeTransport([LAN_DEV], state=None), cloud=FakeCloud()))
    try:
        status, _, body = s.call("GET", "/api/state", token=token)
    finally:
        s.close()
    assert status == 200 and body["reachable"] is False


def test_devices(srv, token):
    status, _, body = srv.call("GET", "/api/devices", token=token)
    assert status == 200
    assert body == {
        "devices": [
            {
                "id": "AA:BB",
                "name": "Floor Lamp",
                "model": "H6076",
                "ip": "192.0.2.10",
                "transports": ["lan"],
            }
        ]
    }


def test_light_dry_run_sends_nothing_and_is_not_logged(srv, token):
    lan = srv.service.backend.lan
    status, _, body = srv.call(
        "POST", "/api/light", {"color": "#FF6A2B", "brightness": 50, "apply": False}, token=token
    )
    assert status == 200
    assert body["applied"] is False and body["status"] == "dry_run"
    assert [c["verb"] for c in body["commands"]] == ["brightness", "colorwc"]
    assert all(c["transport"] == "lan" for c in body["commands"])
    assert any("--apply" in c for c in body["cli"])
    assert body["warnings"] == [] and body["fade"] is None
    assert lan.sent == []
    assert store.recent_events() == []  # d7: web dry runs are not logged


def test_light_apply_sends_the_previewed_commands(srv, token):
    lan = srv.service.backend.lan
    req = {"color": "#00FF00", "apply": False}
    _, _, plan = srv.call("POST", "/api/light", req, token=token)
    status, _, body = srv.call("POST", "/api/light", {**req, "apply": True}, token=token)
    assert status == 200 and body["applied"] is True
    assert body["status"] in ("sent", "confirmed", "unconfirmed")
    assert [c.payload for _, c in lan.sent] == [c["payload"] for c in plan["commands"]]
    ev = _last_event()
    assert ev["source"] == "web" and ev["status"] != "dry_run"


def test_apply_defaults_to_dry_run(srv, token):
    _, _, body = srv.call("POST", "/api/light", {"on": False}, token=token)
    assert body["applied"] is False and srv.service.backend.lan.sent == []


@pytest.mark.parametrize(
    "body",
    [
        {"apply": False},
        {"on": "yes", "apply": False},
        {"brightness": 0, "apply": False},
        {"brightness": True, "apply": False},
        {"brightness": "50", "apply": False},
        {"color": "red", "apply": False},
        {"color": "#FF6A2B", "kelvin": 3000, "apply": False},
        {"kelvin": 100, "apply": False},
        {"on": True, "apply": "true"},
        {"on": True, "fade_seconds": -1, "apply": False},
        {"on": True, "fade_seconds": 99999, "apply": False},
        {"on": True, "fade_seconds": "2", "apply": False},
        {"on": True, "surprise": 1, "apply": False},
        {"on": True, "device": 5, "apply": False},
        {"on": True, "device": "nope", "apply": False},
    ],
)
def test_light_validation_is_400(srv, token, body):
    status, _, resp = srv.call("POST", "/api/light", body, token=token)
    assert status == 400, resp
    assert _error_code(resp) == "bad_request"
    assert srv.service.backend.lan.sent == []


def test_scenes_listing(srv, token):
    status, _, body = srv.call("GET", "/api/scenes", token=token)
    assert status == 200 and body == {"scenes": [{"id": "7", "name": "Sunset"}]}


@pytest.mark.parametrize("scene", ["7", "Sunset", "sunset"])
def test_scene_by_id_or_name(srv, token, scene):
    status, _, body = srv.call("POST", "/api/scene", {"scene": scene, "apply": False}, token=token)
    assert status == 200 and body["status"] == "dry_run"
    assert body["commands"][0]["transport"] == "cloud"
    assert "Sunset" in body["cli"][0]


def test_scene_apply_goes_over_cloud(srv, token):
    cloud = srv.service.backend.cloud
    status, _, body = srv.call("POST", "/api/scene", {"scene": "7", "apply": True}, token=token)
    assert status == 200 and body["applied"] is True
    assert len(cloud.sent) == 1


@pytest.mark.parametrize("body", [{"scene": "Nope", "apply": False}, {"apply": False}])
def test_bad_scene_is_400(srv, token, body):
    status, _, resp = srv.call("POST", "/api/scene", body, token=token)
    assert status == 400 and _error_code(resp) == "bad_request"


# --- fades (d11) ------------------------------------------------------------------------------


def test_fade_dry_run_returns_plan(srv, token):
    status, _, body = srv.call(
        "POST", "/api/light", {"brightness": 80, "fade_seconds": 2, "apply": False}, token=token
    )
    assert status == 200 and body["applied"] is False
    assert body["fade"]["seconds"] == 2.0 and body["fade"]["steps"] > 0
    assert srv.service.backend.lan.sent == []
    assert srv.call("GET", "/api/fade", token=token)[2] == {"active": None}


def test_fade_runs_in_background_and_can_be_cancelled(srv, token):
    status, _, body = srv.call(
        "POST", "/api/light", {"brightness": 90, "fade_seconds": 30, "apply": True}, token=token
    )
    assert status == 200 and body["applied"] is True and body["fade"]["id"]
    _, _, fade = srv.call("GET", "/api/fade", token=token)
    assert fade["active"]["id"] == body["fade"]["id"]
    assert 0 <= fade["active"]["progress"] <= 1
    status, _, cancelled = srv.call("POST", "/api/fade/cancel", {}, token=token)
    assert status == 200 and cancelled == {"cancelled": True}
    deadline = time.time() + 5
    while time.time() < deadline and srv.call("GET", "/api/fade", token=token)[2]["active"]:
        time.sleep(0.02)
    assert srv.call("GET", "/api/fade", token=token)[2] == {"active": None}
    assert srv.call("POST", "/api/fade/cancel", {}, token=token)[2] == {"cancelled": False}


def test_new_write_reports_cancelled_fade(srv, token):
    _, _, first = srv.call(
        "POST", "/api/light", {"brightness": 90, "fade_seconds": 30, "apply": True}, token=token
    )
    _, _, second = srv.call("POST", "/api/light", {"brightness": 10, "apply": True}, token=token)
    assert second["cancelled_fade"] == first["fade"]["id"]


def test_cancel_needs_origin(srv, token):
    assert srv.call("POST", "/api/fade/cancel", {}, token=token, origin=None)[0] == 403


# --- error mapping ----------------------------------------------------------------------------


def test_rate_limit_is_429(token):
    s = Running(make_service(lan=FakeTransport([]), cloud=FakeCloud(exc=RateLimited())))
    try:
        status, _, body = s.call("GET", "/api/state", token=token)
    finally:
        s.close()
    assert status == 429 and _error_code(body) == "rate_limited"


def test_no_device_and_no_key_is_unreachable(token):
    s = Running(make_service(lan=FakeTransport([]), cloud=FakeCloud(exc=MissingKey())))
    try:
        status, _, body = s.call("GET", "/api/state", token=token)
    finally:
        s.close()
    assert status in (502, 503) and _error_code(body) == "unreachable"
    assert body["error"]["remediation"]


def test_unexpected_error_is_500_without_details(srv, token, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("secret-internal-detail")

    monkeypatch.setattr(srv.service, "state", boom)
    status, _, body = srv.call("GET", "/api/state", token=token)
    assert status == 500 and _error_code(body) == "internal"
    assert "secret-internal-detail" not in json.dumps(body)


def test_method_not_allowed(srv, token):
    assert srv.call("POST", "/api/state", {}, token=token)[0] == 405
    assert srv.call("GET", "/api/light", token=token)[0] == 405


# --- events and stats -------------------------------------------------------------------------


def test_events_have_summary_and_limit(srv, token):
    for b in (10, 20, 30):
        srv.call("POST", "/api/light", {"brightness": b, "apply": True}, token=token)
    status, _, body = srv.call("GET", "/api/events?limit=2", token=token)
    assert status == 200 and len(body["events"]) == 2
    ev = body["events"][0]
    assert {"ts", "source", "actor", "verb", "summary", "status"} <= set(ev)
    assert "30" in ev["summary"]
    assert (ev["source"], ev["actor"]) == ("web", "phone")


@pytest.mark.parametrize("q", ["limit=abc", "limit=0", "limit=-3"])
def test_events_bad_limit_is_400(srv, token, q):
    assert srv.call("GET", f"/api/events?{q}", token=token)[0] == 400


def test_stats_shape(srv, token):
    store.record_sample(True, 60, (255, 0, 0), None)
    status, _, body = srv.call("GET", "/api/stats", token=token)
    assert status == 200
    assert set(body["tiles"]) == {
        "on_today_h",
        "on_week_h",
        "commands_7d",
        "agent_commands_7d",
        "avg_brightness",
    }
    assert len(body["hours_per_day"]) == 14
    assert len(body["commands_by_source"]) == 7
    assert len(body["heatmap"]) == 7 * 24
    samples = [s for s in body["day_samples"] if not s.get("gap")]
    assert samples and samples[-1]["rgb"] == "#FF0000"
    assert body["tiles"]["avg_brightness"] == 60


def test_stats_pass_the_server_local_timezone(srv, token, monkeypatch):
    seen = []
    real = store.hours_per_day

    def spy(*a, **k):
        seen.append(k.get("tz"))
        return real(*a, **k)

    monkeypatch.setattr(store, "hours_per_day", spy)
    srv.call("GET", "/api/stats", token=token)
    assert seen and seen[0] is not None
    assert seen[0].utcoffset(None) is None or isinstance(seen[0].utcoffset(None), timedelta)


def test_local_tz_matches_time_localtime():
    import datetime as dt

    tz = web.LocalTimezone()
    now = dt.datetime.now(tz)
    assert now.utcoffset() == timedelta(seconds=time.localtime().tm_gmtoff)


# --- static console ---------------------------------------------------------------------------


def test_index_served_with_security_headers(srv):
    status, hdrs, body = srv.raw("GET", "/")
    assert status == 200
    assert hdrs["content-type"].startswith("text/html")
    assert hdrs["cache-control"] == "no-store"
    assert hdrs["x-content-type-options"] == "nosniff"
    assert hdrs["referrer-policy"] == "no-referrer"
    csp = hdrs["content-security-policy"]
    assert "default-src 'self'" in csp and "script-src 'self'" in csp
    assert "https://fonts.googleapis.com" in csp and "https://fonts.gstatic.com" in csp
    assert "unsafe-eval" not in csp and "frame-ancestors 'none'" in csp
    assert b"app.js" in body
    assert "python" not in hdrs.get("server", "").lower()


@pytest.mark.parametrize(
    ("path", "ctype"),
    [("/index.html", "text/html"), ("/app.js", "text/javascript"), ("/app.css", "text/css")],
)
def test_static_assets(srv, path, ctype):
    status, hdrs, _ = srv.raw("GET", path)
    assert status == 200 and hdrs["content-type"].startswith(ctype)
    assert hdrs["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize(
    "path",
    [
        "/../server.py",
        "/%2e%2e/server.py",
        "/..%2fserver.py",
        "/static/../server.py",
        "/./../__init__.py",
        "//etc/passwd",
        "/%2Fetc%2Fpasswd",
        "/..\\server.py",
        "/nope.js",
        "/.hidden",
    ],
)
def test_static_refuses_traversal_and_unknown(srv, path):
    status, _, body = srv.raw("GET", path)
    assert status == 404
    assert b"import" not in body and b"root:" not in body


def test_static_query_string_is_ignored(srv):
    assert srv.raw("GET", "/app.js?v=3")[0] == 200


# --- access log never contains the token (h23) ------------------------------------------------


def test_access_log_never_contains_the_token(srv, token, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    srv.call("GET", "/api/state", token=token)
    srv.call("GET", f"/api/state?token={token}")
    srv.call("POST", "/api/light", {"on": True, "apply": False}, token=token)
    srv.call("GET", "/api/state", headers={"Authorization": f"Bearer {token}x"})
    srv.raw("GET", f"/{token}")
    srv.raw("BREW", f"/x?{token}")  # an unsupported method still must not log the query
    time.sleep(0.05)
    logged = caplog.text + capsys.readouterr().err
    assert "/api/state" in logged  # the request line is logged...
    assert token not in logged  # ...but never the token
    assert "Bearer" not in logged
