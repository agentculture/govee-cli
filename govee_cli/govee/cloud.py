"""Govee cloud transport: the Developer "OpenAPI" router, over stdlib ``urllib``.

Confirmed from the official docs (fetched 2026-10-29):

- https://developer.govee.com/reference/get-you-devices  -- ``GET user/devices``
  (30 req/min per account); response ``data[]`` of ``{sku, device, deviceName, capabilities}``.
- https://developer.govee.com/reference/get-devices-status -- ``POST device/state``
  (30 req/min per device); body ``{requestId, payload: {sku, device}}``; response
  ``payload.capabilities[] = {type, instance, state: {value}}``.
- https://developer.govee.com/reference/control-you-devices -- ``POST device/control``;
  body ``{requestId, payload: {sku, device, capability: {type, instance, value}}}``.
  powerSwitch 1/0, brightness 1-100, colorRgb packed ``r<<16|g<<8|b``, colorTemperatureK
  2000-9000. Auth header ``Govee-API-Key``. Documented codes 400/401/404/429.
- https://developer.govee.com/reference/get-light-scene -- ``POST device/scenes`` returns
  ``dynamic_scene``/``lightScene`` options ``{name, value: {paramId, id}}``; activating one
  is a ``device/control`` with that ``value`` object.

NOT confirmed by the docs: rate-limit response header names (undocumented). We accept
``Retry-After``, ``API-RateLimit-Reset`` and ``X-RateLimit-Reset`` (epoch seconds, delta
seconds or HTTP-date). ``colorTemperatureK`` state readback is undocumented; we parse it
if present and treat 0 as unset.

The API key is read only from ``os.environ["GOVEE_API_KEY"]`` at call time and is never
logged, stored on the object, or included in any error message.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Mapping, Optional

from govee_cli.govee.types import Command, Device, LampState

ENV_KEY = "GOVEE_API_KEY"
DEFAULT_BASE_URL = "https://openapi.api.govee.com/router/api/v1/"
DEFAULT_TIMEOUT = 10.0

PATH_DEVICES = "user/devices"
PATH_STATE = "device/state"
PATH_CONTROL = "device/control"
PATH_SCENES = "device/scenes"

CAP_ON_OFF = "devices.capabilities.on_off"
CAP_RANGE = "devices.capabilities.range"
CAP_COLOR = "devices.capabilities.color_setting"
CAP_SCENE = "devices.capabilities.dynamic_scene"
CAP_ONLINE = "devices.capabilities.online"
INST_POWER = "powerSwitch"
INST_BRIGHTNESS = "brightness"
INST_COLOR_RGB = "colorRgb"
INST_KELVIN = "colorTemperatureK"
INST_SCENE = "lightScene"

_RESET_HEADERS = ("Retry-After", "API-RateLimit-Reset", "X-RateLimit-Reset")
_LOCAL_HOSTS = ("127.0.0.1", "localhost")


class CloudError(Exception):
    """Base for all cloud transport failures."""


class MissingKey(CloudError):
    def __init__(self) -> None:
        super().__init__(f"{ENV_KEY} is not set in the environment")


class Unauthorized(CloudError):
    """HTTP 401/403: the key was rejected."""


class CloudUnavailable(CloudError):
    """Network failure, timeout or 5xx."""


class RateLimited(CloudError):
    def __init__(self, reset_at: Optional[datetime] = None) -> None:
        self.reset_at = reset_at
        when = f"; resets at {reset_at.isoformat()}" if reset_at else ""
        super().__init__(f"Govee cloud rate limit hit (HTTP 429){when}")


@dataclass(frozen=True)
class Scene:
    name: str
    value: dict[str, Any]


def parse_reset(headers: Mapping[str, str]) -> Optional[datetime]:
    """Reset time from rate-limit headers (epoch seconds, delta seconds or HTTP-date)."""
    lowered = {k.lower(): v for k, v in headers.items()}
    now = datetime.now(timezone.utc)
    for name in _RESET_HEADERS:
        raw = lowered.get(name.lower())
        if raw is None:
            continue
        raw = raw.strip()
        try:
            num = float(raw)
        except ValueError:
            try:
                dt = parsedate_to_datetime(raw)
            except (TypeError, ValueError):
                continue
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        if num > 1e9:  # epoch seconds
            return datetime.fromtimestamp(num, tz=timezone.utc)
        return now + timedelta(seconds=num)
    return None


def _cmd(verb: str, type_: str, instance: str, value: Any) -> Command:
    return Command(verb, "cloud", {"type": type_, "instance": instance, "value": value})


def power_command(on: bool) -> Command:
    return _cmd("power", CAP_ON_OFF, INST_POWER, 1 if on else 0)


def brightness_command(level: int) -> Command:
    return _cmd("brightness", CAP_RANGE, INST_BRIGHTNESS, int(level))


def color_command(r: int, g: int, b: int) -> Command:
    return _cmd("color", CAP_COLOR, INST_COLOR_RGB, (r << 16) | (g << 8) | b)


def kelvin_command(kelvin: int) -> Command:
    return _cmd("kelvin", CAP_COLOR, INST_KELVIN, int(kelvin))


def scene_command(scene: Scene) -> Command:
    return _cmd("scene", CAP_SCENE, INST_SCENE, dict(scene.value))


def _check_base_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    ok = parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in _LOCAL_HOSTS)
    if not ok:
        raise ValueError("base_url must be https, or http on 127.0.0.1/localhost")
    return url if url.endswith("/") else url + "/"


class CloudTransport:
    """``Transport`` over the Govee Developer API."""

    def __init__(self, base_url: str = DEFAULT_BASE_URL, timeout: float = DEFAULT_TIMEOUT):
        self.base_url = _check_base_url(base_url)
        self.timeout = timeout
        self._opener = urllib.request.build_opener()

    def __repr__(self) -> str:
        return f"CloudTransport(base_url={self.base_url!r}, timeout={self.timeout!r})"

    # -- HTTP -------------------------------------------------------------
    def _request(self, method: str, path: str, body: Optional[dict] = None) -> dict:
        key = os.environ.get(ENV_KEY, "")
        if not key:
            raise MissingKey()
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(  # noqa: S310 - scheme validated in _check_base_url
            self.base_url + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json", "Govee-API-Key": key},
        )
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            status = e.code
            headers = dict(e.headers.items()) if e.headers else {}
            e.close()
            if status == 429:
                raise RateLimited(parse_reset(headers)) from None
            if status in (401, 403):
                raise Unauthorized(f"Govee API rejected the key (HTTP {status})") from None
            if status >= 500:
                raise CloudUnavailable(f"Govee API error (HTTP {status})") from None
            raise CloudError(f"Govee API request failed (HTTP {status})") from None
        except OSError as e:
            raise CloudUnavailable(f"Govee API unreachable ({type(e).__name__})") from None
        try:
            parsed = json.loads(raw or b"{}")
        except ValueError:
            raise CloudError("Govee API returned invalid JSON") from None
        if not isinstance(parsed, dict):
            raise CloudError("Govee API returned an unexpected response")
        return parsed

    @staticmethod
    def _device_body(device: Device) -> dict:
        return {"sku": device.model, "device": device.id}

    def _post(self, path: str, payload: dict) -> dict:
        return self._request("POST", path, {"requestId": str(uuid.uuid4()), "payload": payload})

    # -- Transport --------------------------------------------------------
    def discover(self) -> list[Device]:
        resp = self._request("GET", PATH_DEVICES)
        out = []
        for item in resp.get("data") or []:
            sku = item.get("sku", "")
            out.append(
                Device(
                    id=item.get("device", ""),
                    name=item.get("deviceName") or sku,
                    model=sku,
                    ip=None,
                    transports=("cloud",),
                )
            )
        return out

    def get_state(self, device: Device) -> Optional[LampState]:
        resp = self._post(PATH_STATE, self._device_body(device))
        caps = (resp.get("payload") or {}).get("capabilities") or []
        vals = {c.get("instance"): (c.get("state") or {}).get("value") for c in caps}
        if not vals or vals.get("online") is False:
            return None
        rgb = vals.get(INST_COLOR_RGB)
        color = None
        if isinstance(rgb, int) and not isinstance(rgb, bool):
            color = ((rgb >> 16) & 255, (rgb >> 8) & 255, rgb & 255)
        k = vals.get(INST_KELVIN)
        kelvin = k if isinstance(k, int) and not isinstance(k, bool) and k > 0 else None
        return LampState(
            on=bool(vals.get(INST_POWER)),
            brightness=_brightness(vals.get(INST_BRIGHTNESS)),
            color=color,
            kelvin=kelvin,
        )

    def send(self, device: Device, command: Command) -> bool:
        if command.transport != "cloud":
            raise CloudError(f"cloud transport cannot send a {command.transport!r} command")
        resp = self._post(
            PATH_CONTROL, {**self._device_body(device), "capability": dict(command.payload)}
        )
        return resp.get("code", 200) == 200

    # -- scenes -----------------------------------------------------------
    def scenes(self, device: Device) -> list[Scene]:
        resp = self._post(PATH_SCENES, self._device_body(device))
        out = []
        for cap in (resp.get("payload") or {}).get("capabilities") or []:
            if cap.get("instance") != INST_SCENE:
                continue
            for opt in (cap.get("parameters") or {}).get("options") or []:
                out.append(Scene(str(opt.get("name", "")), dict(opt.get("value") or {})))
        return out

    def set_scene(self, device: Device, scene: "Scene | str") -> bool:
        if isinstance(scene, str):
            wanted = scene.strip().lower()
            match = next((s for s in self.scenes(device) if s.name.lower() == wanted), None)
            if match is None:
                raise CloudError(f"unknown scene {scene!r} for {device.name}")
            scene = match
        return self.send(device, scene_command(scene))


def _brightness(value: Any) -> int:
    """Brightness from a state reply, clamped to 0-100; odd types read as 0, never raise."""
    if isinstance(value, bool):
        return 0
    try:
        return max(0, min(100, int(float(value))))
    except (TypeError, ValueError):
        return 0
