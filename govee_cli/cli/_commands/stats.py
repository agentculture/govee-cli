"""``govee stats`` — usage tiles and recent events from the local stats database."""

from __future__ import annotations

import argparse

from govee_cli.cli._errors import EXIT_ENV_ERROR, CliError
from govee_cli.cli._output import emit_result


def _collect() -> dict[str, object]:
    import sqlite3

    from govee_cli.stats import store

    try:
        avg = store.avg_brightness_today()
        return {
            "tiles": {
                "on_today_h": store.on_today_h(),
                "on_week_h": store.on_week_h(),
                "commands_7d": store.commands_7d(),
                "agent_commands_7d": store.agent_commands_7d(),
                "avg_brightness_today": avg,
            },
            "recent_events": store.recent_events(10),
        }
    except (sqlite3.Error, OSError) as exc:
        raise CliError(
            EXIT_ENV_ERROR,
            f"stats database unavailable ({type(exc).__name__})",
            "check that the directory of $XDG_STATE_HOME/govee-cli/stats.db is writable",
        ) from None


def _render(data: dict[str, object]) -> str:
    t = data["tiles"]
    avg = t["avg_brightness_today"]
    lines = [
        f"on today: {t['on_today_h']:.2f} h",
        f"on this week: {t['on_week_h']:.2f} h",
        f"commands (7 d): {t['commands_7d']}",
        f"agent commands (7 d): {t['agent_commands_7d']}",
        "avg brightness today: " + (f"{avg:.0f}" if avg is not None else "n/a"),
        "",
        "recent events:",
    ]
    events = data["recent_events"]
    if not events:
        lines.append("  (none)")
    for ev in events:
        lines.append("  " + "  ".join(f"{k}={ev[k]}" for k in ev if k != "payload"))
    return "\n".join(lines)


def cmd_stats(args: argparse.Namespace) -> int:
    data = _collect()
    if args.json:
        emit_result(data, json_mode=True)
    else:
        emit_result(_render(data), json_mode=False)
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("stats", help="Usage tiles and recent lamp events.")
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")
    p.set_defaults(func=cmd_stats)
