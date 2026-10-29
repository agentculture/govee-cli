"""Fades (approved deviation d11): planning, the runner, and the LampService hook."""

from __future__ import annotations

import json
import os
import stat
import threading
import time

import pytest

from govee_cli.cli._errors import CliError
from govee_cli.govee import fade as fade_mod
from govee_cli.govee.cloud import MissingKey
from govee_cli.govee.fade import (
    CLOUD_FADE_WARNING,
    FadePlan,
    FadeRunner,
    plan_fade,
    remember_brightness,
    remembered_brightness,
    resolve_fade_seconds,
    validate_fade_seconds,
)
from govee_cli.govee.fake import FakeTransport
from govee_cli.govee.lan import build_brightness, build_color, build_kelvin, build_turn
from govee_cli.govee.service import Change, LampService
from govee_cli.govee.types import Device, LampState, kelvin_to_rgb
from govee_cli.stats import store

LAN_DEV = Device(id="AA:BB", name="H6076", model="H6076", ip="192.0.2.10", transports=("lan",))
CLOUD_DEV = Device(id="AA:BB", name="Floor Lamp", model="H6076", transports=("cloud",))
OFF = LampState(on=False, brightness=60, color=(255, 0, 0))
ON_RED = LampState(on=True, brightness=60, color=(255, 0, 0))
ON_WARM = LampState(on=True, brightness=60, kelvin=2700)


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("GOVEE_TOKEN", raising=False)
    monkeypatch.delenv("GOVEE_API_KEY", raising=False)
    monkeypatch.delenv("GOVEE_FADE_DEFAULT", raising=False)
    monkeypatch.setenv("GOVEE_SERVE_URL", "http://127.0.0.1:9")


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


class StateLan(FakeTransport):
    """LAN fake whose state follows the commands it receives (a well-behaved lamp)."""

    def __init__(self, state, devices=(LAN_DEV,)) -> None:
        super().__init__(list(devices), state=state)

    def send(self, device, command):
        super().send(device, command)
        data = command.payload["msg"]["data"]
        s = self.state or LampState(on=False, brightness=1)
        if command.verb == "turn":
            s = LampState(bool(data["value"]), s.brightness, s.color, s.kelvin)
        elif command.verb == "brightness":
            s = LampState(s.on, data["value"], s.color, s.kelvin)
        elif data.get("colorTemInKelvin"):
            s = LampState(s.on, s.brightness, None, data["colorTemInKelvin"])
        else:
            c = data["color"]
            s = LampState(s.on, s.brightness, (c["r"], c["g"], c["b"]), None)
        self.state = s
        return not self.drop


class NoCloud(FakeTransport):
    def discover(self):
        raise MissingKey()


def make(lan, *, clock=None, background=False, source="cli", cloud=None, **kw):
    clock = clock or FakeClock()
    return LampService.direct(
        source=source,
        actor="alice",
        lan=lan,
        cloud=cloud if cloud is not None else NoCloud(),
        clock=clock,
        sleep=clock.sleep,
        background_fades=background,
        **kw,
    )


def brightness_values(plan: FadePlan) -> list[int]:
    return [
        c.payload["msg"]["data"]["value"]
        for _, cmds in plan.steps
        for c in cmds
        if c.verb == "brightness"
    ]


# --- seconds: validation and defaults ---------------------------------------------------------


def test_validate_fade_seconds():
    assert validate_fade_seconds(None) is None
    assert validate_fade_seconds(0) == 0.0
    assert validate_fade_seconds(3600) == 3600.0
    for bad in (-1, 3600.5, float("nan"), float("inf"), True, "5"):
        with pytest.raises(ValueError):
            validate_fade_seconds(bad)


def test_on_off_fade_by_default_and_other_changes_are_instant():
    assert resolve_fade_seconds(Change(on=True), None) == 5.0
    assert resolve_fade_seconds(Change(on=False), None) == 5.0
    assert resolve_fade_seconds(Change(brightness=40), None) == 0.0
    assert resolve_fade_seconds(Change(color=(1, 2, 3)), None) == 0.0
    assert resolve_fade_seconds(Change(on=True), 0) == 0.0
    assert resolve_fade_seconds(Change(brightness=40), 5) == 5.0


def test_default_comes_from_env(monkeypatch):
    monkeypatch.setenv("GOVEE_FADE_DEFAULT", "4")
    assert resolve_fade_seconds(Change(on=False), None) == 4.0
    monkeypatch.setenv("GOVEE_FADE_DEFAULT", "lots")
    with pytest.raises(CliError) as exc:
        resolve_fade_seconds(Change(on=False), None)
    assert exc.value.code == 1 and "GOVEE_FADE_DEFAULT" in exc.value.message
    monkeypatch.setenv("GOVEE_FADE_DEFAULT", "4000")
    with pytest.raises(CliError):
        resolve_fade_seconds(Change(on=False), None)


def test_service_rejects_fade_over_an_hour():
    with pytest.raises(ValueError):
        make(StateLan(ON_RED)).apply(Change(brightness=10), fade_seconds=3601)


# --- planning ---------------------------------------------------------------------------------


def test_fade_in_from_off_sets_brightness_1_turns_on_then_ramps():
    plan = plan_fade(OFF, Change(on=True, brightness=80), 1.5)
    assert plan.kind == "fade_on"
    at0, first = plan.steps[0]
    assert at0 == 0 and first[:2] == [build_brightness(1), build_turn(True)]
    values = brightness_values(plan)
    assert values[0] == 1 and values[-1] == 80
    assert values == sorted(values) and len(set(values)) == len(values)
    assert plan.steps[-1][0] == pytest.approx(1.5)
    offsets = [at for at, _ in plan.steps]
    assert offsets == sorted(offsets)
    assert plan.verify == Change(on=True, brightness=80)


def test_fade_in_target_is_the_lamps_own_level_then_remembered_then_100():
    # The lamp's own brightness (60) beats a stale remembered level from an older fade-out.
    assert brightness_values(plan_fade(OFF, Change(on=True), 2, remembered=42))[-1] == 60
    after_fade_out = LampState(on=False, brightness=1)
    assert brightness_values(plan_fade(after_fade_out, Change(on=True), 2, remembered=42))[-1] == 42
    assert brightness_values(plan_fade(after_fade_out, Change(on=True), 2))[-1] == 100


def test_fade_in_with_unknown_start_turns_on_without_dipping_to_1():
    # A lost devStatus reply must not drop an already-lit lamp to brightness 1.
    plan = plan_fade(None, Change(on=True), 2, remembered=42)
    assert build_brightness(1) not in plan.commands()
    assert plan.commands() == [build_turn(True)]


def test_fade_out_ramps_down_then_turns_off_and_never_sends_brightness_while_off():
    plan = plan_fade(ON_RED, Change(on=False), 1.5)
    assert plan.kind == "fade_off"
    commands = plan.commands()
    assert commands[-1] == build_turn(False)
    assert build_turn(False) not in commands[:-1]
    values = brightness_values(plan)
    assert values[0] < 60 and values[-1] == 1
    assert values == sorted(values, reverse=True)
    assert plan.remember_brightness == 60
    assert plan.verify == Change(on=False)


def test_fade_out_of_an_off_or_unknown_lamp_just_turns_off():
    for start in (OFF, None):
        plan = plan_fade(start, Change(on=False), 5)
        assert plan.commands() == [build_turn(False)]
        assert plan.remember_brightness is None


def test_long_ramp_only_emits_steps_when_the_value_changes():
    plan = plan_fade(LampState(on=False, brightness=1), Change(on=True, brightness=100), 1200)
    assert 95 <= len(plan.steps) <= 101  # ~one step per brightness level, not 8000
    assert plan.seconds == 1200 and plan.interval_ms >= 150
    assert plan.to_dict()["steps"] == len(plan.steps)


def test_min_interval_between_steps():
    plan = plan_fade(ON_RED, Change(brightness=1), 1.5)
    offsets = [at for at, _ in plan.steps]
    gaps = [b - a for a, b in zip(offsets, offsets[1:])]
    assert all(g >= 0.15 - 1e-9 for g in gaps)
    assert plan.interval_ms == 150


def test_rgb_cross_fade_is_linear_with_exact_endpoints():
    plan = plan_fade(ON_RED, Change(color=(0, 0, 255)), 3)
    colors = [c for c in plan.commands() if c.verb == "colorwc"]
    assert colors[-1] == build_color((0, 0, 255))
    reds = [c.payload["msg"]["data"]["color"]["r"] for c in colors]
    blues = [c.payload["msg"]["data"]["color"]["b"] for c in colors]
    assert reds == sorted(reds, reverse=True) and blues == sorted(blues)
    assert 0 < reds[0] < 255
    mid = colors[len(colors) // 2].payload["msg"]["data"]["color"]
    assert abs(mid["r"] - mid["b"]) < 60  # half way is roughly purple


def test_kelvin_cross_fade_is_linear():
    plan = plan_fade(ON_WARM, Change(kelvin=6500), 3)
    ks = [c.payload["msg"]["data"]["colorTemInKelvin"] for c in plan.commands()]
    assert ks == sorted(ks) and ks[-1] == 6500 and ks[0] > 2700
    assert plan.commands()[-1] == build_kelvin(6500)


def test_color_to_kelvin_cross_fades_in_rgb_and_finishes_with_kelvin():
    plan = plan_fade(ON_RED, Change(kelvin=6500), 3)
    commands = plan.commands()
    assert commands[-1] == build_kelvin(6500)
    rgb_steps = [c for c in commands[:-1] if c.verb == "colorwc"]
    assert rgb_steps and all(c.payload["msg"]["data"]["colorTemInKelvin"] == 0 for c in rgb_steps)
    last_rgb = rgb_steps[-1].payload["msg"]["data"]["color"]
    assert (last_rgb["r"], last_rgb["g"], last_rgb["b"]) == kelvin_to_rgb(6500)


def test_kelvin_to_color_starts_from_the_kelvin_rgb():
    plan = plan_fade(ON_WARM, Change(color=(0, 0, 255)), 3)
    first = plan.commands()[0].payload["msg"]["data"]["color"]
    warm = kelvin_to_rgb(2700)
    assert abs(first["r"] - warm[0]) < 30
    assert plan.commands()[-1] == build_color((0, 0, 255))


def test_brightness_and_color_fade_together():
    plan = plan_fade(ON_RED, Change(brightness=10, color=(0, 255, 0)), 2)
    commands = plan.commands()
    brightness = [c for c in commands if c.verb == "brightness"]
    colors = [c for c in commands if c.verb == "colorwc"]
    assert brightness[-1] == build_brightness(10) and colors[-1] == build_color((0, 255, 0))
    # sine easing lands each value by the end, never after it
    assert plan.steps[-1][0] <= 2 + 1e-9


def test_zero_seconds_snaps_to_the_instant_commands():
    plan = plan_fade(ON_RED, Change(brightness=10), 0)
    assert plan.steps == [(0.0, [build_brightness(10)])]


def test_nothing_to_ramp_sends_the_instant_commands_once():
    plan = plan_fade(ON_RED, Change(brightness=60), 5)
    assert plan.commands() == [build_brightness(60)]


# --- remembered brightness ---------------------------------------------------------------------


def test_remembered_brightness_file_is_0600_and_atomic(tmp_path):
    assert remembered_brightness("AA:BB") is None
    remember_brightness("AA:BB", 73)
    remember_brightness("CC:DD", 12)
    assert remembered_brightness("AA:BB") == 73 and remembered_brightness("cc:dd") == 12
    path = fade_mod.fade_state_path()
    assert path == tmp_path / "state" / "govee-cli" / "fade.json"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert json.loads(path.read_text())
    assert not [p for p in path.parent.iterdir() if p.name.startswith(".fade")]


def test_corrupt_remembered_file_is_ignored():
    path = fade_mod.fade_state_path()
    path.parent.mkdir(parents=True)
    path.write_text("{nope")
    assert remembered_brightness("AA:BB") is None


# --- runner -----------------------------------------------------------------------------------


def test_runner_sends_every_step_on_schedule():
    clock = FakeClock()
    plan = plan_fade(ON_RED, Change(brightness=1), 1.5)
    sent = []
    outcome = FadeRunner(
        plan, lambda c: sent.append((clock.t, c)) or True, clock=clock, sleep=clock.sleep
    ).run()
    assert outcome.ok and not outcome.cancelled and outcome.steps_done == len(plan.steps)
    assert [c for _, c in sent] == plan.commands()
    assert sent[-1][0] == pytest.approx(1.5)


def test_brightness_ramps_linearly_and_colour_temperature_eases_in_and_out():
    # Chosen with the user at the real lamp (d32): linear brightness looked best (a gamma curve
    # looked jumpy); colour and temperature ease in and out on a sine.
    up = brightness_values(plan_fade(LampState(on=True, brightness=1), Change(brightness=80), 10))
    deltas = [b - a for a, b in zip(up, up[1:])]
    assert up[-1] == 80 and max(deltas) - min(deltas) <= 1, "even percent steps"
    warm = plan_fade(LampState(on=True, brightness=50, kelvin=6500), Change(kelvin=2700), 10)
    kelvins = [
        c.payload["msg"]["data"]["colorTemInKelvin"]
        for _, cmds in warm.steps
        for c in cmds
        if c.verb == "colorwc"
    ]
    k_deltas = [a - b for a, b in zip(kelvins, kelvins[1:])]
    assert kelvins[-1] == 2700
    assert max(k_deltas[:3]) < max(k_deltas) and max(k_deltas[-3:]) < max(k_deltas)


def test_default_on_off_fade_is_5_seconds():
    assert fade_mod.DEFAULT_FADE_SECONDS == 5.0


def test_runner_cancel_stops_sending():
    clock = FakeClock()
    plan = plan_fade(ON_RED, Change(brightness=1), 3)
    cancel = threading.Event()
    sent = []

    def send(cmd):
        sent.append(cmd)
        if len(sent) == 3:
            cancel.set()
        return True

    outcome = FadeRunner(plan, send, clock=clock, sleep=clock.sleep, cancel=cancel).run()
    assert outcome.cancelled and len(sent) == 3 and outcome.steps_done == 3


# --- LampService hook: foreground (CLI without serve) -----------------------------------------


def test_light_on_fades_by_default_and_logs_one_confirmed_event():
    lan = StateLan(OFF)
    clock = FakeClock()
    result = make(lan, clock=clock).apply(Change(on=True), apply=True)
    assert result.status == "confirmed"
    assert result.fade is not None and result.fade.seconds == 5.0
    assert result.commands == result.fade.commands()
    assert [c for _, c in lan.sent] == result.commands
    assert lan.sent[0][1] == build_brightness(1) and lan.sent[1][1] == build_turn(True)
    assert clock.t >= 5.0
    (event,) = store.recent_events()
    assert event["verb"] == "fade_on" and event["status"] == "confirmed"
    assert event["payload"]["fade"]["seconds"] == 5.0
    assert event["payload"]["target"]["brightness"] == 60  # the off lamp's own brightness


def test_fade_zero_snaps_like_before():
    lan = StateLan(OFF)
    result = make(lan).apply(Change(on=True), apply=True, fade_seconds=0)
    assert result.fade is None and result.commands == [build_turn(True)]
    assert store.recent_events()[0]["verb"] == "light"


def test_brightness_is_instant_unless_fade_seconds_given():
    lan = StateLan(ON_RED)
    assert make(lan).apply(Change(brightness=10), apply=True).fade is None
    result = make(StateLan(ON_RED)).apply(Change(brightness=10), apply=True, fade_seconds=2)
    assert result.fade is not None and result.status == "confirmed"
    assert store.recent_events()[0]["verb"] == "fade"


def test_fade_off_remembers_pre_fade_brightness_and_fade_on_restores_it():
    lan = StateLan(LampState(on=True, brightness=70, color=(255, 0, 0)))
    service = make(lan)
    off = service.apply(Change(on=False), apply=True)
    assert off.status == "confirmed" and lan.state.on is False and lan.state.brightness == 1
    assert remembered_brightness(LAN_DEV.id) == 70
    on = service.apply(Change(on=True), apply=True)
    assert on.status == "confirmed" and lan.state.brightness == 70
    assert [e["verb"] for e in store.recent_events()] == ["fade_on", "fade_off"]


def test_dry_run_returns_the_plan_without_sending():
    lan = StateLan(OFF)
    result = make(lan).apply(Change(on=True), fade_seconds=5)
    assert result.applied is False and result.status == "dry_run"
    assert result.fade.seconds == 5 and len(result.fade.steps) > 1
    assert lan.sent == []
    data = result.to_dict()
    assert data["fade"] == {
        "seconds": 5.0,
        "steps": len(result.fade.steps),
        "interval_ms": result.fade.interval_ms,
        "id": result.fade.id,
    }
    assert data["warnings"] == []
    (event,) = store.recent_events()
    assert event["status"] == "dry_run" and event["verb"] == "fade_on"


def test_web_dry_run_fade_is_not_logged():
    make(StateLan(OFF), source="web").apply(Change(on=True))
    assert store.recent_events() == []


def test_foreground_keyboard_interrupt_stops_and_marks_cancelled():
    lan = StateLan(ON_RED)
    clock = FakeClock()
    calls = []

    def sleep(seconds):
        calls.append(seconds)
        if len(calls) == 3:
            raise KeyboardInterrupt
        clock.sleep(seconds)

    service = LampService.direct(lan=lan, cloud=NoCloud(), clock=clock, sleep=sleep)
    with pytest.raises(KeyboardInterrupt):
        service.apply(Change(brightness=1), apply=True, fade_seconds=10)
    sent = len(lan.sent)
    assert 0 < sent < 10
    (event,) = store.recent_events()
    assert event["status"] == "sent" and event["payload"]["cancelled"] is True
    assert service.active_fade() is None


def test_fade_needs_lan_and_degrades_to_instant_over_cloud():
    cloud = FakeTransport([CLOUD_DEV], state=ON_RED)
    service = LampService.direct(lan=FakeTransport([]), cloud=cloud, clock=FakeClock())
    result = service.apply(Change(brightness=10), apply=True, fade_seconds=30)
    assert result.fade is None
    assert result.warnings == [CLOUD_FADE_WARNING]
    assert [c.transport for c in result.commands] == ["cloud"] and len(cloud.sent) == 1
    assert result.to_dict()["warnings"] == [CLOUD_FADE_WARNING]


def test_default_on_off_over_cloud_is_instant_without_warning():
    cloud = FakeTransport([CLOUD_DEV], state=ON_RED)
    service = LampService.direct(lan=FakeTransport([]), cloud=cloud, clock=FakeClock())
    result = service.apply(Change(on=False), apply=True)
    assert result.fade is None and result.warnings == []


def test_dropped_packets_make_the_fade_unconfirmed():
    lan = StateLan(ON_RED)
    lan.drop = True
    result = make(lan).apply(Change(brightness=5), apply=True, fade_seconds=1)
    assert result.status == "unconfirmed"
    assert store.recent_events()[0]["status"] == "unconfirmed"


def test_cli_hints_carry_the_fade():
    result = make(StateLan(ON_RED)).apply(Change(brightness=10), fade_seconds=30)
    assert result.cli == ["govee light brightness 10 --fade 30 --apply"]
    assert make(StateLan(OFF)).apply(Change(on=True)).cli == ["govee light on --apply"]
    snap = make(StateLan(OFF)).apply(Change(on=True), fade_seconds=0)
    assert snap.cli == ["govee light on --fade 0 --apply"]


# --- LampService hook: background (govee serve) -----------------------------------------------


class GateSleep:
    """A sleep that blocks until released, so a background fade stays 'running'."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.release = threading.Event()
        self.entered = threading.Event()

    def __call__(self, seconds: float) -> None:
        self.entered.set()
        assert self.release.wait(5)
        self.clock.sleep(seconds)


def test_background_fade_returns_immediately_then_confirms():
    lan = StateLan(OFF)
    clock = FakeClock()
    gate = GateSleep(clock)
    service = LampService.direct(
        lan=lan, cloud=NoCloud(), clock=clock, sleep=gate, background_fades=True
    )
    result = service.apply(Change(on=True), apply=True, fade_seconds=3)
    assert result.status == "sent" and result.fade is not None
    assert gate.entered.wait(5)
    active = service.active_fade()
    assert active["id"] == result.fade.id and active["seconds"] == 3
    assert active["device"]["id"] == LAN_DEV.id and 0 <= active["progress"] <= 1
    assert active["target"] == {"on": True, "brightness": 60}
    gate.release.set()
    _wait_for(lambda: service.active_fade() is None)
    assert store.recent_events()[0]["status"] == "confirmed"
    assert len(store.recent_events()) == 1


def test_new_apply_cancels_the_running_fade():
    lan = StateLan(ON_RED)
    service = LampService.direct(
        lan=lan, cloud=NoCloud(), background_fades=True, verify_timeout=0.2, poll_interval=0.01
    )  # real clock: cancel interrupts the wait between steps
    first = service.apply(Change(brightness=1), apply=True, fade_seconds=6)
    _wait_for(lambda: len(lan.sent) >= 1)
    second = service.apply(Change(color=(0, 0, 255)), apply=True)
    assert second.cancelled_fade == first.fade.id
    assert second.to_dict()["cancelled_fade"] == first.fade.id
    assert service.active_fade() is None
    before = len(lan.sent)
    time.sleep(0.3)
    assert len(lan.sent) == before  # the cancelled fade sends nothing more
    events = store.recent_events()
    fade_event = next(e for e in events if e["verb"] == "fade")
    assert fade_event["status"] == "sent" and fade_event["payload"]["cancelled"] is True


def test_concurrent_fades_leave_exactly_one_running():
    # Serve is threaded: two writes racing must never leave an unregistered fade running.
    lan = StateLan(ON_RED)
    service = LampService.direct(lan=lan, cloud=NoCloud(), background_fades=True)
    barrier = threading.Barrier(8)
    results = []

    def write(level: int) -> None:
        barrier.wait(5)
        results.append(service.apply(Change(brightness=level), apply=True, fade_seconds=60))

    threads = [threading.Thread(target=write, args=(10 + i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert len(results) == 8
    running = service.active_fade()
    assert running is not None
    _wait_for(
        lambda: sum(
            1
            for e in store.recent_events()
            if e["verb"] == "fade" and not e["payload"].get("cancelled")
        )
        == 1
    )
    survivor = next(
        e
        for e in store.recent_events()
        if e["verb"] == "fade" and not e["payload"].get("cancelled")
    )
    assert survivor["payload"]["fade"]["id"] == running["id"]
    assert service.cancel_fade() is True


def test_cancel_fade_and_dry_run_does_not_cancel():
    lan = StateLan(ON_RED)
    service = LampService.direct(lan=lan, cloud=NoCloud(), background_fades=True)
    first = service.apply(Change(brightness=1), apply=True, fade_seconds=60)
    _wait_for(lambda: service.active_fade() is not None)
    dry = service.apply(Change(brightness=50))
    assert dry.cancelled_fade is None and service.active_fade()["id"] == first.fade.id
    assert service.cancel_fade() is True
    assert service.active_fade() is None
    assert service.cancel_fade() is False


def test_scene_cancels_the_running_fade():
    from govee_cli.govee.cloud import Scene

    class Cloud(FakeTransport):
        def scenes(self, device):
            return [Scene("Sunset", {"id": 7})]

    lan = StateLan(ON_RED)
    service = LampService.direct(lan=lan, cloud=Cloud([]), background_fades=True)
    first = service.apply(Change(brightness=1), apply=True, fade_seconds=60)
    _wait_for(lambda: service.active_fade() is not None)
    result = service.set_scene("Sunset", apply=True)
    assert result.cancelled_fade == first.fade.id and result.fade is None


def _wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition not reached"
        time.sleep(0.005)


def test_lan_error_mid_fade_is_env_error_and_unconfirmed():
    from govee_cli.govee.lan import LanError

    class Flaky(StateLan):
        def send(self, device, command):
            if len(self.sent) == 2:
                raise LanError("network unreachable")
            return super().send(device, command)

    lan = Flaky(ON_RED)
    service = make(lan)
    with pytest.raises(CliError) as exc:
        service.apply(Change(brightness=5), apply=True, fade_seconds=2)
    assert exc.value.code == 2 and len(lan.sent) == 2
    (event,) = store.recent_events()
    assert event["status"] == "unconfirmed" and "unreachable" in event["payload"]["error"]
    assert service.active_fade() is None


def test_step_plan_lists_offsets_and_commands():
    plan = plan_fade(OFF, Change(on=True, brightness=3), 1)
    listing = plan.step_plan()
    assert listing[0]["at"] == 0 and listing[0]["commands"][1]["verb"] == "turn"
    assert [s["at"] for s in listing] == sorted(s["at"] for s in listing)
    assert len(listing) == plan.to_dict()["steps"]


# --- live finding (t14): a snap-on after a fade-out must not leave the lamp at 1 % -------------


def test_snap_on_after_fade_out_restores_the_remembered_brightness():
    lan = StateLan(ON_RED)
    svc = make(lan)
    svc.apply(Change(on=False), apply=True)  # default fade-out: ramps to 1, turns off, remembers 60
    assert lan.state.on is False and lan.state.brightness == 1
    lan.sent.clear()
    result = svc.apply(Change(on=True), apply=True, fade_seconds=0)
    assert [c.verb for c in result.commands] == ["turn", "brightness"]
    assert lan.state.on is True and lan.state.brightness == 60


def test_snap_on_keeps_the_lamps_own_brightness_when_it_was_not_faded_out():
    lan = StateLan(LampState(on=False, brightness=45, color=(255, 0, 0)))
    remember_brightness(LAN_DEV.id, 90)  # stale memory from an older fade-out
    result = make(lan).apply(Change(on=True), apply=True, fade_seconds=0)
    assert [c.verb for c in result.commands] == ["turn"]
    assert lan.state.brightness == 45


def test_snap_on_with_an_explicit_brightness_ignores_the_memory():
    lan = StateLan(LampState(on=False, brightness=1, color=(255, 0, 0)))
    remember_brightness(LAN_DEV.id, 90)
    result = make(lan).apply(Change(on=True, brightness=20), apply=True, fade_seconds=0)
    assert lan.state.brightness == 20
    assert [c.verb for c in result.commands] == ["turn", "brightness"]
