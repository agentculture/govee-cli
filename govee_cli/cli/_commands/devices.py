"""``govee devices`` — list the Govee devices the service can see (read-only)."""

from __future__ import annotations

import argparse

from govee_cli.cli._commands import _lamp
from govee_cli.cli._output import JSON_HELP, emit_result


def cmd_devices(args: argparse.Namespace) -> None:
    devices = _lamp.connect().devices(refresh=bool(args.refresh))
    rows = [
        {
            "id": d.id,
            "name": d.name,
            "model": d.model,
            "ip": d.ip,
            "transports": list(d.transports),
        }
        for d in devices
    ]
    if args.json:
        emit_result({"devices": rows}, json_mode=True)
        return
    if not rows:
        emit_result("no devices found", json_mode=False)
        return
    lines = [
        f"{r['name']} ({r['model']}) id={r['id']}"
        + (f" ip={r['ip']}" if r["ip"] else "")
        + (f" via {','.join(r['transports'])}" if r["transports"] else "")
        for r in rows
    ]
    emit_result("\n".join(lines), json_mode=False)


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("devices", help="List Govee devices (LAN discovery, then cloud).")
    p.add_argument("--refresh", action="store_true", help="Bypass the short discovery cache.")
    p.add_argument("--json", action="store_true", help=JSON_HELP)
    p.set_defaults(func=cmd_devices)
