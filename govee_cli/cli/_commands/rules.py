"""``govee rules actor`` — print the culture-rules runner actor definition for this machine.

culture-rules starts runner commands with ``PATH=/usr/local/bin:/usr/bin:/bin`` only, so every
``argv[0]`` must be the absolute path of the ``govee`` binary. That path is per machine (``uv tool
install`` puts it in the user's home), so the definition is generated here from the running
binary instead of being committed with one machine's home path in it.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import sys
from typing import Any

from govee_cli.cli._commands.overview import emit_overview
from govee_cli.cli._errors import EXIT_USER_ERROR, CliError
from govee_cli.cli._output import emit_result

FADE_TIMEOUT = 3660  # a fade may run up to 3600 s; never let culture-rules kill one mid-ramp
STEP_TIMEOUT = 30

# name -> (verb argv after "light", typed params, timeout). Every command logs as source
# "rules" and applies the change: a rule acts on purpose, so the dry-run default is off.
COMMANDS: dict[str, tuple[list[str], dict[str, str], int]] = {
    "lamp_on": (["on"], {}, STEP_TIMEOUT),
    "lamp_off": (["off"], {}, STEP_TIMEOUT),
    "lamp_brightness": (["brightness", "{pct}"], {"pct": "int"}, STEP_TIMEOUT),
    "lamp_color": (["color", "{hex}"], {"hex": "string"}, STEP_TIMEOUT),
    "lamp_temp": (["temp", "{kelvin}"], {"kelvin": "int"}, STEP_TIMEOUT),
    "lamp_scene": (["scene", "{name}"], {"name": "string"}, STEP_TIMEOUT),
    "lamp_fade_on": (["on", "--fade", "{seconds}"], {"seconds": "int"}, FADE_TIMEOUT),
    "lamp_fade_off": (["off", "--fade", "{seconds}"], {"seconds": "int"}, FADE_TIMEOUT),
}


def govee_binary() -> str:
    """Absolute path of the ``govee`` console script that is running (or found on PATH)."""
    argv0 = sys.argv[0] if sys.argv and sys.argv[0] else ""
    if os.path.basename(argv0) == "govee" and os.path.isfile(argv0):
        return os.path.abspath(argv0)
    found = shutil.which("govee")
    return os.path.abspath(found) if found else ""


def actor_definition(binary: str, machine: str) -> dict[str, Any]:
    """The culture-rules ``runner`` actor for ``binary`` on ``machine``."""
    if not os.path.isabs(binary):
        raise ValueError(f"the govee binary path must be absolute, got {binary!r}")
    commands = {
        name: {
            "argv": [binary, "light", *verb, "--source", "rules", "--apply", "--json"],
            "params": dict(params),
            "timeout": timeout,
        }
        for name, (verb, params, timeout) in COMMANDS.items()
    }
    return {
        "id": f"{machine}-govee",
        "name": f"Govee lamp ({machine})",
        "description": (
            f"Controls the Govee lamp through the govee CLI on {machine}. "
            "Every command applies the change."
        ),
        "kind": "runner",
        "machine": machine,
        "capabilities": ["lamp"],
        "params": {"commands": commands},
    }


def cmd_actor(args: argparse.Namespace) -> int:
    binary = args.bin or govee_binary()
    if not binary:
        raise CliError(
            EXIT_USER_ERROR,
            "cannot find the govee binary",
            "install it (uv tool install govee-cli) or pass --bin /absolute/path/to/govee",
        )
    machine = args.machine or socket.gethostname().split(".")[0]
    try:
        definition = actor_definition(binary, machine)
    except ValueError as exc:
        raise CliError(EXIT_USER_ERROR, str(exc), "pass --bin with an absolute path") from None
    if args.json:
        emit_result(definition, json_mode=True)
    else:
        emit_result(json.dumps(definition, indent=2), json_mode=False)
    return 0


def cmd_overview(args: argparse.Namespace) -> int:
    emit_overview(
        "govee-cli rules",
        [
            {
                "title": "Verbs",
                "items": [
                    "rules actor [--bin PATH] [--machine NAME] — print the culture-rules "
                    "runner actor definition (read-only)",
                ],
            }
        ],
        json_mode=bool(getattr(args, "json", False)),
    )
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("rules", help="culture-rules integration ('rules actor').")
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")
    p.set_defaults(func=cmd_overview)
    rsub = p.add_subparsers(dest="rules_command", parser_class=type(p))
    ac = rsub.add_parser("actor", help="Print the culture-rules runner actor definition.")
    ac.add_argument("--bin", help="Absolute path of govee (default: the running binary).")
    ac.add_argument("--machine", help="culture-rules machine name (default: this host's name).")
    ac.add_argument("--json", action="store_true", help="Emit compact JSON.")
    ac.set_defaults(func=cmd_actor)
    ov = rsub.add_parser("overview", help="Describe the rules verbs.")
    ov.add_argument("--json", action="store_true", help="Emit structured JSON.")
    ov.set_defaults(func=cmd_overview)
