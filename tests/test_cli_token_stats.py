"""t8: ``govee token ...`` and ``govee stats``."""

from __future__ import annotations

import json
import stat

import pytest

from govee_cli.auth import tokens
from govee_cli.cli import main
from govee_cli.govee.service import client_token_path
from govee_cli.stats import store


def run(capsys, *argv):
    rc = main(list(argv))
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


@pytest.fixture(autouse=True)
def _no_route(monkeypatch):
    monkeypatch.setattr("govee_cli.govee.lan.default_route_ip", lambda: "192.0.2.5")


def test_token_new_prints_secret_and_link(capsys):
    rc, out, err = run(capsys, "token", "new", "--name", "laptop", "--json")
    assert rc == 0 and err == ""
    payload = json.loads(out)
    assert payload["name"] == "laptop" and payload["secret"]
    assert payload["pairing_link"] == tokens.pairing_link(payload["secret"], "192.0.2.5", 8742)
    assert payload["saved_to"] is None
    assert tokens.verify(payload["secret"]) == "laptop"


def test_token_new_text_prints_once(capsys):
    rc, out, _ = run(capsys, "token", "new", "--name", "a", "--port", "9000")
    assert rc == 0 and ":9000/#" in out


def test_token_new_save_mode_0600(capsys):
    rc, out, _ = run(capsys, "token", "new", "--name", "cli", "--save", "--json")
    payload = json.loads(out)
    path = client_token_path()
    assert str(path) == payload["saved_to"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert path.read_text().strip() == payload["secret"]


def test_token_duplicate_name_is_user_error(capsys):
    run(capsys, "token", "new", "--name", "x")
    rc, out, err = run(capsys, "token", "new", "--name", "x")
    assert rc == 1 and "hint:" in err and out == ""


def test_token_list_never_shows_secrets(capsys):
    _, out, _ = run(capsys, "token", "new", "--name", "x", "--json")
    secret = json.loads(out)["secret"]
    rc, out, _ = run(capsys, "token", "list", "--json")
    assert rc == 0 and secret not in out
    assert json.loads(out)["tokens"][0]["name"] == "x"
    rc, out, _ = run(capsys, "token", "list")
    assert "x" in out and secret not in out


def test_token_revoke(capsys):
    run(capsys, "token", "new", "--name", "x")
    rc, out, _ = run(capsys, "token", "revoke", "x", "--json")
    assert rc == 0 and json.loads(out) == {"revoked": ["x"]}
    rc, _, err = run(capsys, "token", "revoke", "x")
    assert rc == 1 and "hint:" in err


def test_token_revoke_all(capsys):
    run(capsys, "token", "new", "--name", "a")
    run(capsys, "token", "new", "--name", "b")
    rc, out, _ = run(capsys, "token", "revoke", "--all", "--json")
    assert json.loads(out)["count"] == 2
    rc, _, err = run(capsys, "token", "revoke")
    assert rc == 1


def test_token_overview(capsys):
    rc, out, _ = run(capsys, "token", "overview")
    assert rc == 0 and "token" in out


def test_stats_json_and_text(capsys):
    store.record_event(source="cli", actor="t", verb="turn", payload={}, status="sent")
    rc, out, _ = run(capsys, "stats", "--json")
    payload = json.loads(out)
    assert rc == 0
    assert {"on_today_h", "on_week_h", "commands_7d", "agent_commands_7d"} <= set(payload["tiles"])
    assert payload["recent_events"]
    rc, out, _ = run(capsys, "stats")
    assert rc == 0 and "commands" in out


def test_stats_cli_and_mcp_count_local_days_like_the_console(monkeypatch, capsys):
    from govee_cli.cli import main
    from govee_cli.mcp import server as mcp_server
    from govee_cli.stats import store

    zones = []
    for name in ("on_today_h", "on_week_h", "commands_7d", "agent_commands_7d"):
        monkeypatch.setattr(store, name, lambda tz=None, **kw: zones.append(tz) or 0)
    monkeypatch.setattr(store, "avg_brightness_today", lambda tz=None, **kw: zones.append(tz))
    assert main(["stats", "--json"]) == 0
    capsys.readouterr()
    mcp_server._tool_get_stats(None, {})
    assert zones and all(isinstance(z, store.LocalTimezone) for z in zones)
