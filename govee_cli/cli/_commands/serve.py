"""``govee serve`` — run the web console and loopback API (imports the server lazily)."""

from __future__ import annotations

import argparse

from govee_cli.cli._errors import EXIT_ENV_ERROR, CliError


def cmd_serve(args: argparse.Namespace) -> int:
    try:
        from govee_cli.web.server import run
    except ImportError as exc:
        raise CliError(
            EXIT_ENV_ERROR,
            f"the web server is not available ({exc.__class__.__name__})",
            "reinstall govee-cli (the govee_cli.web package is part of it)",
        ) from None
    kwargs: dict[str, object] = {"rotate_token": bool(args.rotate_token)}
    if args.host is not None:
        kwargs["host"] = args.host
    if args.port is not None:
        kwargs["port"] = args.port
    return int(run(**kwargs) or 0)


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("serve", help="Run the web console and API (all lamp traffic routes here).")
    p.add_argument("--host", help="Bind address (server default applies when omitted).")
    p.add_argument("--port", type=int, help="Port (default 8742).")
    p.add_argument(
        "--rotate-token", action="store_true", help="Mint a fresh pairing token at startup."
    )
    p.add_argument("--json", action="store_true", help="Emit errors as JSON.")
    p.set_defaults(func=cmd_serve)
