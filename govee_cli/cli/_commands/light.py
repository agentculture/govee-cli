"""``govee light`` — read and change the lamp.

Writes are dry runs unless ``--apply`` is passed: they print the planned commands and exit 0.
``on``/``off`` fade by default (``GOVEE_FADE_DEFAULT``, 1.5 s); ``--fade SECONDS`` is accepted on
every verb (0 snaps, max 3600) and the other verbs are instant unless it is given. Without
``govee serve`` a fade runs in the foreground and Ctrl-C stops it (exit 130).
"""

from __future__ import annotations

import argparse

from govee_cli.cli._commands import _lamp
from govee_cli.cli._commands.overview import emit_overview
from govee_cli.cli._output import emit_diagnostic, emit_result
from govee_cli.govee.service import Change, parse_hex_color

EXIT_INTERRUPTED = 130

_VERB_LINES = [
    "light on|off [--fade S] — power (fades by default, 1.5 s)",
    "light brightness N — 1-100",
    "light color RRGGBB — hex colour",
    "light temp N — colour temperature in kelvin",
    "light scene NAME — cloud scene",
    "light state / scenes — read-only",
    "light fade-status / fade-stop — inspect or stop a running fade",
    "every write: --device X, --fade S, --apply (dry run without it), --json",
]


def _write(args: argparse.Namespace, change_fn, label: str) -> int:
    json_mode = bool(args.json)
    try:
        change = change_fn(args)
        service = _lamp.connect(getattr(args, "source", "cli"))
        result = service.apply(change, apply=args.apply, device=args.device, fade_seconds=args.fade)
    except ValueError as exc:
        raise _lamp.user_errors(exc) from None
    except KeyboardInterrupt:
        emit_diagnostic("fade stopped (Ctrl-C); the lamp keeps its current level")
        return EXIT_INTERRUPTED
    _lamp.emit_apply(result, label=label, json_mode=json_mode)
    return 0


def cmd_on(args: argparse.Namespace) -> int:
    return _write(args, lambda a: Change(on=True), "light on")


def cmd_off(args: argparse.Namespace) -> int:
    return _write(args, lambda a: Change(on=False), "light off")


def cmd_brightness(args: argparse.Namespace) -> int:
    return _write(args, lambda a: Change(brightness=a.level), f"brightness {args.level}")


def cmd_color(args: argparse.Namespace) -> int:
    def build(a: argparse.Namespace) -> Change:
        return Change(color=parse_hex_color(a.hex))

    return _write(args, build, f"color {args.hex}")


def cmd_temp(args: argparse.Namespace) -> int:
    return _write(args, lambda a: Change(kelvin=a.kelvin), f"color temperature {args.kelvin} K")


def cmd_scene(args: argparse.Namespace) -> int:
    if args.fade is not None:
        emit_diagnostic("note: scenes change instantly; --fade is ignored")
    try:
        result = _lamp.connect(getattr(args, "source", "cli")).set_scene(
            args.name, apply=args.apply, device=args.device
        )
    except ValueError as exc:
        raise _lamp.user_errors(exc) from None
    _lamp.emit_apply(result, label=f"scene {args.name}", json_mode=bool(args.json))
    return 0


def cmd_state(args: argparse.Namespace) -> int:
    status = _lamp.connect().state(args.device).to_dict()
    if args.json:
        emit_result(status, json_mode=True)
        return 0
    dev = status["device"]
    head = f"{dev['name']} ({dev['model']}) via {dev['transport'] or 'unknown'}"
    if not status["reachable"]:
        emit_result(f"{head}: not reachable", json_mode=False)
        return 0
    detail = (
        f"{status['kelvin']} K"
        if status["mode"] == "white"
        else (status["color"] or "colour unknown")
    )
    lines = [
        head,
        f"power: {'on' if status['on'] else 'off'}",
        f"brightness: {status['brightness']}",
        f"{status['mode']}: {detail}",
    ]
    if status["scene"]:
        lines.append(f"scene: {status['scene']}")
    emit_result("\n".join(lines), json_mode=False)
    return 0


def cmd_scenes(args: argparse.Namespace) -> int:
    scenes = [s.to_dict() for s in _lamp.connect().scenes(args.device)]
    if args.json:
        emit_result({"scenes": scenes}, json_mode=True)
    else:
        emit_result("\n".join(s["name"] for s in scenes) or "no scenes", json_mode=False)
    return 0


def cmd_fade_status(args: argparse.Namespace) -> int:
    active = _lamp.connect().active_fade(args.device)
    if args.json:
        emit_result({"active": active}, json_mode=True)
    elif active is None:
        emit_result("no fade running", json_mode=False)
    else:
        emit_result("fade running: " + ", ".join(f"{k}={v}" for k, v in active.items()), False)
    return 0


def cmd_fade_stop(args: argparse.Namespace) -> int:
    cancelled = bool(_lamp.connect().cancel_fade(args.device))
    if args.json:
        emit_result({"cancelled": cancelled}, json_mode=True)
    else:
        emit_result("fade stopped" if cancelled else "no fade running", json_mode=False)
    return 0


def cmd_overview(args: argparse.Namespace) -> int:
    emit_overview(
        "govee-cli light",
        [{"title": "Verbs", "items": list(_VERB_LINES)}],
        json_mode=bool(getattr(args, "json", False)),
    )
    return 0


def _common(p: argparse.ArgumentParser, *, write: bool, fade: bool = True) -> None:
    p.add_argument("--device", help="Device name or id (needed with several lamps).")
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")
    if write:
        p.add_argument(
            "--apply", action="store_true", help="Send the change (default: dry run, print plan)."
        )
        p.add_argument(
            "--source",
            choices=("cli", "rules"),
            default="cli",
            help="Log the change as coming from cli (default) or rules (the culture-rules runner).",
        )
        p.add_argument(
            "--fade",
            type=_seconds,
            metavar="SECONDS",
            help="Fade duration, 0-3600 (0 snaps; on/off default GOVEE_FADE_DEFAULT = 1.5).",
        )


def _seconds(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {value!r}") from None
    return seconds


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("light", help="Control the lamp (writes dry-run unless --apply).")
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")
    p.set_defaults(func=cmd_overview, device=None)
    lsub = p.add_subparsers(dest="light_command", parser_class=type(p))

    def verb(name, func, help_, **kw):
        sp = lsub.add_parser(name, help=help_)
        _common(sp, write=kw.get("write", True))
        sp.set_defaults(func=func)
        return sp

    verb("on", cmd_on, "Turn on (fades by default).")
    verb("off", cmd_off, "Turn off (fades by default).")
    sp = verb("brightness", cmd_brightness, "Set brightness 1-100.")
    sp.add_argument("level", type=int, help="Brightness 1-100.")
    sp = verb("color", cmd_color, "Set an RGB colour.")
    sp.add_argument("hex", help="Colour as RRGGBB (a leading # is fine).")
    sp = verb("temp", cmd_temp, "Set colour temperature in kelvin.")
    sp.add_argument("kelvin", type=int, help="Kelvin, 2000-9000.")
    sp = verb("scene", cmd_scene, "Activate a cloud scene by name.")
    sp.add_argument("name", help="Scene name (list them with 'light scenes').")
    verb("state", cmd_state, "Show the lamp's current state.", write=False)
    verb("scenes", cmd_scenes, "List available scenes.", write=False)
    verb("fade-status", cmd_fade_status, "Show the running fade, if any.", write=False)
    verb("fade-stop", cmd_fade_stop, "Stop the running fade.", write=False)
    ov = lsub.add_parser("overview", help="Describe the light verbs.")
    ov.add_argument("--json", action="store_true", help="Emit structured JSON.")
    ov.set_defaults(func=cmd_overview)
