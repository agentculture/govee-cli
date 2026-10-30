"""t8: doctor LAN / key / stats checks."""

from __future__ import annotations

import json

import pytest

from govee_cli.cli import main
from govee_cli.cli._commands import doctor as doctor_mod
from govee_cli.govee.lan import LanError


def report(capsys, *argv):
    rc = main(["doctor", "--json", *argv])
    return rc, json.loads(capsys.readouterr().out)


def check(rep, cid):
    return next(c for c in rep["checks"] if c["id"] == cid)


@pytest.fixture
def scan(monkeypatch):
    out = {
        "interface": {"interface": None, "ip": "192.0.2.5", "source": "default-route"},
        "replied": True,
        "devices": [
            {"id": "AA", "name": "Floor", "model": "H6076", "ip": "192.0.2.10", "method": "cache"}
        ],
        "methods_tried": ["cache", "multicast"],
        "error": None,
    }
    monkeypatch.setattr(doctor_mod, "_lan_scan", lambda: out)
    return out


def test_all_checks_present(capsys, scan):
    rc, rep = report(capsys)
    ids = {c["id"] for c in rep["checks"]}
    assert {"lan-interface", "lan-discovery", "api-key-present", "stats-db-writable"} <= ids
    disc = check(rep, "lan-discovery")
    assert disc["passed"]
    assert "cache" in disc["message"]
    assert "multicast" in disc["message"]


def test_lan_discovery_failure_remediation(capsys, scan):
    scan.update(replied=False, devices=[], methods_tried=["multicast", "sweep"])
    rc, rep = report(capsys)
    disc = check(rep, "lan-discovery")
    assert not disc["passed"]
    for needle in ("LAN Control", "4002", "GOVEE_LAN_INTERFACE", "GOVEE_LAN_HOSTS"):
        assert needle in disc["remediation"]
    assert rc == 0  # advisory; does not make the agent unhealthy


def test_lan_interface_error(capsys, monkeypatch, scan):
    def boom(_=None):
        raise LanError("no default IPv4 route")

    monkeypatch.setattr(doctor_mod, "_interface_info", boom)
    _, rep = report(capsys)
    c = check(rep, "lan-interface")
    assert not c["passed"]
    assert "GOVEE_LAN_INTERFACE" in c["remediation"]


def test_api_key_never_printed(capsys, monkeypatch, scan):
    monkeypatch.setenv("GOVEE_API_KEY", "super-secret-key-123")
    main(["doctor", "--json"])
    out = capsys.readouterr()
    assert "super-secret-key-123" not in out.out + out.err
    rep = json.loads(out.out)
    assert check(rep, "api-key-present")["passed"] is True


def test_api_key_absent_is_advisory(capsys, scan):
    rc, rep = report(capsys)
    assert check(rep, "api-key-present")["passed"] is False
    assert check(rep, "api-key-present")["remediation"]
    assert rc == 0


def test_stats_db_writable(capsys, scan):
    _, rep = report(capsys)
    assert check(rep, "stats-db-writable")["passed"] is True


def test_no_scan_flag_skips_discovery(capsys, monkeypatch):
    def fail():
        raise AssertionError("scan must not run")

    monkeypatch.setattr(doctor_mod, "_lan_scan", fail)
    rc, rep = report(capsys, "--no-scan")
    assert check(rep, "lan-discovery")["severity"] == "info"


def test_env_no_scan(capsys, monkeypatch):
    monkeypatch.setenv("GOVEE_DOCTOR_NO_SCAN", "1")
    monkeypatch.setattr(doctor_mod, "_lan_scan", lambda: (_ for _ in ()).throw(AssertionError))
    rc, rep = report(capsys)
    assert check(rep, "lan-discovery")["severity"] == "info"


def test_discovery_asks_govee_serve_when_it_runs(capsys, monkeypatch, scan):
    # serve owns UDP 4002; a scan of our own would fail with PortBusy on a healthy setup.
    from govee_cli.govee import service as service_mod
    from govee_cli.govee import service_serve
    from govee_cli.govee.types import Device

    class Served:
        def devices(self):
            return [Device(id="AA", name="Floor", model="H6076", transports=("lan",))]

    monkeypatch.setattr(service_serve, "serve_running", lambda base: True)
    monkeypatch.setattr(service_mod.LampService, "connect", classmethod(lambda cls: Served()))
    monkeypatch.setattr(doctor_mod, "_lan_scan", lambda: (_ for _ in ()).throw(AssertionError))
    rc, rep = report(capsys)
    disc = check(rep, "lan-discovery")
    assert disc["passed"]
    assert "through govee serve" in disc["message"]
    assert "Floor" in disc["message"]


def test_discovery_through_serve_reports_a_missing_token(capsys, monkeypatch, scan):
    from govee_cli.cli._errors import CliError
    from govee_cli.govee import service as service_mod
    from govee_cli.govee import service_serve

    def no_token(cls):
        raise CliError(2, "govee serve is running but this client has no token", "govee token new")

    monkeypatch.setattr(service_serve, "serve_running", lambda base: True)
    monkeypatch.setattr(service_mod.LampService, "connect", classmethod(no_token))
    rc, rep = report(capsys)
    disc = check(rep, "lan-discovery")
    assert not disc["passed"]
    assert "no token" in disc["message"]
    assert disc["remediation"] == "govee token new"
