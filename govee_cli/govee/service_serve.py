"""Serve LampService backend: routes every lamp operation through ``govee serve``'s loopback API.

Approved deviation d4: while serve runs, only serve holds ``GOVEE_API_KEY`` and the LAN reply
port; CLI/MCP clients authenticate with a client token (``GOVEE_TOKEN``, else the 0600 file
``$XDG_CONFIG_HOME/govee-cli/client-token`` written by ``govee token new --save``). Serve logs the
events, so this backend never writes the local event log.

Requests carry ``Authorization: Bearer``, ``Origin: <base URL>`` (serve's same-origin write
check) and ``X-Govee-Source`` / ``X-Govee-Actor``. Proxies are bypassed so the token never leaves
the host. Neither the token nor any key appears in an error message.
"""

from __future__ import annotations

import json
import os
import stat
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional

from govee_cli.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, CliError
from govee_cli.govee.fade import FadePlan
from govee_cli.govee.service_types import (
    REMEDIATION_TOKEN,
    ApplyResult,
    Change,
    LampStatus,
    SceneInfo,
    parse_hex_color,
)
from govee_cli.govee.types import Command, Device, LampState

ENV_SERVE_URL = "GOVEE_SERVE_URL"
ENV_TOKEN = "GOVEE_TOKEN"  # nosec B105 - an env var name, not a secret
DEFAULT_SERVE_PORT = 8742
DEFAULT_SERVE_URL = f"http://127.0.0.1:{DEFAULT_SERVE_PORT}"
HEALTH_TIMEOUT = 0.3
REQUEST_TIMEOUT = 30.0  # serve may verify a LAN write for up to 3 s, or wait on the cloud
SERVICE_NAME = "govee-serve"
_LOOPBACK = ("127.0.0.1", "localhost", "::1")
_USER_ERRORS = ("bad_request", "too_large")


def serve_base_url() -> str:
    """``GOVEE_SERVE_URL`` (no trailing slash) or ``http://127.0.0.1:8742``.

    Raises ``CliError(2)`` unless it is https, or http on a loopback host (the client token must
    not travel in clear text over a network).
    """
    url = (os.environ.get(ENV_SERVE_URL) or DEFAULT_SERVE_URL).strip().rstrip("/")
    parts = urllib.parse.urlsplit(url)
    ok = parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in _LOOPBACK)
    if not ok or not parts.hostname or parts.path:
        raise CliError(
            EXIT_ENV_ERROR,
            f"{ENV_SERVE_URL} must be http://127.0.0.1:<port> (or an https URL), got {url!r}",
            f"unset {ENV_SERVE_URL} to use {DEFAULT_SERVE_URL}",
        )
    return url


def client_token_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "govee-cli" / "client-token"


def client_token() -> Optional[str]:
    """The client token from ``GOVEE_TOKEN`` or the client-token file; ``None`` if neither.

    A token file readable or writable by group/other is refused with ``CliError(2)``.
    """
    env = os.environ.get(ENV_TOKEN, "").strip()
    if env:
        return env
    path = client_token_path()
    try:
        mode = path.stat().st_mode
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise CliError(
            EXIT_ENV_ERROR, f"cannot read {path} ({type(exc).__name__})", REMEDIATION_TOKEN
        ) from None
    if stat.S_IMODE(mode) & 0o077:
        raise CliError(
            EXIT_ENV_ERROR,
            f"refusing to use client token file {path}: its mode "
            f"{oct(stat.S_IMODE(mode))} is wider than 0600",
            f"chmod 600 {path}",
        )
    try:
        content = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise CliError(
            EXIT_ENV_ERROR, f"cannot read {path} ({type(exc).__name__})", REMEDIATION_TOKEN
        ) from None
    return content or None


def _opener() -> urllib.request.OpenerDirector:
    # An empty ProxyHandler disables http(s)_proxy: loopback traffic must never go to a proxy.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def serve_running(base_url: str, timeout: float = HEALTH_TIMEOUT) -> bool:
    """True when ``GET {base_url}/api/health`` answers as govee serve within ``timeout``."""
    req = urllib.request.Request(base_url + "/api/health", method="GET")  # noqa: S310
    try:
        with _opener().open(req, timeout=timeout) as resp:  # nosec B310 - scheme checked
            body = json.loads(resp.read() or b"{}")
    except (OSError, ValueError):
        return False
    return isinstance(body, dict) and body.get("service") == SERVICE_NAME and bool(body.get("ok"))


def _command(item: dict) -> Command:
    return Command(
        str(item.get("verb", "")), str(item.get("transport", "")), item.get("payload") or {}
    )


def _apply_result(body: dict) -> ApplyResult:
    fade = body.get("fade")
    cancelled = body.get("cancelled_fade")
    return ApplyResult(
        applied=bool(body.get("applied")),
        commands=[_command(c) for c in body.get("commands") or []],
        cli=[str(c) for c in body.get("cli") or []],
        status=str(body.get("status", "")),
        event_ids=[],
        fade=FadePlan.from_dict(fade) if isinstance(fade, dict) else None,
        warnings=[str(w) for w in body.get("warnings") or []],
        cancelled_fade=str(cancelled) if cancelled is not None else None,
    )


def _device(item: dict) -> Device:
    return Device(
        id=str(item.get("id", "")),
        name=str(item.get("name", "")),
        model=str(item.get("model", "")),
        ip=item.get("ip"),
        transports=tuple(item.get("transports") or ()),
    )


class ServeBackend:
    routed = True

    def __init__(self, base_url: str, token: str, timeout: float = REQUEST_TIMEOUT) -> None:
        self.base_url = base_url
        self._token = token
        self.timeout = timeout
        self._opener = _opener()

    def __repr__(self) -> str:  # never show the token
        return f"ServeBackend(base_url={self.base_url!r})"

    def _request(
        self,
        method: str,
        path: str,
        *,
        source: str,
        actor: Any,
        query: Optional[dict] = None,
        body: Optional[dict] = None,
    ) -> dict:
        url = self.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Origin": self.base_url,
            "X-Govee-Source": source,
            "Accept": "application/json",
        }
        if actor is not None:
            headers["X-Govee-Actor"] = str(actor)
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)  # noqa: S310
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:  # nosec B310
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            exc.close()
            raise self._http_error(exc.code, raw) from None
        except OSError as exc:
            raise CliError(
                EXIT_ENV_ERROR,
                f"govee serve at {self.base_url} stopped responding ({type(exc).__name__})",
                "restart `govee serve`, or stop it to control the lamp directly",
            ) from None
        try:
            parsed = json.loads(raw or b"{}")
        except ValueError:
            parsed = None
        if not isinstance(parsed, dict):
            raise CliError(
                EXIT_ENV_ERROR,
                f"govee serve at {self.base_url} returned an unexpected response",
                "upgrade or restart `govee serve`",
            )
        return parsed

    def _http_error(self, status: int, raw: bytes) -> CliError:
        try:
            err = json.loads(raw or b"{}").get("error") or {}
        except (ValueError, AttributeError):
            err = {}
        if not isinstance(err, dict):
            err = {}
        code = str(err.get("code", ""))
        message = str(err.get("message") or f"govee serve returned HTTP {status}")
        remediation = str(err.get("remediation") or "")
        if code == "unauthorized" or status == 401:
            remediation = remediation or f"create a client token: {REMEDIATION_TOKEN}"
            return CliError(
                EXIT_ENV_ERROR, f"govee serve rejected the token: {message}", remediation
            )
        if code in _USER_ERRORS or status in (400, 413):
            return CliError(EXIT_USER_ERROR, message, remediation or "check the arguments")
        return CliError(EXIT_ENV_ERROR, message, remediation or "check `govee serve` logs")

    # -- LampService backend interface --

    def devices(self, *, refresh: bool = False, source: str = "cli", actor: Any = None):
        body = self._request("GET", "/api/devices", source=source, actor=actor)
        return [_device(d) for d in body.get("devices") or []]

    def state(self, device: Optional[str] = None, *, source: str = "cli", actor: Any = None):
        body = self._request(
            "GET", "/api/state", source=source, actor=actor, query={"device": device}
        )
        dev = _device(body.get("device") or {})
        transport = (body.get("device") or {}).get("transport")
        state = None
        if body.get("reachable"):
            color = body.get("color")
            state = LampState(
                on=bool(body.get("on")),
                brightness=int(body.get("brightness") or 0),
                color=parse_hex_color(color) if isinstance(color, str) else None,
                kelvin=body.get("kelvin"),
                scene=body.get("scene"),
            )
        return LampStatus(dev, transport, state)

    def scenes(self, device: Optional[str] = None, *, source: str = "cli", actor: Any = None):
        body = self._request(
            "GET", "/api/scenes", source=source, actor=actor, query={"device": device}
        )
        return [
            SceneInfo(str(s.get("id", "")), str(s.get("name", "")))
            for s in body.get("scenes") or []
        ]

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
        req: dict[str, Any] = {**change.to_dict(), "apply": bool(apply)}
        if device is not None:
            req["device"] = device
        if fade_seconds is not None:  # omitted: serve applies its own GOVEE_FADE_DEFAULT
            req["fade_seconds"] = float(fade_seconds)
        body = self._request("POST", "/api/light", source=source, actor=actor, body=req)
        return _apply_result(body)

    def active_fade(
        self, device: Optional[str] = None, *, source: str = "cli", actor: Any = None
    ) -> Optional[dict]:
        body = self._request(
            "GET", "/api/fade", source=source, actor=actor, query={"device": device}
        )
        active = body.get("active")
        return active if isinstance(active, dict) else None

    def cancel_fade(
        self, device: Optional[str] = None, *, source: str = "cli", actor: Any = None
    ) -> bool:
        req: dict[str, Any] = {} if device is None else {"device": device}
        body = self._request("POST", "/api/fade/cancel", source=source, actor=actor, body=req)
        return bool(body.get("cancelled"))

    def set_scene(
        self, name: str, *, apply: bool, device: Optional[str], source: str, actor: Any
    ) -> ApplyResult:
        req: dict[str, Any] = {"scene": name, "apply": bool(apply)}
        if device is not None:
            req["device"] = device
        body = self._request("POST", "/api/scene", source=source, actor=actor, body=req)
        return _apply_result(body)
