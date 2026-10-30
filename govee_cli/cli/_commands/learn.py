"""``govee-cli learn`` — the learnability affordance.

Prints a structured self-teaching prompt. Must satisfy the agent-first rubric:
>=200 chars and mention purpose, command map, exit codes, --json, and explain.
"""

from __future__ import annotations

import argparse

from govee_cli import __version__
from govee_cli.cli._output import emit_result

_TEXT = """\
govee-cli — agent-first CLI that controls a Govee floor lamp (H6076) at home.

Purpose
-------
Discover the lamp, switch it on/off with fades, set brightness, colour, colour
temperature and scenes, over the local LAN API (no key) and the Govee cloud API
(GOVEE_API_KEY). Writes are SAFE BY DEFAULT: without --apply a write prints the
commands it would send and exits 0. `govee serve` adds a web console and, while
it runs, all lamp traffic routes through it (clients use GOVEE_TOKEN or the
client token file written by `govee token new --save`). Installed command: govee.

Commands
--------
  govee devices                      List devices.
  govee light on|off                 Power, fading by default (--fade S, 0 snaps).
  govee light brightness N           1-100.
  govee light color RRGGBB           Hex colour.
  govee light temp N                 Colour temperature in kelvin (2000-9000).
  govee light scene NAME             Cloud scene.
  govee light state | scenes         Read-only.
  govee light fade-status | fade-stop  Inspect or stop a running fade.
  govee stats                        Usage tiles and recent events.
  govee token new|list|revoke        Client tokens for serve (new --save writes the file).
  govee serve [--host --port --rotate-token]  Web console + API.
  govee mcp serve                    MCP server over stdio.
  govee rules actor                  Print the culture-rules runner actor for this machine.
  govee whoami | learn | explain <path> | overview | doctor | cli overview

Every write verb takes --device X, --fade SECONDS and --apply.

Machine-readable output
-----------------------
Every command supports --json. Errors in JSON mode emit
{"code", "message", "remediation"} to stderr. Stdout and stderr never mix.

Exit-code policy
----------------
  0 success (a dry run is a success)
  1 user-input error (bad flag, bad value, unknown device or scene)
  2 environment error (lamp or API unreachable, missing API key or token)
  3+ reserved
  130 a foreground fade was stopped with Ctrl-C

More detail
-----------
  govee explain govee-cli
  govee explain light
  govee doctor      # LAN, API key and stats checks with remediation
"""


_COMMANDS = [
    (["whoami"], "Identity probe from culture.yaml."),
    (["learn"], "Self-teaching prompt."),
    (["explain"], "Markdown docs by path."),
    (["overview"], "Descriptive snapshot of the agent."),
    (["doctor"], "Identity, LAN, API-key and stats-db checks."),
    (["cli", "overview"], "Describe the CLI surface."),
    (["devices"], "List Govee devices."),
    (["light", "on"], "Turn on (fades by default). Dry run unless --apply."),
    (["light", "off"], "Turn off (fades by default). Dry run unless --apply."),
    (["light", "brightness"], "Set brightness 1-100."),
    (["light", "color"], "Set colour as RRGGBB."),
    (["light", "temp"], "Set colour temperature in kelvin."),
    (["light", "scene"], "Activate a cloud scene by name."),
    (["light", "state"], "Show the lamp's state."),
    (["light", "scenes"], "List scenes."),
    (["light", "fade-status"], "Show the running fade."),
    (["light", "fade-stop"], "Stop the running fade."),
    (["light", "overview"], "Describe the light verbs."),
    (["stats"], "Usage tiles and recent events."),
    (["token", "new"], "Mint a client token (--save writes the client token file)."),
    (["token", "list"], "List tokens."),
    (["token", "revoke"], "Revoke a token or all tokens."),
    (["token", "overview"], "Describe the token verbs."),
    (["serve"], "Run the web console and API."),
    (["mcp", "serve"], "Run the MCP server over stdio."),
    (["mcp", "overview"], "Describe the mcp verbs."),
    (["rules", "actor"], "Print the culture-rules runner actor definition."),
    (["rules", "overview"], "Describe the rules verbs."),
]


def _as_json_payload() -> dict[str, object]:
    return {
        "tool": "govee-cli",
        "version": __version__,
        "purpose": (
            "Agent-first control of a Govee floor lamp over the LAN and cloud APIs; "
            "writes are dry runs unless --apply."
        ),
        "commands": [{"path": path, "summary": summary} for path, summary in _COMMANDS],
        "exit_codes": {
            "0": "success (a dry run is a success)",
            "1": "user-input error",
            "2": "environment/setup error",
            "130": "foreground fade stopped with Ctrl-C",
        },
        "json_support": True,
        "explain_pointer": "govee-cli explain <path>",
    }


def cmd_learn(args: argparse.Namespace) -> int:
    if getattr(args, "json", False):
        emit_result(_as_json_payload(), json_mode=True)
    else:
        emit_result(_TEXT, json_mode=False)
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "learn",
        help="Print a structured self-teaching prompt for agent consumers.",
    )
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")
    p.set_defaults(func=cmd_learn)
