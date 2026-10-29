"""State poller (t11): fake clock, FakeTransport, tmp stats DB."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

import pytest

from govee_cli.cli._errors import CliError
from govee_cli.govee.cloud import RateLimited
from govee_cli.govee.fake import FakeTransport
from govee_cli.govee.service import LampService, map_error
from govee_cli.govee.types import Device, LampState
from govee_cli.stats import store
from govee_cli.web.poller import MAX_BACKOFF, Poller

LAN_DEV = Device(id="AA:BB", name="H6008", model="H6008", ip="192.0.2.10", transports=("lan",))
CLOUD_DEV = Device(id="AA:BB", name="Desk Lamp", model="H6008", transports=("cloud",))
T0 = datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc).timestamp()


@pytest.fixture(autouse=True)
def _state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


class Clock:
    def __init__(self, t: float = T0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def _rows():
    conn = store.connect()
    try:
        return [
            dict(r) for r in conn.execute('SELECT ts, "on", brightness, rgb, kelvin FROM samples')
        ]
    finally:
        conn.close()


def _lan_poller(state, clock=None, **kw):
    svc = LampService.direct(lan=FakeTransport([LAN_DEV], state=state))
    return Poller(svc, clock=clock or Clock(), **kw)


class Raising:
    """Stub service whose state() raises or returns scripted values in order."""

    def __init__(self, *outcomes) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    def state(self, device=None):
        self.calls += 1
        out = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(out, BaseException):
            raise out
        return out


def test_lan_sample_written_and_interval_60():
    p = _lan_poller(LampState(on=True, brightness=40, color=(255, 106, 43)))
    assert p.sample_once() == 60
    (row,) = _rows()
    assert row["ts"] == T0 and row["on"] == 1 and row["brightness"] == 40
    assert row["rgb"] == "[255, 106, 43]"  # the store's own encoding


def test_cloud_only_interval_300():
    cloud = FakeTransport([CLOUD_DEV], state=LampState(on=True, brightness=70, kelvin=3000))
    p = Poller(LampService.direct(cloud=cloud), clock=Clock())
    assert p.sample_once() == 300
    (row,) = _rows()
    assert row["kelvin"] == 3000 and row["rgb"] is None


def test_cloud_interval_never_below_300():
    p = Poller(LampService.direct(), cloud_interval=10, lan_interval=60)
    assert p.cloud_interval == 300


def test_off_but_reachable_is_a_real_off_sample():
    p = _lan_poller(LampState(on=False, brightness=1))
    p.sample_once()
    (row,) = _rows()
    assert row["on"] == 0


def test_unreachable_writes_nothing_and_leaves_a_gap():
    clock = Clock()
    p = _lan_poller(None, clock)
    assert p.sample_once() == 60
    assert _rows() == []
    now = datetime.fromtimestamp(clock.t, timezone.utc)
    assert store.day_samples(hours=1, now=now) == [
        {"ts": datetime.fromtimestamp(clock.t - 3600, timezone.utc).isoformat(), "gap": True}
    ]


def test_rate_limited_backs_off_until_reset():
    clock = Clock()
    reset = datetime.fromtimestamp(T0 + 600, timezone.utc)
    p = Poller(Raising(RateLimited(reset)), clock=clock)
    assert p.sample_once() == pytest.approx(600)
    assert _rows() == []


def test_rate_limited_surfaced_as_clierror_is_also_handled():
    reset = datetime.fromtimestamp(T0 + 900, timezone.utc)
    try:
        try:
            raise RateLimited(reset)
        except RateLimited as exc:
            raise map_error(exc) from None
    except CliError as mapped:
        err = mapped
    p = Poller(Raising(err), clock=Clock())
    assert p.sample_once() == pytest.approx(900)


def test_rate_limited_clierror_without_context_parses_remediation():
    reset = datetime.fromtimestamp(T0 + 450, timezone.utc)
    err = map_error(RateLimited(reset))  # no active exception context
    err.__context__ = None
    assert Poller(Raising(err), clock=Clock()).sample_once() == pytest.approx(450)


def test_rate_limited_without_reset_and_far_reset_are_bounded():
    assert Poller(Raising(RateLimited(None)), clock=Clock()).sample_once() == 60
    far = datetime.fromtimestamp(T0 + 10 * 3600, timezone.utc)
    assert Poller(Raising(RateLimited(far)), clock=Clock()).sample_once() == MAX_BACKOFF
    past = datetime.fromtimestamp(T0 - 5, timezone.utc)
    assert Poller(Raising(RateLimited(past)), clock=Clock()).sample_once() >= 1


def test_other_exceptions_do_not_kill_the_loop():
    ok = LampService.direct(lan=FakeTransport([LAN_DEV], LampState(on=True, brightness=5)))
    svc = Raising(RuntimeError("boom"), ok.state())
    p = Poller(svc, clock=Clock())
    assert p.sample_once() > 0
    assert _rows() == []
    p.sample_once()
    assert len(_rows()) == 1


def test_run_loop_uses_interval_and_survives_errors():
    clock = Clock()
    waits: list[float] = []

    def wait(seconds: float) -> bool:
        waits.append(seconds)
        clock.t += seconds
        return len(waits) >= 4  # stop after the 4th wait

    st = LampState(on=True, brightness=9)
    svc = Raising(RuntimeError("x"), CliError(2, "down", ""), _status(st, "lan"))
    p = Poller(svc, clock=clock, wait=wait)
    p.run()
    assert svc.calls == 4
    assert len(waits) == 4 and all(w > 0 for w in waits)
    assert len(_rows()) == 2  # calls 3 and 4 (outcome repeats)


def _status(state, transport):
    from govee_cli.govee.service_types import LampStatus

    return LampStatus(LAN_DEV, transport, state)


def test_start_stop_idempotent_and_prompt():
    p = _lan_poller(LampState(on=True, brightness=3), lan_interval=60)
    p.start()
    p.start()
    deadline = time.time() + 2
    while not _rows() and time.time() < deadline:
        time.sleep(0.01)
    assert len(_rows()) == 1
    threads = [t for t in threading.enumerate() if t.name == "govee-poller"]
    assert len(threads) == 1 and threads[0].daemon
    t0 = time.time()
    p.stop()
    assert time.time() - t0 < 1
    assert not p.running


def test_stopped_poller_leaves_no_rows():
    p = _lan_poller(LampState(on=True, brightness=3))
    p.start()
    p.stop()
    n = len(_rows())
    time.sleep(0.1)
    assert len(_rows()) == n
    p.stop()  # stop twice is fine


def test_never_started_poller_has_no_rows():
    _lan_poller(LampState(on=True, brightness=3))
    assert _rows() == []


def test_unreachable_lamp_with_cloud_route_waits_cloud_interval():
    # LAN failed and the cloud fallback failed too: the next try must respect the cloud quota.
    from govee_cli.govee.service_types import LampStatus

    both = Device(
        id="AA:BB", name="Lamp", model="H6076", ip="192.0.2.10", transports=("lan", "cloud")
    )

    class Stub:
        def state(self, device=None):
            return LampStatus(device=both, transport="lan", state=None)

    assert Poller(Stub(), clock=lambda: 0.0).sample_once() == 300
