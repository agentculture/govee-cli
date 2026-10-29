"""Core Govee types shared by every transport and by LampService."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, runtime_checkable

KELVIN_MIN = 2000
KELVIN_MAX = 9000


@dataclass(frozen=True)
class Device:
    """A Govee lamp. ``ip`` is only known for LAN-discovered devices."""

    id: str
    name: str
    model: str
    ip: Optional[str] = None
    transports: tuple[str, ...] = ()


@dataclass(frozen=True)
class LampState:
    """Observed or desired lamp state. ``brightness`` is 1-100."""

    on: bool
    brightness: int
    color: Optional[tuple[int, int, int]] = None
    kelvin: Optional[int] = None
    scene: Optional[str] = None


@dataclass(frozen=True)
class Command:
    """A wire command: ``transport`` is "lan" or "cloud"; ``payload`` is the JSON dict sent."""

    verb: str
    transport: str
    payload: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Transport(Protocol):
    """Minimal transport surface implemented by LAN, cloud and fake transports."""

    def discover(self) -> list[Device]: ...

    def get_state(self, device: Device) -> Optional[LampState]: ...

    def send(self, device: Device, command: Command) -> bool: ...


def kelvin_to_rgb(kelvin: int) -> tuple[int, int, int]:
    """Approximate RGB for a colour temperature (Tanner Helland), clamped to 2000-9000 K."""
    t = max(KELVIN_MIN, min(KELVIN_MAX, kelvin)) / 100.0
    if t <= 66:
        r = 255.0
        g = 99.4708025861 * math.log(t) - 161.1195681661
    else:
        r = 329.698727446 * (t - 60) ** -0.1332047592
        g = 288.1221695283 * (t - 60) ** -0.0755148492
    if t >= 66:
        b = 255.0
    elif t <= 19:
        b = 0.0
    else:
        b = 138.5177312231 * math.log(t - 10) - 305.0447927307

    def clamp(v: float) -> int:
        return int(max(0, min(255, round(v))))

    return clamp(r), clamp(g), clamp(b)
