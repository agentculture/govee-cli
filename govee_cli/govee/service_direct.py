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
"""

from __future__ import annotations

import sqlite3
import threading
import time
from typing import Any, Callable, Optional

from govee_cli.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, CliError
from govee_cli.govee import cloud as cloud_mod
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
    ) -> None:
        self.lan = lan if lan is not None else lan_mod.LanTransport()
        self.cloud = cloud if cloud is not None else cloud_mod.CloudTransport()
        self.verify_timeout = verify_timeout
        self.poll_interval = poll_interval
        self.cache_ttl = cache_ttl
        self.clock = clock or time.monotonic
        self.sleep = sleep or time.sleep
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
        self, change: Change, *, apply: bool, device: Optional[str], source: str, actor: Any
    ) -> ApplyResult:
        dev = self._device(device)
        commands = lan_commands(change) if dev.ip else cloud_commands(change)
        cli = cli_for_change(change, device)
        return self._run(
            dev, commands, cli, "light", change.to_dict(), apply, source, actor, change
        )

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
                f"pick one of: {known} (list them with `govee scene list`)",
            )
        commands = [cloud_mod.scene_command(match)]
        cli = cli_for_scene(match.name, device)
        return self._run(dev, commands, cli, "scene", {"scene": match.name}, apply, source, actor)

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

    def _verify(self, dev: Device, change: Change) -> bool:
        deadline = self.clock() + self.verify_timeout
        while True:
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
