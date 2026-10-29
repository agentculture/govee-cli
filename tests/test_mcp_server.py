"""MCP stdio server (t9): protocol, tool schemas, dry-run rule, errors, fades, subprocess e2e."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys

import pytest

from govee_cli.govee.fake import FakeTransport
from govee_cli.govee.service import LampService
from govee_cli.govee.types import Device, LampState
from govee_cli.mcp import server
from govee_cli.stats import store

DEV = Device(id="AA:BB", name="Floor Lamp", model="H6076", ip="192.0.2.10", transports=("lan",))
ON = LampState(on=True, brightness=100, color=(255, 255, 255))
OFF = LampState(on=False, brightness=100, color=(255, 255, 255))


def rpc(id_, method, params=None):
    msg = {"jsonrpc": "2.0", "method": method}
    if id_ is not None:
        msg["id"] = id_
    if params is not None:
        msg["params"] = params
    return msg


def init_msg(name="claude-code"):
    return rpc(
        1,
        "initialize",
        {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": name, "version": "1"},
        },
    )


def call(id_, tool, **arguments):
    return rpc(id_, "tools/call", {"name": tool, "arguments": arguments})


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("GOVEE_FADE_DEFAULT", "0")
    monkeypatch.delenv("GOVEE_ACTOR", raising=False)


@pytest.fixture
def lan():
    return FakeTransport([DEV], state=ON)


@pytest.fixture
def factory(lan, monkeypatch):
    made = []

    def make(source, actor):
        made.append((source, actor))
        return LampService.direct(
            source=source, actor=actor, lan=lan, cloud=FakeTransport([]), sleep=lambda s: None
        )

    monkeypatch.setattr(server, "make_service", make)
    return made


def session(messages):
    out = io.StringIO()
    code = server.run(
        stdin=io.StringIO("".join(json.dumps(m) + "\n" for m in messages)), stdout=out
    )
    assert code == 0
    return [json.loads(line) for line in out.getvalue().splitlines()]


def payload(resp):
    assert not resp["result"].get("isError"), resp
    return resp["result"]["structuredContent"]


def test_initialize_negotiates_and_ping(factory):
    r = session([init_msg(), rpc(2, "ping")])
    res = r[0]["result"]
    assert res["protocolVersion"] == "2025-06-18"
    assert "tools" in res["capabilities"]
    assert res["serverInfo"]["name"] == "govee-cli"
    assert r[1] == {"jsonrpc": "2.0", "id": 2, "result": {}}


def test_notifications_get_no_reply(factory):
    r = session([init_msg(), rpc(None, "notifications/initialized"), rpc(3, "ping")])
    assert [x["id"] for x in r] == [1, 3]


def test_tools_list_schemas(factory):
    r = session([init_msg(), rpc(2, "tools/list")])
    tools = {t["name"]: t for t in r[1]["result"]["tools"]}
    assert set(tools) == {
        "list_devices",
        "get_state",
        "set_light",
        "set_scene",
        "get_stats",
        "get_fade",
        "cancel_fade",
    }
    for t in tools.values():
        assert t["inputSchema"]["type"] == "object"
        assert t["description"]
    props = tools["set_light"]["inputSchema"]["properties"]
    assert set(props) == {"device", "on", "brightness", "color", "kelvin", "fade_seconds", "apply"}
    assert props["apply"]["default"] is False
    assert props["brightness"]["minimum"] == 1 and props["brightness"]["maximum"] == 100
    assert props["kelvin"]["minimum"] == 2000 and props["kelvin"]["maximum"] == 9000
    assert tools["set_scene"]["inputSchema"]["required"] == ["name"]
    for n in ("set_light", "set_scene"):
        assert "dry run" in tools[n]["description"].lower()
        assert "apply" in tools[n]["description"]


def test_dry_run_then_apply(factory, lan):
    lan.state = OFF
    r = session(
        [
            init_msg("agent-x"),
            call(2, "set_light", on=True),
            call(3, "set_light", on=True, apply=True),
        ]
    )
    dry, applied = payload(r[1]), payload(r[2])
    assert dry["applied"] is False and applied["applied"] is True
    assert len(lan.sent) == len(applied["commands"]) >= 1
    assert factory == [("mcp", "agent-x")]
    # content text mirrors structuredContent
    assert json.loads(r[2]["result"]["content"][0]["text"]) == applied


def test_actor_prefers_env(factory, monkeypatch):
    monkeypatch.setenv("GOVEE_ACTOR", "envactor")
    session([init_msg("agent-x"), call(2, "list_devices")])
    assert factory == [("mcp", "envactor")]


def test_actor_default_without_init(factory):
    session([call(2, "list_devices")])
    assert factory == [("mcp", "mcp")]


def test_read_tools(factory, tmp_path):
    r = session([init_msg(), call(2, "list_devices"), call(3, "get_state"), call(4, "get_stats")])
    assert payload(r[1])["devices"][0]["id"] == "AA:BB"
    assert payload(r[2])["on"] is True
    stats = payload(r[3])
    for key in ("on_today_h", "on_week_h", "commands_7d", "agent_commands_7d", "recent_events"):
        assert key in stats


def test_bad_color_is_tool_error(factory):
    r = session([init_msg(), call(2, "set_light", color="nope")])
    res = r[1]["result"]
    assert res["isError"] is True
    assert res["content"][0]["text"].startswith("error: ")
    assert "Traceback" not in res["content"][0]["text"]


def test_cli_error_has_message_and_hint(factory, monkeypatch):
    from govee_cli.cli._errors import CliError

    class Boom:
        def devices(self, **kw):
            raise CliError(2, "lamp unreachable", "check power")

    monkeypatch.setattr(server, "make_service", lambda s, a: Boom())
    r = session([init_msg(), call(2, "list_devices")])
    assert r[1]["result"] == {
        "isError": True,
        "content": [{"type": "text", "text": "error: lamp unreachable\nhint: check power"}],
    }


def test_unexpected_exception_has_no_traceback(factory, monkeypatch):
    class Boom:
        def devices(self, **kw):
            raise RuntimeError("kaboom")

    monkeypatch.setattr(server, "make_service", lambda s, a: Boom())
    r = session([init_msg(), call(2, "list_devices")])
    assert r[1]["result"]["isError"] is True
    assert "Traceback" not in r[1]["result"]["content"][0]["text"]


def test_protocol_errors(factory):
    r = session(
        [
            rpc(2, "nope/method"),
            rpc(3, "tools/call", {"name": "no_such_tool", "arguments": {}}),
            rpc(4, "tools/call", {"arguments": {}}),
            rpc(5, "tools/call", {"name": "set_light", "arguments": {"brightness": "x"}}),
        ]
    )
    assert r[0]["error"]["code"] == -32601
    assert r[1]["error"]["code"] == -32602
    assert r[2]["error"]["code"] == -32602
    assert r[3]["error"]["code"] == -32602


def test_parse_error_and_blank_lines(factory):
    out = io.StringIO()
    server.run(stdin=io.StringIO("\n{not json\n"), stdout=out)
    resp = json.loads(out.getvalue())
    assert resp["error"]["code"] == -32700 and resp["id"] is None


def test_fade_tools(factory, lan):
    r = session(
        [
            init_msg(),
            call(2, "get_fade"),
            call(3, "cancel_fade"),
            call(4, "set_light", brightness=50, fade_seconds=0, apply=True),
            call(5, "set_light", brightness=50, fade_seconds=5000),
        ]
    )
    assert payload(r[1]) == {"fade": None}
    assert payload(r[2]) == {"cancelled": False}
    assert payload(r[3])["applied"] is True
    assert r[4]["result"]["isError"] is True


def test_set_scene_dry_run(factory):
    r = session([init_msg(), call(2, "set_scene", name="Sunset")])
    res = r[1]["result"]
    # Fake cloud has no scenes: must be a clean tool result either way, never a crash.
    assert "content" in res


def test_never_reads_api_key():
    src = open(server.__file__, encoding="utf-8").read()
    assert "GOVEE_API_KEY" not in src.replace("Never read GOVEE_API_KEY", "")


def test_subprocess_dry_run_then_apply(tmp_path):
    env = dict(os.environ)
    env.update(
        GOVEE_MCP_TEST_FAKE="1",
        GOVEE_FADE_DEFAULT="0",
        XDG_STATE_HOME=str(tmp_path / "state"),
        XDG_CONFIG_HOME=str(tmp_path / "config"),
        GOVEE_SERVE_URL="http://127.0.0.1:9",
    )
    env.pop("GOVEE_ACTOR", None)
    msgs = [
        init_msg("sub-client"),
        rpc(None, "notifications/initialized"),
        rpc(2, "tools/list"),
        call(3, "set_light", on=True),
        call(6, "get_state"),
        call(4, "set_light", on=True, apply=True),
        call(5, "get_state"),
    ]
    proc = subprocess.run(
        [sys.executable, "-m", "govee_cli.mcp.server"],
        input="".join(json.dumps(m) + "\n" for m in msgs),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    r = {m["id"]: m for m in map(json.loads, proc.stdout.splitlines())}
    assert len(r[2]["result"]["tools"]) == 7
    dry, applied = payload(r[3]), payload(r[4])
    assert dry["applied"] is False
    assert applied["applied"] is True
    assert payload(r[6])["on"] is False  # the dry run sent nothing
    assert payload(r[5])["on"] is True
    os.environ["XDG_STATE_HOME"] = str(tmp_path / "state")
    events = [e for e in store.recent_events() if e["actor"] == "sub-client"]
    assert events and all(e["source"] == "mcp" for e in events)
