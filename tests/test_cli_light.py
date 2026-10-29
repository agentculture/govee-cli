"""t8: ``govee light ...`` / ``devices`` verbs over a fake-transport LampService."""

from __future__ import annotations

import json
import shlex

import pytest

from govee_cli.cli import _build_parser, main
from govee_cli.govee import service_types as svc
from govee_cli.govee.cloud import Scene
from govee_cli.govee.fake import FakeTransport
from govee_cli.govee.service import Change, LampService
from govee_cli.govee.types import Device, LampState

DEV = Device(id="AA:BB", name="Floor Lamp", model="H6076", ip="192.0.2.10", transports=("lan",))
STATE = LampState(on=True, brightness=40, color=(0, 0, 255))


class Cloud(FakeTransport):
    def scenes(self, device):
        return [Scene("Sunset", {"paramId": 1, "id": 7})]


@pytest.fixture
def lan():
    return FakeTransport(devices=[DEV], state=STATE)


@pytest.fixture(autouse=True)
def fake_service(monkeypatch, lan):
    calls = []

    def connect(source="cli", actor=None, **opts):
        calls.append((source, actor))
        return LampService.direct(
            source=source,
            actor=actor,
            lan=lan,
            cloud=Cloud(),
            sleep=lambda _s: None,
            verify_timeout=0.0,
            poll_interval=0.0,
        )

    monkeypatch.setattr(LampService, "connect", staticmethod(connect))
    monkeypatch.setenv("USER", "tester")
    return calls


def run(capsys, *argv):
    rc = main(list(argv))
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


# --- dry run vs apply -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["light", "on", "--fade", "0"],
        ["light", "off", "--fade", "0"],
        ["light", "brightness", "50"],
        ["light", "color", "FF6A2B"],
        ["light", "temp", "3000"],
        ["light", "scene", "Sunset"],
    ],
)
def test_dry_run_sends_nothing(capsys, lan, argv):
    rc, out, err = run(capsys, *argv)
    assert rc == 0
    assert lan.sent == []
    assert "would send" in out
    assert "--apply" in out


@pytest.mark.parametrize(
    "argv",
    [
        ["light", "on", "--fade", "0"],
        ["light", "brightness", "50"],
        ["light", "color", "#FF6A2B"],
        ["light", "temp", "3000"],
    ],
)
def test_apply_sends(capsys, lan, argv):
    rc, out, _ = run(capsys, *argv, "--apply")
    assert rc == 0
    assert lan.sent
    assert "applied" in out


def test_json_dry_run_is_apply_result(capsys, lan):
    rc, out, err = run(capsys, "light", "brightness", "50", "--json")
    assert rc == 0 and err == ""
    payload = json.loads(out)
    assert payload["applied"] is False
    assert payload["commands"][0]["verb"]
    assert payload["cli"]


def test_source_and_actor(capsys, fake_service):
    run(capsys, "light", "brightness", "50")
    assert fake_service[-1] == ("cli", "tester")


def test_bad_brightness_is_user_error(capsys):
    rc, out, err = run(capsys, "light", "brightness", "500")
    assert rc == 1
    assert err.startswith("error:") and "hint:" in err and out == ""


def test_bad_color_json_error(capsys):
    rc, out, err = run(capsys, "light", "color", "nothex", "--json")
    assert rc == 1
    assert json.loads(err)["code"] == 1 and out == ""


def test_bad_fade_is_user_error(capsys):
    rc, _, err = run(capsys, "light", "on", "--fade", "99999")
    assert rc == 1 and "hint:" in err


def test_unknown_scene_is_user_error(capsys):
    rc, _, err = run(capsys, "light", "scene", "Nope")
    assert rc == 1 and "hint:" in err


# --- fades ------------------------------------------------------------------------------------


def test_fade_dry_run_prints_plan(capsys, lan):
    rc, out, _ = run(capsys, "light", "off", "--fade", "2")
    assert rc == 0 and lan.sent == []
    assert "fade" in out and "steps" in out and "first" in out and "last" in out


def test_fade_json_has_fade_plan(capsys):
    rc, out, _ = run(capsys, "light", "off", "--fade", "2", "--json")
    payload = json.loads(out)
    assert payload["fade"]["seconds"] == 2.0
    assert payload["fade_preview"]["first"] and payload["fade_preview"]["last"]


def test_ctrl_c_exits_130_cleanly(capsys, monkeypatch):
    def boom(self, *a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(LampService, "apply", boom)
    rc, out, err = run(capsys, "light", "off", "--fade", "5", "--apply")
    assert rc == 130
    assert "Traceback" not in err and "stopped" in err


def test_fade_status_and_stop(capsys):
    rc, out, _ = run(capsys, "light", "fade-status")
    assert rc == 0 and "no fade" in out
    rc, out, _ = run(capsys, "light", "fade-status", "--json")
    assert json.loads(out) == {"active": None}
    rc, out, _ = run(capsys, "light", "fade-stop", "--json")
    assert json.loads(out) == {"cancelled": False}
    rc, out, _ = run(capsys, "light", "fade-stop")
    assert rc == 0 and "no fade" in out


# --- reads ------------------------------------------------------------------------------------


def test_devices_text_and_json(capsys):
    rc, out, _ = run(capsys, "devices")
    assert rc == 0 and "Floor Lamp" in out
    rc, out, _ = run(capsys, "devices", "--json")
    payload = json.loads(out)
    assert payload["devices"][0]["id"] == "AA:BB"


def test_light_state(capsys):
    rc, out, _ = run(capsys, "light", "state")
    assert rc == 0 and "Floor Lamp" in out and "40" in out
    rc, out, _ = run(capsys, "light", "state", "--device", "floor lamp", "--json")
    payload = json.loads(out)
    assert payload["brightness"] == 40 and payload["reachable"] is True


def test_light_scenes(capsys):
    rc, out, _ = run(capsys, "light", "scenes", "--json")
    assert json.loads(out)["scenes"][0]["name"] == "Sunset"


def test_light_overview(capsys):
    rc, out, _ = run(capsys, "light", "overview")
    assert rc == 0 and "light" in out
    rc, out, _ = run(capsys, "light")
    assert rc == 0 and "light" in out


def test_device_mismatch_error(capsys):
    rc, _, err = run(capsys, "light", "state", "--device", "ghost")
    assert rc == 1 and "hint:" in err


# --- every hint LampService emits must parse --------------------------------------------------


def _hints():
    hints = []
    for change in (
        Change(on=True),
        Change(on=False),
        Change(brightness=30),
        Change(color=(255, 106, 43)),
        Change(kelvin=3000),
    ):
        for device in (None, "Floor Lamp"):
            for fade in (None, 0.0, 2.5):
                hints += svc.cli_for_change(change, device, fade, 1.5)
    for device in (None, "AA:BB"):
        hints += svc.cli_for_scene("Warm Sunset", device)
    return hints


@pytest.mark.parametrize("hint", _hints())
def test_every_service_hint_parses(hint):
    tokens = shlex.split(hint)
    assert tokens[0] == "govee"
    args = _build_parser().parse_args(tokens[1:])
    assert args.command == "light" and args.apply is True


def test_source_rules_is_passed_to_the_service_and_logged(capsys, fake_service):
    rc, out, _ = run(capsys, "light", "brightness", "40", "--source", "rules", "--json")
    assert rc == 0
    assert fake_service[-1] == ("rules", "tester")
    rc, _, _ = run(capsys, "light", "on", "--json")
    assert fake_service[-1][0] == "cli"


def test_source_rejects_other_values(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["light", "on", "--source", "web"])
    assert exc.value.code == 1
    assert "hint:" in capsys.readouterr().err
