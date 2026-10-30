"""``govee rules actor``: the culture-rules runner definition, generated per machine."""

from __future__ import annotations

import json

import pytest

from govee_cli.cli import main


def run(capsys, *argv):
    rc = main(list(argv))
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


def test_actor_uses_the_given_binary_and_machine(capsys):
    rc, out, _ = run(capsys, "rules", "actor", "--bin", "/opt/govee/bin/govee", "--machine", "thor")
    assert rc == 0
    actor = json.loads(out)
    assert actor["machine"] == "thor"
    assert actor["id"] == "thor-govee"
    argv = actor["params"]["commands"]["lamp_color"]["argv"]
    assert argv[0] == "/opt/govee/bin/govee"
    assert argv[1:] == ["light", "color", "{hex}", "--source", "rules", "--apply", "--json"]


def test_actor_defaults_to_the_govee_on_path(capsys, monkeypatch, tmp_path):
    fake = tmp_path / "govee"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr("sys.argv", ["pytest"])
    rc, out, _ = run(capsys, "rules", "actor", "--json")
    assert rc == 0
    assert json.loads(out)["params"]["commands"]["lamp_on"]["argv"][0] == str(fake)


def test_actor_refuses_a_relative_binary(capsys):
    rc, _, err = run(capsys, "rules", "actor", "--bin", "govee")
    assert rc == 1
    assert "absolute" in err
    assert "hint:" in err


def test_actor_without_any_govee_binary_is_a_user_error(capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr("sys.argv", ["pytest"])
    rc, _, err = run(capsys, "rules", "actor")
    assert rc == 1
    assert "--bin" in err


@pytest.mark.parametrize("argv", [["rules"], ["rules", "overview"], ["explain", "rules", "actor"]])
def test_rules_noun_is_described(capsys, argv):
    rc, out, _ = run(capsys, *argv)
    assert rc == 0
    assert "rules actor" in out


def test_fade_on_to_takes_a_target_brightness(capsys):
    rc, out, _ = run(capsys, "rules", "actor", "--bin", "/opt/govee/bin/govee", "--machine", "thor")
    assert rc == 0
    command = json.loads(out)["params"]["commands"]["lamp_fade_on_to"]
    assert command["argv"][1:] == [
        "light",
        "on",
        "--brightness",
        "{pct}",
        "--fade",
        "{seconds}",
        "--source",
        "rules",
        "--apply",
        "--json",
    ]
    assert command["params"] == {"pct": "int", "seconds": "int"}
    assert command["timeout"] == 3660
