"""Shared helpers for the lamp verbs (``devices``, ``light``): service handle and rendering."""

from __future__ import annotations

import getpass
import os
from typing import Any

from govee_cli.cli._errors import CliError
from govee_cli.cli._output import emit_diagnostic, emit_result
from govee_cli.govee.service import ApplyResult, LampService, map_error


def actor() -> str:
    """Who is acting, for the event log: ``$USER`` (falls back to the login name)."""
    name = os.environ.get("USER", "").strip()
    if name:
        return name
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 - no passwd entry in some containers
        return "unknown"


def connect() -> LampService:
    """Service handle: routed through ``govee serve`` when it runs, else direct."""
    try:
        return LampService.connect(source="cli", actor=actor())
    except ValueError as exc:
        raise map_error(exc) from None


def user_errors(exc: BaseException) -> CliError:
    """Map ``ValueError`` (bad argument) / transport errors to a ``CliError``."""
    return map_error(exc)


def _fmt_command(c: dict[str, Any]) -> str:
    import json

    transport = c.get("transport")
    prefix = f"{transport} " if transport else ""
    return f"{prefix}{c['verb']} {json.dumps(c['payload'], separators=(',', ':'))}"


def fade_preview(result: ApplyResult) -> dict[str, Any] | None:
    """First and last fade step (from ``FadePlan.step_plan()``), or ``None`` without a fade."""
    if result.fade is None:
        return None
    steps = result.fade.step_plan()
    if not steps:
        return None
    return {"first": steps[0], "last": steps[-1]}


def render_apply(result: ApplyResult, *, label: str) -> str:
    data = result.to_dict()
    lines = []
    if result.applied:
        lines.append(f"applied: {label} (status: {result.status})")
    else:
        lines.append(f"dry run: {label} (nothing sent)")
    fade = data["fade"]
    preview = fade_preview(result)
    if fade is not None:
        lines.append(
            f"fade: {fade['seconds']:g} s, {fade['steps']} steps, interval {fade['interval_ms']} ms"
        )
        if preview is not None:
            for key in ("first", "last"):
                step = preview[key]
                cmds = "; ".join(_fmt_command(c) for c in step["commands"]) or "(none)"
                lines.append(f"  {key} step (+{step['at']:g} s): {cmds}")
    else:
        verb = "sent" if result.applied else "would send"
        lines.append(f"{verb}:")
        for c in data["commands"]:
            lines.append(f"  {_fmt_command(c)}")
    if not result.applied and data["cli"]:
        lines.append("to send, re-run with --apply:")
        for line in data["cli"]:
            lines.append(f"  {line}")
    if result.cancelled_fade:
        lines.append(f"cancelled running fade {result.cancelled_fade}")
    return "\n".join(lines)


def emit_apply(result: ApplyResult, *, label: str, json_mode: bool) -> None:
    if json_mode:
        payload = result.to_dict()
        payload["fade_preview"] = fade_preview(result)
        emit_result(payload, json_mode=True)
        return
    for warning in result.warnings:
        emit_diagnostic(f"warning: {warning}")
    emit_result(render_apply(result, label=label), json_mode=False)
