"""``govee mcp serve`` — stdio MCP server (imported lazily; stdout is the protocol channel)."""

from __future__ import annotations

import argparse

from govee_cli.cli._commands.overview import emit_overview
from govee_cli.cli._errors import EXIT_ENV_ERROR, CliError


def cmd_serve(args: argparse.Namespace) -> int:
    try:
        from govee_cli.mcp.server import run
    except ImportError as exc:
        raise CliError(
            EXIT_ENV_ERROR,
            f"the MCP server is not available ({exc.__class__.__name__})",
            "reinstall govee-cli (the govee_cli.mcp package is part of it)",
        ) from None
    return int(run() or 0)


def cmd_overview(args: argparse.Namespace) -> int:
    emit_overview(
        "govee-cli mcp",
        [{"title": "Verbs", "items": ["mcp serve — run the MCP server over stdio"]}],
        json_mode=bool(getattr(args, "json", False)),
    )
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("mcp", help="MCP server entry point ('mcp serve').")
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")
    p.set_defaults(func=cmd_overview)
    msub = p.add_subparsers(dest="mcp_command", parser_class=type(p))
    sv = msub.add_parser("serve", help="Run the MCP server over stdio.")
    sv.add_argument("--json", action="store_true", help="Emit errors as JSON.")
    sv.set_defaults(func=cmd_serve)
    ov = msub.add_parser("overview", help="Describe the mcp verbs.")
    ov.add_argument("--json", action="store_true", help="Emit structured JSON.")
    ov.set_defaults(func=cmd_overview)
