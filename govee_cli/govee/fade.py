"""Client-side fades over the LAN API (approved deviation d11).

Govee has no native transition parameter, so govee-cli ramps brightness, colour and colour
temperature itself: :func:`plan_fade` turns a start state and a target :class:`Change` into timed
steps of LAN commands, and :class:`FadeRunner` sends them on schedule until done or cancelled.

* ``govee light on/off`` fade by default over ``GOVEE_FADE_DEFAULT`` seconds (5); brightness,
  colour and temperature are instant unless a fade is asked for. ``0`` snaps; the maximum is 3600.
* Fade-in (lamp off): brightness 1, turn on, then ramp up to the explicit brightness, else the
  brightness remembered by the last fade-out, else the off lamp's own brightness, else 100.
* Fade-out: ramp down to 1, then turn off (nothing is sent after ``turn off``). The pre-fade
  brightness is remembered per device in ``$XDG_STATE_HOME/govee-cli/fade.json`` (0600).
* A step is emitted only when the integer brightness, the rounded RGB or the rounded kelvin
  changes, so a 20-minute 1->100 ramp is about 100 steps. Steps are at least ``min_interval``
  apart. RGB and kelvin interpolate linearly; a colour<->kelvin cross-fade interpolates in RGB
  (``kelvin_to_rgb`` for the kelvin side) and ends with the exact target command.
* Over the cloud API (rate-limited) LampService applies the change instantly with a warning.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from govee_cli.cli._errors import EXIT_USER_ERROR, CliError
from govee_cli.govee import lan as lan_mod
from govee_cli.govee.service_types import Change
from govee_cli.govee.types import KELVIN_MAX, KELVIN_MIN, Command, LampState, kelvin_to_rgb

ENV_FADE_DEFAULT = "GOVEE_FADE_DEFAULT"
DEFAULT_FADE_SECONDS = 5.0  # 1.5 s looked abrupt on the real lamp (deviation d32)
MAX_FADE_SECONDS = 3600.0
MIN_INTERVAL = 0.15
MAX_TICKS = 2400  # a 3600 s fade then ticks every 1.5 s; steps are emitted only on change
KELVIN_ROUND = 10
CLOUD_FADE_WARNING = "fade needs LAN; applied instantly over cloud"

RGB = tuple[int, int, int]
Step = tuple[float, list[Command]]


# --- seconds ---------------------------------------------------------------------------------


def validate_fade_seconds(value: Any) -> Optional[float]:
    """``None`` passes through; otherwise a finite number 0-3600 as float, else ``ValueError``."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"fade seconds must be a number 0-{MAX_FADE_SECONDS:g}, got {value!r}")
    seconds = float(value)
    if not math.isfinite(seconds) or not 0 <= seconds <= MAX_FADE_SECONDS:
        raise ValueError(f"fade seconds must be 0-{MAX_FADE_SECONDS:g}, got {value!r}")
    return seconds


def default_fade_seconds() -> float:
    """``GOVEE_FADE_DEFAULT`` (seconds, 0-3600) or 1.5; an invalid value is ``CliError(1)``."""
    raw = os.environ.get(ENV_FADE_DEFAULT, "").strip()
    if not raw:
        return DEFAULT_FADE_SECONDS
    try:
        seconds = validate_fade_seconds(float(raw))
    except ValueError:
        raise CliError(
            EXIT_USER_ERROR,
            f"{ENV_FADE_DEFAULT} must be seconds 0-{MAX_FADE_SECONDS:g}, got {raw!r}",
            f"set {ENV_FADE_DEFAULT} to a number like 1.5, or unset it",
        ) from None
    return float(seconds or 0.0)


def resolve_fade_seconds(change: Change, fade_seconds: Any) -> float:
    """The fade duration for ``change``: explicit value, else the on/off default, else 0."""
    explicit = validate_fade_seconds(fade_seconds)
    if explicit is not None:
        return explicit
    return default_fade_seconds() if change.on is not None else 0.0


def fade_kind(change: Change) -> str:
    """Event verb: ``fade_on`` | ``fade_off`` | ``fade``."""
    if change.on is True:
        return "fade_on"
    if change.on is False:
        return "fade_off"
    return "fade"


# --- plan ------------------------------------------------------------------------------------


@dataclass
class FadePlan:
    """Timed LAN steps ``[(seconds_from_start, [Command, ...]), ...]``.

    ``verify`` is the state the lamp should be in at the end; ``remember_brightness`` is the
    pre-fade brightness a fade-out stores. A plan parsed from ``govee serve`` has no ``steps``
    but keeps the step count (``n_steps``)."""

    kind: str
    seconds: float
    interval_ms: int
    steps: list[Step]
    verify: Optional[Change] = None
    remember_brightness: Optional[int] = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    n_steps: Optional[int] = None

    def __post_init__(self) -> None:
        if self.n_steps is None:
            self.n_steps = len(self.steps)

    def commands(self) -> list[Command]:
        return [c for _, cmds in self.steps for c in cmds]

    def target(self) -> dict[str, Any]:
        return self.verify.to_dict() if self.verify is not None else {}

    def to_dict(self) -> dict[str, Any]:
        """The ``fade`` field of the HTTP contract."""
        return {
            "seconds": float(self.seconds),
            "steps": int(self.n_steps or 0),
            "interval_ms": int(self.interval_ms),
            "id": self.id,
        }

    def step_plan(self) -> list[dict[str, Any]]:
        """Human/JSON step listing for dry runs: offset and the commands of each step."""
        return [
            {
                "at": round(at, 3),
                "commands": [{"verb": c.verb, "payload": c.payload} for c in cmds],
            }
            for at, cmds in self.steps
        ]

    @classmethod
    def from_dict(cls, data: dict[str, Any], kind: str = "fade") -> "FadePlan":
        return cls(
            kind=kind,
            seconds=float(data.get("seconds") or 0.0),
            interval_ms=int(data.get("interval_ms") or 0),
            steps=[],
            id=str(data.get("id") or ""),
            n_steps=int(data.get("steps") or 0),
        )


def _instant(change: Change) -> list[Command]:
    cmds = []
    if change.on is True:
        cmds.append(lan_mod.build_turn(True))
    if change.brightness is not None:
        cmds.append(lan_mod.build_brightness(change.brightness))
    if change.color is not None:
        cmds.append(lan_mod.build_color(change.color))
    if change.kelvin is not None:
        cmds.append(lan_mod.build_kelvin(change.kelvin))
    if change.on is False:
        cmds.append(lan_mod.build_turn(False))
    return cmds


def _lerp(a: float, b: float, f: float) -> float:
    return a + (b - a) * f


def _ease(f: float) -> float:
    """Sine ease-in-out, as in motion profiles: starts and stops gently (0 -> 0, 1 -> 1)."""
    return (1 - math.cos(math.pi * f)) / 2


def _round_kelvin(k: float) -> int:
    k = int(round(k / KELVIN_ROUND)) * KELVIN_ROUND
    return max(KELVIN_MIN, min(KELVIN_MAX, k))


def _start_rgb(state: LampState) -> Optional[RGB]:
    if state.kelvin:
        return kelvin_to_rgb(state.kelvin)
    return state.color


class _Ramp:
    """Endpoints of one fade; ``build`` turns them into emitted-on-change steps."""

    def __init__(self) -> None:
        self.first: list[Command] = []  # sent at t=0
        self.last: list[Command] = []  # sent after the final ramp values
        self.brightness: Optional[tuple[int, int]] = None
        self.rgb: Optional[tuple[RGB, RGB]] = None
        self.kelvin: Optional[tuple[int, int]] = None
        self.exact: Optional[Command] = None  # exact final colour/kelvin command

    def build(self, ticks: int, interval: float) -> list[Step]:
        steps: list[Step] = [(0.0, list(self.first))] if self.first else []
        cursor = _RampCursor(self)
        for i in range(1, ticks + 1):
            cmds = cursor.tick(i / ticks, final=i == ticks)
            if cmds:
                steps.append((round(i * interval, 6), cmds))
        return steps


class _RampCursor:
    """The last values a :class:`_Ramp` emitted: each tick sends only what changed."""

    def __init__(self, ramp: _Ramp) -> None:
        self.ramp = ramp
        self.brightness = ramp.brightness[0] if ramp.brightness else None
        self.rgb = ramp.rgb[0] if ramp.rgb else None
        self.kelvin = ramp.kelvin[0] if ramp.kelvin else None
        self.color_cmd: Optional[Command] = None

    def tick(self, f: float, *, final: bool) -> list[Command]:
        # Chosen with the user at the real lamp (deviation d32): brightness steps linearly,
        # colour and temperature ease in and out on a sine.
        eased = _ease(f)
        cmds: list[Command] = []
        self._brightness_step(f, cmds)
        self._rgb_step(eased, cmds)
        self._kelvin_step(eased, cmds)
        if final:
            exact = self.ramp.exact
            if exact is not None and exact != self.color_cmd:
                cmds.append(exact)
            cmds.extend(self.ramp.last)
        return cmds

    def _brightness_step(self, f: float, cmds: list[Command]) -> None:
        if not self.ramp.brightness:
            return
        b = int(round(_lerp(*self.ramp.brightness, f)))
        if b != self.brightness:
            cmds.append(lan_mod.build_brightness(b))
            self.brightness = b

    def _rgb_step(self, eased: float, cmds: list[Command]) -> None:
        if not self.ramp.rgb:
            return
        start, end = self.ramp.rgb
        rgb = tuple(int(round(_lerp(s, e, eased))) for s, e in zip(start, end))
        if rgb != self.rgb:
            self.color_cmd = lan_mod.build_color(rgb)  # type: ignore[arg-type]
            cmds.append(self.color_cmd)
            self.rgb = rgb  # type: ignore[assignment]

    def _kelvin_step(self, eased: float, cmds: list[Command]) -> None:
        if not self.ramp.kelvin:
            return
        k = _round_kelvin(_lerp(*self.ramp.kelvin, eased))
        if k != self.kelvin:
            self.color_cmd = lan_mod.build_kelvin(k)
            cmds.append(self.color_cmd)
            self.kelvin = k


def _plan_off(ramp: _Ramp, lit: Optional[LampState]) -> Optional[int]:
    """Fade-out of a ``lit`` lamp (else an instant off); the brightness to remember."""
    if lit is None:
        ramp.first = [lan_mod.build_turn(False)]
        return None
    ramp.brightness = (lit.brightness, 1)
    ramp.last = [lan_mod.build_turn(False)]
    return lit.brightness


def _plan_on(
    ramp: _Ramp, start: Optional[LampState], change: Change, remembered: Optional[int]
) -> Change:
    """Fade-in of an off lamp; the state to verify at the end."""
    # The lamp's own level wins; the level remembered from a fade-out only replaces the
    # brightness 1 that the fade-out left behind.
    target = change.brightness
    if not target and start is not None and start.brightness > 1:
        target = start.brightness
    target = int(target or remembered or 100)
    ramp.first = [lan_mod.build_brightness(1), lan_mod.build_turn(True)]
    if change.color is not None:
        ramp.first.append(lan_mod.build_color(change.color))
    if change.kelvin is not None:
        ramp.first.append(lan_mod.build_kelvin(change.kelvin))
    ramp.brightness = (1, target)
    return Change(on=True, brightness=target, color=change.color, kelvin=change.kelvin)


def _plan_adjust(ramp: _Ramp, start: LampState, change: Change) -> None:
    """Fade of a lit lamp: brightness, then temperature or colour."""
    if change.brightness is not None:
        ramp.brightness = (start.brightness, change.brightness)
    if change.kelvin is not None and start.kelvin:
        ramp.kelvin = (start.kelvin, change.kelvin)
        ramp.exact = lan_mod.build_kelvin(change.kelvin)
    elif change.kelvin is not None or change.color is not None:
        _plan_rgb(ramp, start, change)


def _plan_rgb(ramp: _Ramp, start: LampState, change: Change) -> None:
    """Colour (or colour<->kelvin cross-fade) in RGB, ending with the exact target command."""
    target_rgb = change.color or kelvin_to_rgb(int(change.kelvin or 0))
    exact = (
        lan_mod.build_color(change.color)
        if change.color is not None
        else lan_mod.build_kelvin(int(change.kelvin or 0))
    )
    begin = _start_rgb(start)
    if begin is None:
        ramp.first.append(exact)
    else:
        ramp.rgb = (begin, target_rgb)
        ramp.exact = exact


def plan_fade(
    start: Optional[LampState],
    change: Change,
    seconds: float,
    *,
    remembered: Optional[int] = None,
    min_interval: float = MIN_INTERVAL,
    max_ticks: int = MAX_TICKS,
) -> FadePlan:
    """Plan a LAN fade from ``start`` (``None`` = unknown) to ``change`` over ``seconds``."""
    seconds = float(validate_fade_seconds(seconds) or 0.0)
    kind = fade_kind(change)
    if seconds == 0:
        return FadePlan(kind, 0.0, 0, [(0.0, _instant(change))], verify=change)
    ticks = max(1, min(max_ticks, int(math.floor(seconds / min_interval + 1e-9))))
    interval = seconds / ticks
    ramp = _Ramp()
    verify = change
    remember: Optional[int] = None
    lamp_on = start is not None and start.on

    if change.on is False:
        remember = _plan_off(ramp, start if lamp_on else None)
        verify = Change(on=False)
    elif change.on is True and start is None:
        # The lamp could not be read: it may already be lit, so a dip to 1 % could be a visible
        # flash. Turn it on as asked, without a ramp.
        ramp.first = _instant(change)
    elif change.on is True and not lamp_on:
        verify = _plan_on(ramp, start, change, remembered)
    elif lamp_on and start is not None:
        _plan_adjust(ramp, start, change)
    else:
        # Lamp off (or unknown) and not turning on: nothing visible to ramp; send it instantly.
        ramp.first = _instant(change)

    steps = ramp.build(ticks, interval)
    if not steps:
        steps = [(0.0, _instant(change))]
    return FadePlan(
        kind,
        seconds,
        int(round(interval * 1000)),
        steps,
        verify=verify,
        remember_brightness=remember,
    )


# --- remembered pre-fade brightness ----------------------------------------------------------

_state_lock = threading.Lock()


def fade_state_path() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "govee-cli" / "fade.json"


def _read_state(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def remembered_brightness(device_id: str) -> Optional[int]:
    """The brightness the last fade-out of ``device_id`` started from, if any."""
    entry = _read_state(fade_state_path()).get(device_id.upper())
    if isinstance(entry, dict):
        entry = entry.get("brightness")
    if isinstance(entry, int) and not isinstance(entry, bool) and 1 <= entry <= 100:
        return entry
    return None


def remember_brightness(device_id: str, brightness: int) -> None:
    """Store ``brightness`` for ``device_id`` (atomic replace, mode 0600)."""
    path = fade_state_path()
    with _state_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = _read_state(path)
        data[device_id.upper()] = {"brightness": int(brightness)}
        fd, tmp = tempfile.mkstemp(prefix=".fade", suffix=".tmp", dir=str(path.parent))
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


# --- runner ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class FadeOutcome:
    ok: bool  # every send reported success
    cancelled: bool
    steps_done: int


class FadeRunner:
    """Send a plan's steps on schedule. ``send(command) -> bool`` sends one command (callers
    take the LAN lock per command, never across waits). Between steps the runner waits with
    ``sleep`` when one is injected (tests), else ``cancel.wait`` so a cancel interrupts the
    wait at once; ``cancel`` is checked before every step."""

    def __init__(
        self,
        plan: FadePlan,
        send: Callable[[Command], bool],
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Optional[Callable[[float], None]] = None,
        cancel: Optional[threading.Event] = None,
    ) -> None:
        self.plan = plan
        self.send = send
        self.clock = clock
        self.sleep = sleep
        self.cancel = cancel or threading.Event()
        self.started: Optional[float] = None
        self.steps_done = 0

    def _wait(self, seconds: float) -> None:
        if self.sleep is not None:
            self.sleep(seconds)
        else:
            self.cancel.wait(seconds)

    def progress(self) -> float:
        if self.started is None:
            return 0.0
        if self.plan.seconds <= 0:
            return 1.0
        return max(0.0, min(1.0, (self.clock() - self.started) / self.plan.seconds))

    def run(self) -> FadeOutcome:
        self.started = self.clock()
        ok = True
        for at, cmds in self.plan.steps:
            while not self.cancel.is_set():
                remaining = self.started + at - self.clock()
                if remaining <= 0:
                    break
                self._wait(remaining)
            if self.cancel.is_set():
                return FadeOutcome(ok, True, self.steps_done)
            for cmd in cmds:
                ok = bool(self.send(cmd)) and ok
            self.steps_done += 1
        return FadeOutcome(ok, False, self.steps_done)
