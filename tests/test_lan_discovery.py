"""Tests for LAN discovery fallbacks (hosts, cache, subnet sweep). Loopback fakes only: no real
multicast and no real sweep is ever sent."""

from __future__ import annotations

import json
import os
import stat

import pytest

from govee_cli.govee import lan
from govee_cli.govee.lan import LanError, LanTransport
from tests.test_lan_transport import LOOPBACK, FakeLanDevice

DEV_ID = "AA:BB:CC:DD:EE:FF:00:11"
SILENT = "127.0.0.2"  # nothing listens on this scan port: simulates multicast silence


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("GOVEE_LAN_HOSTS", raising=False)


def _t(dev, **kw) -> LanTransport:
    params = dict(
        interface=LOOPBACK,
        group=SILENT,
        scan_port=dev.port,
        listen_host=LOOPBACK,
        listen_port=0,
        scan_timeout=0.3,
        sweep=False,
    )
    params.update(kw)
    return LanTransport(**params)


def _cache_file(tmp_path):
    return tmp_path / "state" / "govee-cli" / "lan-devices.json"


def test_hosts_argument_finds_lamp():
    with FakeLanDevice() as dev:
        t = _t(dev, hosts=[LOOPBACK], cache_path=None)
        devices = t.discover()
        report = t.scan_report()
    assert [d.id for d in devices] == [DEV_ID]
    assert report["devices"][0]["method"] == "hosts"
    assert report["methods_tried"][0] == "hosts"


def test_hosts_from_env(monkeypatch):
    monkeypatch.setenv("GOVEE_LAN_HOSTS", f" {LOOPBACK} , ")
    with FakeLanDevice() as dev:
        assert [d.id for d in _t(dev, cache_path=None).discover()] == [DEV_ID]


@pytest.mark.parametrize("bad", ["not-an-ip", "10.0.0.256", "192.0.2.1,nope"])
def test_invalid_hosts_raise_lan_error(bad, monkeypatch):
    monkeypatch.setenv("GOVEE_LAN_HOSTS", bad)
    with FakeLanDevice() as dev:
        with pytest.raises(LanError) as exc:
            _t(dev).discover()
    assert "GOVEE_LAN_HOSTS" in str(exc.value)


def test_cached_ip_finds_lamp_and_cache_is_0600(tmp_path):
    cache = _cache_file(tmp_path)
    cache.parent.mkdir(parents=True)
    cache.write_text(json.dumps({DEV_ID: {"ip": LOOPBACK, "sku": "H6008", "seen": "x"}}))
    with FakeLanDevice() as dev:
        t = _t(dev)
        assert [d.id for d in t.discover()] == [DEV_ID]
        report = t.scan_report()
    assert report["devices"][0]["method"] == "cache"
    assert stat.S_IMODE(os.stat(cache).st_mode) == 0o600
    data = json.loads(cache.read_text())
    assert data[DEV_ID]["ip"] == LOOPBACK and data[DEV_ID]["sku"] == "H6008"
    assert data[DEV_ID]["seen"].startswith("20")


def test_cache_updated_on_multicast_reply(tmp_path):
    with FakeLanDevice() as dev:
        t = _t(dev, group=LOOPBACK)
        assert t.scan_report()["devices"][0]["method"] == "multicast"
    assert json.loads(_cache_file(tmp_path).read_text())[DEV_ID]["ip"] == LOOPBACK


def test_sweep_runs_when_nothing_answers_and_caches(tmp_path):
    with FakeLanDevice() as dev:
        t = _t(dev, sweep=True, sweep_targets=lambda: [LOOPBACK])
        report = t.scan_report()
    assert [d["id"] for d in report["devices"]] == [DEV_ID]
    assert report["devices"][0]["method"] == "sweep"
    assert report["methods_tried"][-1] == "sweep"
    assert json.loads(_cache_file(tmp_path).read_text())[DEV_ID]["ip"] == LOOPBACK


def test_sweep_not_run_when_step_one_answered():
    calls = []

    def targets():
        calls.append(1)
        return [LOOPBACK]

    with FakeLanDevice() as dev:
        t = _t(dev, group=LOOPBACK, sweep=True, sweep_targets=targets)
        t.discover()
        assert "sweep" not in t.scan_report()["methods_tried"]
    assert calls == []


def test_sweep_disabled():
    with FakeLanDevice() as dev:
        t = _t(dev, sweep=False, sweep_targets=lambda: [LOOPBACK])
        assert t.discover() == []


def test_ip_taken_from_source_address_when_reply_has_no_ip():
    with FakeLanDevice(scan_ip=None) as dev:
        devices = _t(dev, hosts=[LOOPBACK], cache_path=None).discover()
    assert devices[0].ip == LOOPBACK


def test_corrupt_cache_ignored(tmp_path):
    cache = _cache_file(tmp_path)
    cache.parent.mkdir(parents=True)
    cache.write_text("{not json")
    with FakeLanDevice() as dev:
        assert [d.id for d in _t(dev, group=LOOPBACK).discover()] == [DEV_ID]
    assert json.loads(cache.read_text())[DEV_ID]["ip"] == LOOPBACK  # rewritten cleanly


def test_cache_path_none_writes_nothing(tmp_path):
    with FakeLanDevice() as dev:
        _t(dev, group=LOOPBACK, cache_path=None).discover()
    assert not (tmp_path / "state").exists()


def test_subnet_targets_slash24_and_cap():
    t = lan.subnet_targets("192.0.2.77")
    assert len(t) == 253 and "192.0.2.77" not in t and "192.0.2.1" in t and "192.0.2.254" in t
    wide = lan.subnet_targets("192.0.2.77", "255.255.0.0")
    assert wide == t
    narrow = lan.subnet_targets("192.0.2.77", "255.255.255.252")
    assert narrow == ["192.0.2.78"]
    assert lan.subnet_targets("127.0.0.1") == []
