"""Value types and error mapping shared by LampService and its two backends."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from typing import Any, Optional

from govee_cli.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, CliError
from govee_cli.govee.cloud import (
    CloudError,
    CloudUnavailable,
    MissingKey,
    RateLimited,
    Unauthorized,
)
from govee_cli.govee.lan import LanError, PortBusy
from govee_cli.govee.types import KELVIN_MAX, KELVIN_MIN, Command, Device, LampState
from govee_cli.stats.store import SOURCES

_HEX = re.compile(r"^#?([0-9a-fA-F]{6})$")

REMEDIATION_KEY = "set GOVEE_API_KEY or start govee serve with the key in ~/.config/govee-cli/env"
REMEDIATION_NO_DEVICE = (
    "enable LAN Control for the lamp in the Govee Home app, allow inbound UDP 4002 on this host, "
    "set GOVEE_LAN_INTERFACE to the interface on the lamp's network, or set GOVEE_API_KEY to use "
    "the cloud API"
)
REMEDIATION_TOKEN = "govee token new --name <name> --save"  # nosec B105 - a hint


def parse_hex_color(value: str) -> tuple[int, int, int]:
    """``"FF6A2B"`` or ``"#ff6a2b"`` -> ``(255, 106, 43)``; ``ValueError`` otherwise."""
    m = _HEX.match(value.strip())
    if not m:
        raise ValueError(f"color must be 6 hex digits like FF6A2B, got {value!r}")
    h = m.group(1)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def hex_color(rgb: Optional[tuple[int, int, int]]) -> Optional[str]:
    """``(255, 106, 43)`` -> ``"#FF6A2B"`` (``None`` passes through)."""
    if rgb is None:
        return None
    return "#{:02X}{:02X}{:02X}".format(*rgb)


def check_source(source: str) -> str:
    if source not in SOURCES:
        raise ValueError(f"source must be one of {', '.join(SOURCES)}, got {source!r}")
    return source


@dataclass(frozen=True)
class Change:
    """A desired light change: any non-empty subset; ``color`` and ``kelvin`` are exclusive."""

    on: Optional[bool] = None
    brightness: Optional[int] = None
    color: Optional[tuple[int, int, int]] = None
    kelvin: Optional[int] = None

    def __post_init__(self) -> None:
        if self.on is None and self.brightness is None and self.color is None:
            if self.kelvin is None:
                raise ValueError("a change needs at least one of on, brightness, color, kelvin")
        if self.color is not None and self.kelvin is not None:
            raise ValueError("color and kelvin are mutually exclusive")
        if self.brightness is not None and not 1 <= int(self.brightness) <= 100:
            raise ValueError(f"brightness must be 1-100, got {self.brightness}")
        if self.kelvin is not None and not KELVIN_MIN <= int(self.kelvin) <= KELVIN_MAX:
            raise ValueError(f"kelvin must be {KELVIN_MIN}-{KELVIN_MAX}, got {self.kelvin}")
        if self.color is not None:
            if len(self.color) != 3 or not all(0 <= int(c) <= 255 for c in self.color):
                raise ValueError(f"color must be three 0-255 components, got {self.color}")
            object.__setattr__(self, "color", tuple(int(c) for c in self.color))

    def to_dict(self) -> dict[str, Any]:
        """Wire/body form (``color`` as ``#RRGGBB``), omitting unset fields."""
        out: dict[str, Any] = {}
        if self.on is not None:
            out["on"] = bool(self.on)
        if self.brightness is not None:
            out["brightness"] = int(self.brightness)
        if self.color is not None:
            out["color"] = hex_color(self.color)
        if self.kelvin is not None:
            out["kelvin"] = int(self.kelvin)
        return out


@dataclass
class ApplyResult:
    """Outcome of ``apply``/``set_scene``. ``commands`` are the planned (and, if applied, sent)
    wire commands; ``cli`` are equivalent ``govee`` invocations; ``event_ids`` are local event-log
    rows (empty when routed through serve, which logs on its side)."""

    applied: bool
    commands: list[Command]
    cli: list[str]
    status: str
    event_ids: list[int] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "applied": self.applied,
            "commands": [
                {"verb": c.verb, "transport": c.transport, "payload": c.payload}
                for c in self.commands
            ],
            "cli": list(self.cli),
            "status": self.status,
            "event_ids": list(self.event_ids),
        }


@dataclass(frozen=True)
class LampStatus:
    """Device plus observed state; ``state`` is ``None`` when the lamp did not answer."""

    device: Device
    transport: Optional[str]
    state: Optional[LampState]

    @property
    def reachable(self) -> bool:
        return self.state is not None

    def to_dict(self) -> dict[str, Any]:
        """The ``GET /api/state`` shape."""
        s = self.state
        return {
            "device": {
                "id": self.device.id,
                "name": self.device.name,
                "model": self.device.model,
                "transport": self.transport,
            },
            "reachable": s is not None,
            "on": bool(s.on) if s else False,
            "brightness": int(s.brightness) if s else 0,
            "mode": "white" if s and s.kelvin else "color",
            "color": hex_color(s.color) if s and not s.kelvin else None,
            "kelvin": s.kelvin if s else None,
            "scene": s.scene if s else None,
        }


@dataclass(frozen=True)
class SceneInfo:
    id: str
    name: str

    def to_dict(self) -> dict[str, str]:
        return {"id": self.id, "name": self.name}


# --- cli hints -------------------------------------------------------------------------------


def _hint(args: list[str], device: Optional[str]) -> str:
    parts = ["govee", *args]
    if device:
        parts += ["--device", device]
    parts.append("--apply")
    return " ".join(shlex.quote(p) for p in parts)


def cli_for_change(change: Change, device: Optional[str] = None) -> list[str]:
    """Equivalent ``govee light ...`` invocations, one per changed attribute."""
    out = []
    if change.on is True:
        out.append(_hint(["light", "on"], device))
    if change.brightness is not None:
        out.append(_hint(["light", "brightness", str(change.brightness)], device))
    if change.color is not None:
        out.append(_hint(["light", "color", (hex_color(change.color) or "")[1:]], device))
    if change.kelvin is not None:
        out.append(_hint(["light", "temp", str(change.kelvin)], device))
    if change.on is False:
        out.append(_hint(["light", "off"], device))
    return out


def cli_for_scene(name: str, device: Optional[str] = None) -> list[str]:
    return [_hint(["light", "scene", name], device)]


# --- error mapping ---------------------------------------------------------------------------


def map_error(exc: BaseException) -> CliError:
    """Map a transport exception to ``CliError`` (exit 2 for environment problems).

    Messages come from the transports, which never include the API key or a token.
    """
    if isinstance(exc, CliError):
        return exc
    if isinstance(exc, PortBusy):
        return CliError(
            EXIT_ENV_ERROR,
            str(exc),
            "stop the other Govee LAN client, run commands through `govee serve`, or set "
            "GOVEE_API_KEY so the cloud API can be used instead",
        )
    if isinstance(exc, MissingKey):
        return CliError(EXIT_ENV_ERROR, "the Govee cloud API needs GOVEE_API_KEY", REMEDIATION_KEY)
    if isinstance(exc, RateLimited):
        when = exc.reset_at.isoformat() if exc.reset_at else "in about a minute"
        return CliError(
            EXIT_ENV_ERROR,
            str(exc),
            f"wait until {when} and retry; prefer the LAN API for frequent changes",
        )
    if isinstance(exc, Unauthorized):
        return CliError(
            EXIT_ENV_ERROR,
            str(exc),
            "check GOVEE_API_KEY (request a new key in the Govee Home app if it was revoked)",
        )
    if isinstance(exc, CloudUnavailable):
        return CliError(
            EXIT_ENV_ERROR, str(exc), "check the internet connection, or use the LAN API"
        )
    if isinstance(exc, CloudError):
        return CliError(EXIT_ENV_ERROR, str(exc), "retry; if it persists, use the LAN API")
    if isinstance(exc, LanError):
        return CliError(EXIT_ENV_ERROR, str(exc), REMEDIATION_NO_DEVICE)
    if isinstance(exc, ValueError):
        return CliError(EXIT_USER_ERROR, str(exc), "check the command's arguments")
    return CliError(
        EXIT_ENV_ERROR, f"lamp operation failed ({type(exc).__name__})", "run `govee doctor`"
    )


def select_device(devices: list[Device], selector: Optional[str]) -> Device:
    """Pick a device by id or (case-insensitive) name; default the only one."""
    if not devices:
        raise CliError(EXIT_ENV_ERROR, "no Govee device found", REMEDIATION_NO_DEVICE)
    names = ", ".join(f"{d.name} ({d.id})" for d in devices)
    if selector is None:
        if len(devices) == 1:
            return devices[0]
        raise CliError(
            EXIT_USER_ERROR,
            f"several devices found: {names}",
            "pick one with --device <name|id> (MCP/web: the 'device' field)",
        )
    wanted = selector.strip().lower()
    for d in devices:
        if d.id.lower() == wanted:
            return d
    matches = [d for d in devices if d.name.lower() == wanted]
    if len(matches) == 1:
        return matches[0]
    reason = "matches several devices" if matches else "matches no device"
    raise CliError(
        EXIT_USER_ERROR,
        f"device {selector!r} {reason}; known: {names}",
        "pick a device by its exact id or name",
    )
