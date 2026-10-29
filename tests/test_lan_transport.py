"""Tests for the Govee LAN transport. Never sends real multicast: everything runs on 127.0.0.1
against a fake device thread with ephemeral ports."""

from __future__ import annotations

import ast
import json
import socket
import threading
import time
from pathlib import Path

import pytest

from govee_cli.govee import lan
from govee_cli.govee.lan import LanError, LanTransport, PortBusy
from govee_cli.govee.types import Command, Device, LampState, Transport

LOOPBACK = "127.0.0.1"


class FakeLanDevice:
    """A UDP 'lamp' on 127.0.0.1: answers scan and devStatus to the sender, records the rest."""

    def __init__(self, status=None, answer_status=True, answer_scan=True, scan_ip="127.0.0.1"):
        self.scan_ip = scan_ip
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((LOOPBACK, 0))
        self.sock.settimeout(0.05)
        self.port = self.sock.getsockname()[1]
        self.status = status or {
            "onOff": 1,
            "brightness": 42,
            "color": {"r": 255, "g": 10, "b": 0},
            "colorTemInKelvin": 0,
        }
        self.answer_status = answer_status
        self.answer_scan = answer_scan
        self.received: list[dict] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(1)
        self.sock.close()

    def _run(self):
        while not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                return
            msg = json.loads(data)["msg"]
            self.received.append(msg)
            reply = None
            if msg["cmd"] == "scan" and self.answer_scan:
                body = {
                    "device": "AA:BB:CC:DD:EE:FF:00:11",
                    "sku": "H6008",
                    "bleVersionHard": "3.01.01",
                }
                if self.scan_ip:
                    body["ip"] = self.scan_ip
                reply = {"cmd": "scan", "data": body}
            elif msg["cmd"] == "devStatus" and self.answer_status:
                reply = {"cmd": "devStatus", "data": self.status}
            if reply is not None:
                self.sock.sendto(json.dumps({"msg": reply}).encode(), addr)

    def wait_for(self, count, timeout=1.0):
        deadline = time.monotonic() + timeout
        while len(self.received) < count and time.monotonic() < deadline:
            time.sleep(0.01)
        return len(self.received) >= count


def _transport(dev: FakeLanDevice, **kw) -> LanTransport:
    params = dict(
        interface=LOOPBACK,
        group=LOOPBACK,
        scan_port=dev.port,
        listen_host=LOOPBACK,
        listen_port=0,
        control_port=dev.port,
        status_port=dev.port,
        scan_timeout=0.3,
        status_timeout=0.3,
        sweep=False,
        cache_path=None,
    )
    params.update(kw)
    return LanTransport(**params)


def _device() -> Device:
    return Device(id="AA:BB:CC:DD:EE:FF:00:11", name="H6008", model="H6008", ip=LOOPBACK)


# --- defaults and constants --------------------------------------------------------------


def test_real_defaults():
    assert lan.SCAN_GROUP == "239.255.255.250"
    assert lan.SCAN_PORT == 4001
    assert lan.LISTEN_PORT == 4002
    assert lan.CONTROL_PORT == 4003
    assert lan.STATUS_PORT == 4003
    t = LanTransport(interface=LOOPBACK)
    assert (t.group, t.scan_port, t.listen_port, t.control_port, t.status_port) == (
        "239.255.255.250",
        4001,
        4002,
        4003,
        4003,
    )
    assert t.scan_timeout == pytest.approx(2.0)
    assert t.status_timeout == pytest.approx(1.0)


def test_implements_transport_protocol():
    assert isinstance(LanTransport(interface=LOOPBACK), Transport)


def test_only_stdlib_imports():
    tree = ast.parse(Path(lan.__file__).read_text())
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            mods.add(node.module.split(".")[0])
    allowed = {"__future__", "socket", "json", "select", "os", "struct", "fcntl", "time"}
    allowed |= {"typing", "dataclasses", "errno", "govee_cli"}
    allowed |= {"logging", "tempfile", "datetime", "pathlib"}
    assert mods <= allowed, mods - allowed


# --- command builders --------------------------------------------------------------------


def test_build_turn_commands():
    on = lan.build_turn(True)
    assert on == Command("turn", "lan", {"msg": {"cmd": "turn", "data": {"value": 1}}})
    assert lan.build_turn(False).payload["msg"]["data"]["value"] == 0


def test_build_brightness_clamps_and_validates():
    assert lan.build_brightness(55).payload == {"msg": {"cmd": "brightness", "data": {"value": 55}}}
    with pytest.raises(ValueError):
        lan.build_brightness(0)
    with pytest.raises(ValueError):
        lan.build_brightness(101)


def test_build_color_and_kelvin():
    c = lan.build_color((1, 2, 3))
    assert c.verb == "colorwc" and c.transport == "lan"
    assert c.payload == {
        "msg": {
            "cmd": "colorwc",
            "data": {"color": {"r": 1, "g": 2, "b": 3}, "colorTemInKelvin": 0},
        }
    }
    k = lan.build_kelvin(4000)
    assert k.payload["msg"]["data"]["colorTemInKelvin"] == 4000
    assert set(k.payload["msg"]["data"]["color"]) == {"r", "g", "b"}
    with pytest.raises(ValueError):
        lan.build_color((256, 0, 0))
    with pytest.raises(ValueError):
        lan.build_kelvin(100)


# --- interface selection -----------------------------------------------------------------


def test_interface_argument_ip_wins_over_env(monkeypatch):
    monkeypatch.setenv("GOVEE_LAN_INTERFACE", "10.9.9.9")
    info = lan.interface_info(LOOPBACK)
    assert info == {"interface": LOOPBACK, "ip": LOOPBACK, "source": "argument"}


def test_interface_from_env(monkeypatch):
    monkeypatch.setenv("GOVEE_LAN_INTERFACE", LOOPBACK)
    info = lan.interface_info()
    assert info["ip"] == LOOPBACK and info["source"] == "env"


def test_interface_name_resolves_loopback(monkeypatch):
    monkeypatch.delenv("GOVEE_LAN_INTERFACE", raising=False)
    assert lan.resolve_interface("lo") == LOOPBACK


def test_unknown_interface_name_raises_lan_error():
    with pytest.raises(LanError) as exc:
        lan.resolve_interface("nosuchif0")
    assert "nosuchif0" in str(exc.value)


def test_default_route_interface(monkeypatch):
    monkeypatch.delenv("GOVEE_LAN_INTERFACE", raising=False)
    monkeypatch.setattr(lan, "default_route_ip", lambda: "192.0.2.7")
    info = lan.interface_info()
    assert info == {"interface": None, "ip": "192.0.2.7", "source": "default-route"}


# --- discovery ---------------------------------------------------------------------------


def test_discover_finds_fake_device():
    with FakeLanDevice() as dev:
        devices = _transport(dev).discover()
    assert devices == [
        Device(
            id="AA:BB:CC:DD:EE:FF:00:11",
            name="H6008",
            model="H6008",
            ip=LOOPBACK,
            transports=("lan",),
        )
    ]
    assert dev.received[0] == {"cmd": "scan", "data": {"account_topic": "reserve"}}


def test_discover_no_reply_returns_empty():
    with FakeLanDevice(answer_scan=False) as dev:
        assert _transport(dev, scan_timeout=0.1).discover() == []


def test_scan_report_for_doctor():
    with FakeLanDevice() as dev:
        report = _transport(dev).scan_report()
    assert report["interface"] == {"interface": LOOPBACK, "ip": LOOPBACK, "source": "argument"}
    assert report["replied"] is True
    assert report["devices"][0]["id"] == "AA:BB:CC:DD:EE:FF:00:11"
    assert report["error"] is None


def test_scan_report_no_reply_and_port_busy():
    with FakeLanDevice(answer_scan=False) as dev:
        report = _transport(dev, scan_timeout=0.1).scan_report()
        assert report["replied"] is False and report["devices"] == []
        holder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        holder.bind((LOOPBACK, 0))
        try:
            busy = _transport(dev, listen_port=holder.getsockname()[1]).scan_report()
        finally:
            holder.close()
    assert busy["replied"] is False
    assert "in use" in busy["error"]


# --- port busy ---------------------------------------------------------------------------


def test_port_busy_raises_typed_error():
    holder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    holder.bind((LOOPBACK, 0))
    port = holder.getsockname()[1]
    try:
        with FakeLanDevice() as dev:
            t = _transport(dev, listen_port=port)
            with pytest.raises(PortBusy) as exc:
                t.discover()
            with pytest.raises(PortBusy):
                t.get_state(_device())
    finally:
        holder.close()
    assert isinstance(exc.value, LanError)
    assert exc.value.port == port
    assert f"port {port}" in str(exc.value) and "in use" in str(exc.value)


def test_listener_does_not_share_port_with_reuseaddr_holder():
    """Two govee-cli processes must not silently share the reply port."""
    holder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    holder.bind((LOOPBACK, 0))
    try:
        with FakeLanDevice() as dev:
            with pytest.raises(PortBusy):
                _transport(dev, listen_port=holder.getsockname()[1]).discover()
    finally:
        holder.close()


# --- control -----------------------------------------------------------------------------


def test_send_emits_control_json_to_device_port():
    with FakeLanDevice() as dev:
        t = _transport(dev)
        assert t.send(_device(), lan.build_turn(True)) is True
        assert t.send(_device(), lan.build_brightness(30)) is True
        assert t.send(_device(), lan.build_color((9, 8, 7))) is True
        assert dev.wait_for(3)
    assert [m["cmd"] for m in dev.received] == ["turn", "brightness", "colorwc"]
    assert dev.received[1] == {"cmd": "brightness", "data": {"value": 30}}


def test_send_works_while_listen_port_busy():
    holder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    holder.bind((LOOPBACK, 0))
    try:
        with FakeLanDevice() as dev:
            t = _transport(dev, listen_port=holder.getsockname()[1])
            assert t.send(_device(), lan.build_turn(False)) is True
            assert dev.wait_for(1)
    finally:
        holder.close()


def test_send_refuses_without_ip_or_non_lan_command():
    t = LanTransport(interface=LOOPBACK)
    no_ip = Device(id="x", name="x", model="H6008")
    assert t.send(no_ip, lan.build_turn(True)) is False
    assert t.send(_device(), Command("turn", "cloud", {})) is False


# --- status ------------------------------------------------------------------------------


def test_get_state_parses_devstatus_rgb():
    with FakeLanDevice() as dev:
        state = _transport(dev).get_state(_device())
    assert state == LampState(on=True, brightness=42, color=(255, 10, 0), kelvin=None)
    assert dev.received[0] == {"cmd": "devStatus", "data": {}}


def test_get_state_parses_kelvin_mode():
    status = {
        "onOff": 0,
        "brightness": 100,
        "color": {"r": 0, "g": 0, "b": 0},
        "colorTemInKelvin": 3000,
    }
    with FakeLanDevice(status=status) as dev:
        state = _transport(dev).get_state(_device())
    assert state == LampState(on=False, brightness=100, color=None, kelvin=3000)


def test_get_state_timeout_returns_none():
    with FakeLanDevice(answer_status=False) as dev:
        assert _transport(dev, status_timeout=0.1).get_state(_device()) is None


def test_get_state_without_ip_returns_none():
    t = LanTransport(interface=LOOPBACK)
    assert t.get_state(Device(id="x", name="x", model="H6008")) is None


def test_parse_ignores_garbage():
    assert lan.parse_message(b"not json") is None
    assert lan.parse_message(b'{"nope": 1}') is None
    assert lan.parse_message(b'{"msg": {"cmd": "scan", "data": {}}}') == ("scan", {})
