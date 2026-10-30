"""``govee token`` — manage client tokens for ``govee serve`` (new, list, revoke)."""

from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path

from govee_cli.cli._commands.overview import emit_overview
from govee_cli.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, CliError
from govee_cli.cli._output import JSON_HELP, emit_result

DEFAULT_PORT = 8742

_VERB_LINES = [
    "token new --name N [--save] [--port P] — mint a token; secret and pairing link shown once",
    "token list — names and timestamps (never secrets)",
    "token revoke NAME | --all — revoke tokens",
]


def _store_error(exc: Exception) -> CliError:
    from govee_cli.auth import tokens

    if isinstance(exc, tokens.TokenStoreError):
        return CliError(EXIT_ENV_ERROR, str(exc), "check the token store file permissions")
    if isinstance(exc, ValueError):
        return CliError(EXIT_USER_ERROR, str(exc), "pick another --name (letters, digits, . _ -)")
    return CliError(
        EXIT_ENV_ERROR, f"token store error ({type(exc).__name__})", "run 'govee doctor'"
    )


def _pairing_host() -> str:
    from govee_cli.govee import lan

    try:
        return lan.default_route_ip()
    except (lan.LanError, OSError):
        return "127.0.0.1"


def save_client_token(secret: str) -> Path:
    """Write the secret to the client token file: mode 0600, parent 0700, atomically."""
    from govee_cli.govee.service import client_token_path

    path = client_token_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".client-token.")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(secret + "\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def cmd_new(args: argparse.Namespace) -> int:
    from govee_cli.auth import tokens

    try:
        minted = tokens.new(args.name)
        link = tokens.pairing_link(minted, _pairing_host(), args.port)
    except (ValueError, tokens.TokenStoreError) as exc:
        raise _store_error(exc) from None
    saved = None
    if args.save:
        try:
            saved = str(save_client_token(minted))
        except OSError as exc:
            raise CliError(
                EXIT_ENV_ERROR,
                f"token created but could not be saved ({type(exc).__name__})",
                "set GOVEE_TOKEN to the secret instead, or fix the config directory permissions",
            ) from None
    payload = {"name": args.name, "secret": minted, "pairing_link": link, "saved_to": saved}
    if args.json:
        emit_result(payload, json_mode=True)
        return 0
    lines = [
        f"token '{args.name}' created. The secret is shown once; it is not stored.",
        f"secret: {minted}",
        f"pairing link (open in a browser): {link}",
    ]
    if saved:
        lines.append(f"saved to {saved} (mode 0600); the CLI will use it to reach 'govee serve'")
    else:
        lines.append("use it as GOVEE_TOKEN, or re-run with --save to write the client token file")
    emit_result("\n".join(lines), json_mode=False)
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    from govee_cli.auth import tokens

    try:
        items = tokens.list_tokens()
    except tokens.TokenStoreError as exc:
        raise _store_error(exc) from None
    if args.json:
        emit_result({"tokens": items}, json_mode=True)
    elif not items:
        emit_result("no tokens", json_mode=False)
    else:
        emit_result(
            "\n".join(
                f"{t['name']}  created={t['created']}  last_used={t['last_used'] or 'never'}"
                for t in items
            ),
            json_mode=False,
        )
    return 0


def cmd_revoke(args: argparse.Namespace) -> int:
    from govee_cli.auth import tokens

    if args.all == bool(args.name):
        raise CliError(
            EXIT_USER_ERROR,
            "give exactly one of a token NAME or --all",
            "see names with 'govee token list'",
        )
    try:
        if args.all:
            count = tokens.revoke_all()
            payload: dict[str, object] = {"revoked_all": True, "count": count}
            text = f"revoked {count} token(s)"
        else:
            tokens.revoke(args.name)
            payload = {"revoked": [args.name]}
            text = f"revoked '{args.name}'"
    except KeyError:
        raise CliError(
            EXIT_USER_ERROR, f"no token named {args.name!r}", "see names with 'govee token list'"
        ) from None
    except tokens.TokenStoreError as exc:
        raise _store_error(exc) from None
    emit_result(payload if args.json else text, json_mode=bool(args.json))
    return 0


def cmd_overview(args: argparse.Namespace) -> int:
    emit_overview(
        "govee-cli token",
        [{"title": "Verbs", "items": list(_VERB_LINES)}],
        json_mode=bool(getattr(args, "json", False)),
    )
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("token", help="Manage client tokens for 'govee serve'.")
    p.add_argument("--json", action="store_true", help=JSON_HELP)
    p.set_defaults(func=cmd_overview)
    tsub = p.add_subparsers(dest="token_command", parser_class=type(p))

    n = tsub.add_parser("new", help="Mint a token (secret shown once).")
    n.add_argument("--name", required=True, help="Token name (letters, digits, . _ -).")
    n.add_argument("--save", action="store_true", help="Write the secret to the client token file.")
    n.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help="Serve port for the pairing link."
    )
    n.add_argument("--json", action="store_true", help=JSON_HELP)
    n.set_defaults(func=cmd_new)

    ls = tsub.add_parser("list", help="List tokens (names and timestamps only).")
    ls.add_argument("--json", action="store_true", help=JSON_HELP)
    ls.set_defaults(func=cmd_list)

    rv = tsub.add_parser("revoke", help="Revoke a token by name, or all of them.")
    rv.add_argument("name", nargs="?", help="Token name.")
    rv.add_argument("--all", action="store_true", help="Revoke every token.")
    rv.add_argument("--json", action="store_true", help=JSON_HELP)
    rv.set_defaults(func=cmd_revoke)

    ov = tsub.add_parser("overview", help="Describe the token verbs.")
    ov.add_argument("--json", action="store_true", help=JSON_HELP)
    ov.set_defaults(func=cmd_overview)
