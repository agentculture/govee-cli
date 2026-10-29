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
    assert disc["passed"] and "cache" in disc["message"] and "multicast" in disc["message"]


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
    assert not c["passed"] and "GOVEE_LAN_INTERFACE" in c["remediation"]


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
