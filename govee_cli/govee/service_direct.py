"""Direct LampService backend: talks to the lamp over the LAN and cloud transports.

This is the only place in govee-cli that calls a ``Transport``. ``govee serve`` uses it
explicitly; CLI/MCP use it when no serve is running (see ``service.LampService.connect``).

* Device list: LAN discovery merged with the cloud device list by id (cloud names win).
  A missing ``GOVEE_API_KEY`` is tolerated while LAN finds the lamp.
* Transport per command: LAN when the device was LAN-discovered (it has an IP), else cloud.
  Scenes are always cloud. The commands sent on ``apply=True`` are exactly the planned ones.
* Event log: one event per call. Dry runs log ``dry_run`` (except ``source="web"``, approved
  deviation d7). Applied LAN writes log ``sent`` and are then verified with ``get_state`` for up
  to ``verify_timeout`` seconds, ending ``confirmed`` or ``unconfirmed``. Applied cloud writes log
  ``sent`` (cloud verification is out of scope; a non-200 control reply logs ``unconfirmed``).
* Fades (approved deviation d11, see ``fade.py``): one fade per device, LAN only (over the cloud
  the change is applied instantly with a warning). ``background_fades=True`` (``govee serve``)
  runs a fade in a daemon thread and returns ``sent`` at once; otherwise the fade runs in the
  calling thread and returns the verified status, and Ctrl-C (``KeyboardInterrupt``) stops it.
  Any applied write or scene for the device cancels its running fade first. A fade logs one
  event (verb ``fade_on``/``fade_off``/``fade``), ``sent`` at the start, then ``confirmed`` or
  ``unconfirmed`` after one verification at the end; a cancelled fade stays ``sent`` and its
  payload gains ``"cancelled": true``.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from govee_cli.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, CliError
from govee_cli.govee import cloud as cloud_mod
from govee_cli.govee import fade as fade_mod
from govee_cli.govee import lan as lan_mod
from govee_cli.govee.cloud import CloudError, MissingKey
from govee_cli.govee.lan import LanError
from govee_cli.govee.service_types import (
    ApplyResult,
    Change,
    LampStatus,
    SceneInfo,
    cli_for_change,
    cli_for_scene,
    map_error,
    select_device,
)
from govee_cli.govee.types import Command, Device, LampState
from govee_cli.stats import store

DEFAULT_VERIFY_TIMEOUT = 3.0
DEFAULT_POLL_INTERVAL = 0.25
DEFAULT_CACHE_TTL = 300.0
COLOR_TOLERANCE = 24  # per RGB channel; lamps quantise colour
KELVIN_TOLERANCE = 150
FADE_JOIN_TIMEOUT = 2.0  # how long a new write waits for a cancelled fade's thread to stop


def lan_commands(change: Change) -> list[Command]:
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


def cloud_commands(change: Change) -> list[Command]:
    cmds = []
    if change.on is True:
        cmds.append(cloud_mod.power_command(True))
    if change.brightness is not None:
        cmds.append(cloud_mod.brightness_command(change.brightness))
    if change.color is not None:
        cmds.append(cloud_mod.color_command(*change.color))
    if change.kelvin is not None:
        cmds.append(cloud_mod.kelvin_command(change.kelvin))
    if change.on is False:
        cmds.append(cloud_mod.power_command(False))
    return cmds


def state_matches(change: Change, state: Optional[LampState]) -> bool:
    """Does the observed ``state`` satisfy ``change``? on/brightness exact, colour tolerant."""
    if state is None:
        return False
    if change.on is not None and bool(state.on) != change.on:
        return False
    if change.on is False:
        return True  # an off lamp's other attributes are not observable
    if change.brightness is not None and state.brightness != change.brightness:
        return False
    if change.color is not None:
        if state.color is None or state.kelvin:
            return False
        if any(abs(a - b) > COLOR_TOLERANCE for a, b in zip(state.color, change.color)):
            return False
    if change.kelvin is not None:
        if not state.kelvin or abs(state.kelvin - change.kelvin) > KELVIN_TOLERANCE:
            return False
    return True


def _merge(lan_devices: list[Device], cloud_devices: list[Device]) -> list[Device]:
    merged: dict[str, Device] = {}
    for d in lan_devices:
        merged.setdefault(d.id.upper(), d)
    for c in cloud_devices:
        key = c.id.upper()
        lan_dev = merged.get(key)
        if lan_dev is None:
            merged[key] = c
            continue
        merged[key] = Device(
            id=lan_dev.id,
            name=c.name or lan_dev.name,
            model=c.model or lan_dev.model,
            ip=lan_dev.ip,
            transports=tuple(dict.fromkeys(lan_dev.transports + c.transports)),
        )
    return list(merged.values())


@dataclass
class _ActiveFade:
    plan: fade_mod.FadePlan
    device: Device
    runner: fade_mod.FadeRunner
    event_id: int
    payload: dict
    started: str
    cancel: threading.Event = field(default_factory=threading.Event)
    thread: Optional[threading.Thread] = None

    def to_dict(self) -> dict[str, Any]:
        """The ``active`` object of ``GET /api/fade``."""
        return {
            "id": self.plan.id,
            "kind": self.plan.kind,
            "device": {"id": self.device.id, "name": self.device.name},
            "started": self.started,
            "seconds": self.plan.seconds,
            "target": self.plan.target(),
            "progress": round(self.runner.progress(), 4),
        }


class DirectBackend:
    routed = False

    def __init__(
        self,
        *,
        lan: Any = None,
        cloud: Any = None,
        verify_timeout: float = DEFAULT_VERIFY_TIMEOUT,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        cache_ttl: float = DEFAULT_CACHE_TTL,
        clock: Optional[Callable[[], float]] = None,
        sleep: Optional[Callable[[float], None]] = None,
        background_fades: bool = False,
    ) -> None:
        self.lan = lan if lan is not None else lan_mod.LanTransport()
        self.cloud = cloud if cloud is not None else cloud_mod.CloudTransport()
        self.verify_timeout = verify_timeout
        self.poll_interval = poll_interval
        self.cache_ttl = cache_ttl
        self.clock = clock or time.monotonic
        self.sleep = sleep or time.sleep
        # Fades wait with an injected sleep (tests) or else on their cancel event.
        self._fade_sleep = sleep
        self.background_fades = background_fades
        self._fade_lock = threading.Lock()
        self._fades: dict[str, _ActiveFade] = {}
        # The LAN reply port is exclusive per host, so LAN I/O is serialised (serve is threaded).
        self._lan_lock = threading.RLock()
        self._cache_lock = threading.Lock()
        self._cache: Optional[tuple[float, list[Device]]] = None

    # -- discovery --

    def _discover(self) -> list[Device]:
        lan_error: Optional[Exception] = None
        lan_devices: list[Device] = []
        try:
            with self._lan_lock:
                lan_devices = self.lan.discover()
        except LanError as exc:
            lan_error = exc
        cloud_error: Optional[Exception] = None
        cloud_devices: list[Device] = []
        try:
            cloud_devices = self.cloud.discover()
        except CloudError as exc:
            cloud_error = exc
        devices = _merge(lan_devices, cloud_devices)
        if devices:
            return devices
        if cloud_error is not None and not isinstance(cloud_error, MissingKey):
            raise map_error(cloud_error)
        if lan_error is not None:
            raise map_error(lan_error)
        return []

    def devices(self, *, refresh: bool = False, source: str = "cli", actor: Any = None):
        with self._cache_lock:
            now = self.clock()
            if not refresh and self._cache and now - self._cache[0] < self.cache_ttl:
                return list(self._cache[1])
            devices = self._discover()
            if devices:
                self._cache = (now, devices)
            return list(devices)

    def _device(self, selector: Optional[str]) -> Device:
        return select_device(self.devices(), selector)

    # -- reads --

    def _lan_state(self, device: Device) -> Optional[LampState]:
        if not device.ip:
            return None
        try:
            with self._lan_lock:
                return self.lan.get_state(device)
        except LanError:
            return None

    def state(
        self, device: Optional[str] = None, *, source: str = "cli", actor: Any = None
    ) -> LampStatus:
        dev = self._device(device)
        state = self._lan_state(dev)
        if state is not None:
            return LampStatus(dev, "lan", state)
        if "cloud" in dev.transports:
            try:
                state = self.cloud.get_state(dev)
            except CloudError as exc:
                if not dev.ip:
                    raise map_error(exc) from None
                state = None
            if state is not None:
                return LampStatus(dev, "cloud", state)
        return LampStatus(dev, "lan" if dev.ip else "cloud", None)

    def _cloud_scenes(self, dev: Device) -> list:
        try:
            return list(self.cloud.scenes(dev))
        except CloudError as exc:
            raise map_error(exc) from None

    def scenes(
        self, device: Optional[str] = None, *, source: str = "cli", actor: Any = None
    ) -> list[SceneInfo]:
        dev = self._device(device)
        return [SceneInfo(str(s.value.get("id", s.name)), s.name) for s in self._cloud_scenes(dev)]

    # -- writes --

    def apply(
        self,
        change: Change,
        *,
        apply: bool,
        device: Optional[str],
        source: str,
        actor: Any,
        fade_seconds: Optional[float] = None,
    ) -> ApplyResult:
        dev = self._device(device)
        seconds = fade_mod.resolve_fade_seconds(change, fade_seconds)
        default = fade_mod.default_fade_seconds() if change.on is not None else None
        hint_fade = seconds if (fade_seconds is not None or seconds) else None
        cli = cli_for_change(change, device, hint_fade, default)
        cancelled = self._cancel_device_fade(dev) if apply else None
        warnings: list[str] = []
        if seconds > 0 and dev.ip:
            result = self._fade(dev, change, seconds, cli, apply, source, actor)
        else:
            if seconds > 0 and fade_seconds is not None:
                warnings.append(fade_mod.CLOUD_FADE_WARNING)
            commands = lan_commands(change) if dev.ip else cloud_commands(change)
            result = self._run(
                dev, commands, cli, "light", change.to_dict(), apply, source, actor, change
            )
        result.warnings = warnings
        result.cancelled_fade = cancelled
        return result

    def set_scene(
        self, name: str, *, apply: bool, device: Optional[str], source: str, actor: Any
    ) -> ApplyResult:
        dev = self._device(device)
        scenes = self._cloud_scenes(dev)
        wanted = name.strip().lower()
        match = next((s for s in scenes if s.name.lower() == wanted), None)
        if match is None:
            known = ", ".join(s.name for s in scenes) or "none"
            raise CliError(
                EXIT_USER_ERROR,
                f"unknown scene {name!r} for {dev.name}",
                f"pick one of: {known} (list them with `govee light scenes`)",
            )
        commands = [cloud_mod.scene_command(match)]
        cli = cli_for_scene(match.name, device)
        cancelled = self._cancel_device_fade(dev) if apply else None
        result = self._run(dev, commands, cli, "scene", {"scene": match.name}, apply, source, actor)
        result.cancelled_fade = cancelled
        return result

    # -- fades --

    def active_fade(
        self, device: Optional[str] = None, *, source: str = "cli", actor: Any = None
    ) -> Optional[dict[str, Any]]:
        """The running fade (of ``device``, else of any device) as a dict, or ``None``."""
        with self._fade_lock:
            fades = list(self._fades.values())
        if device is not None:
            dev = self._device(device)
            fades = [f for f in fades if f.device.id == dev.id]
        return fades[0].to_dict() if fades else None

    def cancel_fade(
        self, device: Optional[str] = None, *, source: str = "cli", actor: Any = None
    ) -> bool:
        """Cancel the running fade of ``device`` (``None``: every device); True if one ran."""
        if device is not None:
            return self._cancel_device_fade(self._device(device)) is not None
        with self._fade_lock:
            devices = [f.device for f in self._fades.values()]
        return any([self._cancel_device_fade(d) is not None for d in devices])

    def _cancel_device_fade(self, dev: Device) -> Optional[str]:
        with self._fade_lock:
            active = self._fades.get(dev.id)
        if active is None:
            return None
        active.cancel.set()
        thread = active.thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(FADE_JOIN_TIMEOUT)
        with self._fade_lock:
            if self._fades.get(dev.id) is active:
                del self._fades[dev.id]
        return active.plan.id

    def _fade(
        self,
        dev: Device,
        change: Change,
        seconds: float,
        cli: list[str],
        apply: bool,
        source: str,
        actor: Any,
    ) -> ApplyResult:
        start = self._lan_state(dev)
        remembered = fade_mod.remembered_brightness(dev.id) if change.on else None
        plan = fade_mod.plan_fade(start, change, seconds, remembered=remembered)
        commands = plan.commands()
        payload = {
            "device": {"id": dev.id, "name": dev.name},
            "request": {**change.to_dict(), "fade_seconds": seconds},
            "commands": [
                {"verb": v, "transport": "lan"} for v in dict.fromkeys(c.verb for c in commands)
            ],
            "cli": cli,
            "fade": plan.to_dict(),
            "target": plan.target(),
        }
        if not apply:
            ids = []
            if source != "web":  # approved deviation d7
                ids.append(_record(source, actor, plan.kind, payload, "dry_run"))
            return ApplyResult(False, commands, cli, "dry_run", ids, fade=plan)

        if plan.remember_brightness is not None:
            try:
                fade_mod.remember_brightness(dev.id, plan.remember_brightness)
            except OSError:
                pass  # losing the remembered level only changes the next fade-in's target
        event_id = _record(source, actor, plan.kind, payload, "sent")

        def send(cmd: Command) -> bool:
            with self._lan_lock:
                return bool(self.lan.send(dev, cmd))

        runner = fade_mod.FadeRunner(plan, send, clock=self.clock, sleep=self._fade_sleep)
        active = _ActiveFade(
            plan=plan,
            device=dev,
            runner=runner,
            event_id=event_id,
            payload=payload,
            started=datetime.now(timezone.utc).isoformat(),
        )
        runner.cancel = active.cancel
        with self._fade_lock:
            self._fades[dev.id] = active
        if self.background_fades:
            active.thread = threading.Thread(
                target=self._fade_thread, args=(active,), name=f"fade-{plan.id}", daemon=True
            )
            active.thread.start()
            return ApplyResult(True, commands, cli, "sent", [event_id], fade=plan)
        status = self._execute_fade(active, raise_errors=True)
        return ApplyResult(True, commands, cli, status, [event_id], fade=plan)

    def _fade_thread(self, active: _ActiveFade) -> None:
        try:
            self._execute_fade(active, raise_errors=False)
        except Exception:  # nosec B110 - a background fade must never crash serve
            pass

    def _execute_fade(self, active: _ActiveFade, *, raise_errors: bool) -> str:
        """Run a registered fade to its end; returns the event's final status."""
        try:
            try:
                outcome = active.runner.run()
            except LanError as exc:
                _update_payload(active.event_id, {**active.payload, "error": str(exc)})
                _update(active.event_id, "unconfirmed")
                if raise_errors:
                    raise map_error(exc) from None
                return "unconfirmed"
            if outcome.cancelled:
                self._mark_cancelled(active)
                return "sent"
            if not outcome.ok:
                _update(active.event_id, "unconfirmed")
                return "unconfirmed"
            verify = active.plan.verify
            verified = verify is not None and self._verify(active.device, verify, active.cancel)
            if active.cancel.is_set():
                self._mark_cancelled(active)
                return "sent"
            status = "confirmed" if verified else "unconfirmed"
            _update(active.event_id, status)
            return status
        except KeyboardInterrupt:  # Ctrl-C on a foreground fade: stop sending, keep 'sent'
            active.cancel.set()
            self._mark_cancelled(active)
            raise
        finally:
            with self._fade_lock:
                if self._fades.get(active.device.id) is active:
                    del self._fades[active.device.id]

    def _mark_cancelled(self, active: _ActiveFade) -> None:
        payload = {**active.payload, "cancelled": True, "steps_sent": active.runner.steps_done}
        _update_payload(active.event_id, payload)

    def _run(
        self,
        dev: Device,
        commands: list[Command],
        cli: list[str],
        verb: str,
        request: dict,
        apply: bool,
        source: str,
        actor: Any,
        change: Optional[Change] = None,
    ) -> ApplyResult:
        payload = {
            "device": {"id": dev.id, "name": dev.name},
            "request": request,
            "commands": [{"verb": c.verb, "transport": c.transport} for c in commands],
            "cli": cli,
        }
        if not apply:
            ids = []
            if source != "web":  # approved deviation d7
                ids.append(_record(source, actor, verb, payload, "dry_run"))
            return ApplyResult(False, commands, cli, "dry_run", ids)

        ok = self._send_all(dev, commands, source, actor, verb, payload)
        status = "sent" if ok else "unconfirmed"
        event_id = _record(source, actor, verb, payload, status)
        lan_write = any(c.transport == "lan" for c in commands)
        if ok and lan_write and change is not None:
            status = "confirmed" if self._verify(dev, change) else "unconfirmed"
            _update(event_id, status)
        return ApplyResult(True, commands, cli, status, [event_id])

    def _send_all(
        self,
        dev: Device,
        commands: list[Command],
        source: str,
        actor: Any,
        verb: str,
        payload: dict,
    ) -> bool:
        ok = True
        sent = 0
        for cmd in commands:
            try:
                if cmd.transport == "lan":
                    with self._lan_lock:
                        result = self.lan.send(dev, cmd)
                else:
                    result = self.cloud.send(dev, cmd)
            except (CloudError, LanError) as exc:
                if sent:  # part of the change went out: record it before failing
                    _record(source, actor, verb, {**payload, "error": str(exc)}, "unconfirmed")
                raise map_error(exc) from None
            sent += 1
            ok = ok and bool(result)
        return ok

    def _verify(
        self, dev: Device, change: Change, cancel: Optional[threading.Event] = None
    ) -> bool:
        deadline = self.clock() + self.verify_timeout
        while True:
            if cancel is not None and cancel.is_set():
                return False
            if state_matches(change, self._lan_state(dev)):
                return True
            remaining = deadline - self.clock()
            if remaining <= 0:
                return False
            self.sleep(min(self.poll_interval, remaining))


def _record(source: str, actor: Any, verb: str, payload: dict, status: str) -> int:
    try:
        return store.record_event(source, actor, verb, payload, status)
    except sqlite3.Error as exc:
        raise CliError(
            EXIT_ENV_ERROR,
            f"cannot write the event log at {store.db_path()} ({type(exc).__name__})",
            "make the directory writable or point XDG_STATE_HOME elsewhere",
        ) from None


def _update(event_id: int, status: str) -> None:
    try:
        store.update_event_status(event_id, status)
    except sqlite3.Error as exc:
        raise CliError(
            EXIT_ENV_ERROR,
            f"cannot update the event log at {store.db_path()} ({type(exc).__name__})",
            "make the directory writable or point XDG_STATE_HOME elsewhere",
        ) from None


def _update_payload(event_id: int, payload: dict) -> None:
    try:
        store.update_event_payload(event_id, payload)
    except sqlite3.Error as exc:
        raise CliError(
            EXIT_ENV_ERROR,
            f"cannot update the event log at {store.db_path()} ({type(exc).__name__})",
            "make the directory writable or point XDG_STATE_HOME elsewhere",
        ) from None
