"""MCP server over stdio: line-delimited JSON-RPC 2.0 exposing the lamp as tools.

Targets MCP protocol version 2025-06-18. Never read GOVEE_API_KEY here: ``LampService`` owns the
transports (and routes through ``govee serve`` when it runs). The service is created lazily on the
first tool call, after ``initialize``, so the actor is known: ``GOVEE_ACTOR``, else the client's
``clientInfo.name``, else ``mcp``. Every call is logged with ``source=mcp``.

``set_light`` and ``set_scene`` are dry runs unless the call passes ``apply: true``. Tool failures
are MCP tool errors (``isError``) carrying the CliError message and remediation, never a traceback.
Only JSON-RPC goes to stdout; diagnostics go to stderr.

Test-only hook: ``GOVEE_MCP_TEST_FAKE=1`` makes :func:`make_service` build the service over an
in-memory fake lamp (starts off; ``send`` turns it on) instead of real transports, so a subprocess
test can drive the server without a lamp or network. Never set it outside tests.
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys
from typing import Any, Callable, Optional

from govee_cli import __version__
from govee_cli.cli._errors import CliError
from govee_cli.govee.service import Change, LampService, parse_hex_color
from govee_cli.govee.service_serve import serve_base_url, serve_running

PROTOCOL_VERSION = "2025-06-18"
SOURCE = "mcp"
ENV_ACTOR = "GOVEE_ACTOR"
ENV_TEST_FAKE = "GOVEE_MCP_TEST_FAKE"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602

_DRY = "This is a dry run unless apply is true: without it nothing is sent to the lamp."
_DEVICE = {"type": "string", "description": "Device id or name; omit for the default lamp."}
_APPLY = {
    "type": "boolean",
    "default": False,
    "description": "Send the change. Omitted or false means a dry run that sends nothing.",
}


def _schema(props: dict, required: Optional[list] = None) -> dict:
    out: dict[str, Any] = {"type": "object", "properties": props, "additionalProperties": False}
    if required:
        out["required"] = required
    return out


TOOLS: list[dict[str, Any]] = [
    {
        "name": "list_devices",
        "description": "List known Govee devices (read-only).",
        "inputSchema": _schema({}),
    },
    {
        "name": "get_state",
        "description": "Current state of a device: on/off, brightness, color (read-only).",
        "inputSchema": _schema({"device": _DEVICE}),
    },
    {
        "name": "set_light",
        "description": (
            "Change on/off, brightness, color or color temperature. " + _DRY + " Optional "
            "fade_seconds fades the change; omitted means on/off fades by the default and other "
            "changes are instant; 0 snaps."
        ),
        "inputSchema": _schema(
            {
                "device": _DEVICE,
                "on": {"type": "boolean", "description": "Turn the lamp on or off."},
                "brightness": {"type": "integer", "minimum": 1, "maximum": 100},
                "color": {"type": "string", "description": "Hex color, #RRGGBB."},
                "kelvin": {"type": "integer", "minimum": 2000, "maximum": 9000},
                "fade_seconds": {"type": "number", "minimum": 0, "maximum": 3600},
                "apply": _APPLY,
            }
        ),
    },
    {
        "name": "set_scene",
        "description": "Activate a named scene (cloud). " + _DRY,
        "inputSchema": _schema(
            {"name": {"type": "string"}, "device": _DEVICE, "apply": _APPLY}, ["name"]
        ),
    },
    {
        "name": "get_stats",
        "description": "Usage statistics: hours on, command counts, brightness, recent events.",
        "inputSchema": _schema({}),
    },
    {
        "name": "get_fade",
        "description": 'The fade currently running (or {"fade": null}) (read-only).',
        "inputSchema": _schema({"device": _DEVICE}),
    },
    {
        "name": "cancel_fade",
        "description": 'Stop a running fade. Returns {"cancelled": bool}.',
        "inputSchema": _schema({"device": _DEVICE}),
    },
]


# --- service construction -------------------------------------------------------------------


def _fake_service(actor: Optional[str]) -> LampService:
    from govee_cli.govee.fake import FakeTransport
    from govee_cli.govee.types import Device, LampState

    class _Lamp(FakeTransport):
        def send(self, device, command):
            self.state = LampState(on=True, brightness=100, color=(255, 255, 255))
            return super().send(device, command)

    dev = Device(id="AA:BB", name="Test Lamp", model="H6076", ip="192.0.2.10", transports=("lan",))
    lan = _Lamp([dev], state=LampState(on=False, brightness=100, color=(255, 255, 255)))
    return LampService.direct(
        source=SOURCE, actor=actor, lan=lan, cloud=FakeTransport([]), sleep=lambda s: None
    )


def make_service(source: str, actor: Optional[str]) -> LampService:
    if os.environ.get(ENV_TEST_FAKE) == "1":
        return _fake_service(actor)
    # Without serve, fades run on a background thread so the stdio loop keeps answering (and
    # ``cancel_fade`` can stop a fade this server started).
    return LampService.connect(source=source, actor=actor, background_fades=True)


def _serve_up() -> bool:
    if os.environ.get(ENV_TEST_FAKE) == "1":
        return False
    return serve_running(serve_base_url())


# --- tool handlers --------------------------------------------------------------------------


def _bad(msg: str) -> "_InvalidParams":
    return _InvalidParams(msg)


class _InvalidParams(Exception):
    pass


def _check(args: dict, name: str, kind: Any, label: str) -> Any:
    if name not in args or args[name] is None:
        return None
    v = args[name]
    if isinstance(v, bool) and kind is not bool:
        raise _bad(f"{name} must be {label}")
    if not isinstance(v, kind):
        raise _bad(f"{name} must be {label}")
    return v


def _tool_set_light(svc, a: dict) -> dict:
    on = _check(a, "on", bool, "a boolean")
    bri = _check(a, "brightness", int, "an integer")
    color = _check(a, "color", str, "a string")
    kelvin = _check(a, "kelvin", int, "an integer")
    fade = _check(a, "fade_seconds", (int, float), "a number")
    apply = _check(a, "apply", bool, "a boolean") or False
    change = Change(
        on=on,
        brightness=bri,
        color=parse_hex_color(color) if color is not None else None,
        kelvin=kelvin,
    )
    res = svc.apply(
        change,
        apply=apply,
        device=_check(a, "device", str, "a string"),
        fade_seconds=fade,
    )
    return res.to_dict()


def _tool_set_scene(svc, a: dict) -> dict:
    name = _check(a, "name", str, "a string")
    if not name:
        raise _bad("name is required")
    res = svc.set_scene(
        name,
        apply=_check(a, "apply", bool, "a boolean") or False,
        device=_check(a, "device", str, "a string"),
    )
    return res.to_dict()


def _tool_get_stats(svc, a: dict) -> dict:
    from govee_cli.stats import store

    tz = store.LocalTimezone()  # the same local days the web console counts
    return {
        "on_today_h": store.on_today_h(tz=tz),
        "on_week_h": store.on_week_h(tz=tz),
        "commands_7d": store.commands_7d(tz=tz),
        "agent_commands_7d": store.agent_commands_7d(tz=tz),
        "avg_brightness_today": store.avg_brightness_today(tz=tz),
        "hours_per_day": store.hours_per_day(tz=tz),
        "commands_by_source": store.commands_by_source(tz=tz),
        "recent_events": store.recent_events(),
    }


def _device(a: dict) -> Optional[str]:
    return _check(a, "device", str, "a string")


HANDLERS: dict[str, Callable[[Any, dict], dict]] = {
    "list_devices": lambda s, a: {"devices": [dataclasses.asdict(d) for d in s.devices()]},
    "get_state": lambda s, a: s.state(_device(a)).to_dict(),
    "set_light": _tool_set_light,
    "set_scene": _tool_set_scene,
    "get_stats": _tool_get_stats,
    "get_fade": lambda s, a: {"fade": s.active_fade(_device(a))},
    "cancel_fade": lambda s, a: {"cancelled": bool(s.cancel_fade(_device(a)))},
}


def _tool_error(text: str) -> dict:
    return {"isError": True, "content": [{"type": "text", "text": text}]}


def _format_error(code_msg: str, remediation: str) -> str:
    text = f"error: {code_msg}"
    return f"{text}\nhint: {remediation}" if remediation else text


# --- server ---------------------------------------------------------------------------------


class Server:
    def __init__(self) -> None:
        self.client_name: Optional[str] = None
        self._svcs: dict[bool, Any] = {}

    def actor(self) -> str:
        return os.environ.get(ENV_ACTOR) or self.client_name or "mcp"

    def service(self) -> Any:
        """The backend for this call: ``govee serve`` if it answers now, else in-process.

        Checked on every call (a refused loopback connect is instant), so serve starting or
        stopping during a session is picked up. Each kind is built once and kept, so a fade the
        in-process backend started stays visible to ``get_fade`` and ``cancel_fade``.
        """
        routed = _serve_up()
        if routed not in self._svcs:
            self._svcs[routed] = make_service(SOURCE, self.actor())
        return self._svcs[routed]

    def close(self) -> None:
        """Let fades this server started finish before the process exits."""
        for svc in self._svcs.values():
            wait = getattr(getattr(svc, "backend", None), "wait_for_fades", None)
            if callable(wait):
                wait()

    def call_tool(self, params: Any) -> dict:
        if not isinstance(params, dict) or not isinstance(params.get("name"), str):
            raise _bad("tools/call needs a tool name")
        name = params["name"]
        if name not in HANDLERS:
            raise _bad(f"unknown tool: {name}")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            raise _bad("arguments must be an object")
        try:
            data = HANDLERS[name](self.service(), args)
        except _InvalidParams:
            raise
        except CliError as exc:
            return _tool_error(_format_error(exc.message, exc.remediation))
        except ValueError as exc:
            return _tool_error(_format_error(str(exc), ""))
        except Exception as exc:  # noqa: BLE001 - never leak a traceback to the client
            print(f"govee mcp: {name} failed: {exc!r}", file=sys.stderr)
            return _tool_error(_format_error(f"{type(exc).__name__}: {exc}", ""))
        return {
            "content": [{"type": "text", "text": json.dumps(data)}],
            "structuredContent": data,
        }

    def handle(self, method: str, params: Any) -> dict:
        if method == "initialize":
            info = params.get("clientInfo") if isinstance(params, dict) else None
            if isinstance(info, dict) and isinstance(info.get("name"), str):
                self.client_name = info["name"]
            return {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "govee-cli", "version": __version__},
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": TOOLS}
        if method == "tools/call":
            return self.call_tool(params)
        raise KeyError(method)

    def process(self, line: str) -> Optional[dict]:
        try:
            msg = json.loads(line)
        except ValueError:
            return _err(None, PARSE_ERROR, "parse error")
        if not isinstance(msg, dict) or not isinstance(msg.get("method"), str):
            if isinstance(msg, dict) and ("result" in msg or "error" in msg):
                return None  # a response to something we never sent
            return _err(None, INVALID_REQUEST, "invalid request")
        rid = msg.get("id")
        if "id" not in msg:
            return None  # notification
        try:
            return {
                "jsonrpc": "2.0",
                "id": rid,
                "result": self.handle(msg["method"], msg.get("params")),
            }
        except KeyError:
            return _err(rid, METHOD_NOT_FOUND, f"method not found: {msg['method']}")
        except _InvalidParams as exc:
            return _err(rid, INVALID_PARAMS, str(exc))


def _err(rid: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def run(stdin=None, stdout=None) -> int:
    """Serve until EOF on ``stdin``; returns 0."""
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    server = Server()
    for line in stdin:
        if not line.strip():
            continue
        reply = server.process(line)
        if reply is not None:
            stdout.write(json.dumps(reply) + "\n")
            stdout.flush()
    server.close()
    return 0


if __name__ == "__main__":
    sys.exit(run())
