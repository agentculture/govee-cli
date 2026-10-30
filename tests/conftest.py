"""Suite-wide isolation: no test may reach the real network, Govee account or home config."""

from __future__ import annotations

import ipaddress
import socket

import pytest

_GOVEE_ENV = (
    "GOVEE_API_KEY",
    "GOVEE_TOKEN",
    "GOVEE_SERVE_URL",
    "GOVEE_LAN_HOSTS",
    "GOVEE_LAN_INTERFACE",
    "GOVEE_FADE_DEFAULT",
    "GOVEE_TRUSTED_NETWORKS",
)


def _is_local(address) -> bool:
    if not isinstance(address, tuple) or not address:
        return True  # AF_UNIX paths and the like
    host = address[0]
    if host in ("", "localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    for name in _GOVEE_ENV:
        monkeypatch.delenv(name, raising=False)
    # A `govee serve` running on this machine must not be found: point the probe at a closed port.
    monkeypatch.setenv("GOVEE_SERVE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    real_sendto, real_connect = socket.socket.sendto, socket.socket.connect

    def sendto(self, data, *args):
        if self.family == socket.AF_INET and not _is_local(args[-1]):
            raise OSError(f"tests may not send to {args[-1]!r} (real network blocked)")
        return real_sendto(self, data, *args)

    def connect(self, address):
        if self.family == socket.AF_INET and not _is_local(address):
            raise OSError(f"tests may not connect to {address!r} (real network blocked)")
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "sendto", sendto)
    monkeypatch.setattr(socket.socket, "connect", connect)
