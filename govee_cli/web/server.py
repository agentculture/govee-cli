"""``govee serve``: the token-gated HTTP API and the static web console (stdlib only).

While it runs, serve is the single owner of the lamp: it holds the LAN reply port and the cloud
key, and CLI/MCP clients route every lamp operation through its public routes (approved
deviation d3). The security rules, all enforced here:

* Every ``/api`` route needs ``Authorization: Bearer <token>`` (401 otherwise), reads included.
  Tokens are checked only through :func:`govee_cli.auth.tokens.verify` (constant-time compare
  against stored hashes). ``GET /api/health`` is the one exception and returns nothing but
  ``{"ok": true, "service": "govee-serve"}``.
* Writes (POST) also need an ``Origin`` equal to ``http://<Host>`` of the request (403).
* Bodies over 16 KiB are refused with 413 before they are read; a body must be a JSON object.
* ``X-Govee-Source`` / ``X-Govee-Actor`` are honoured only from loopback clients (the local CLI
  and MCP server); otherwise source is ``web`` and actor is the token's name.
* The access log has the method, a known route (or ``-``) and the status: never the query
  string, an unknown path or any header, so a token can not reach it.
* Static files come from a fixed whitelist built from ``static/`` at start (no path traversal).

``run()`` is what ``govee serve`` calls; ``make_server()`` builds the server for tests.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import signal
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, TextIO, Union
from urllib.parse import parse_qs, urlsplit

from govee_cli.auth import tokens
from govee_cli.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, CliError
from govee_cli.govee import lan
from govee_cli.govee.cloud import MissingKey, RateLimited
from govee_cli.govee.service import Change, LampService, hex_color, parse_hex_color
from govee_cli.stats import store
from govee_cli.stats.store import LocalTimezone
from govee_cli.web.poller import Poller

DEFAULT_HOST = "0.0.0.0"  # nosec B104
DEFAULT_PORT = tokens.DEFAULT_SERVE_PORT
MAX_BODY = 16 * 1024
SERVICE_NAME = "govee-serve"
STATIC_DIR = Path(__file__).resolve().parent / "static"
REQUEST_TIMEOUT = 30.0  # seconds a client may take to send its request
BODY_TIMEOUT = 5.0  # seconds to receive a (<= 16 KiB) body; it is read before auth
MAX_EVENTS = 200
_MAX_FIELD = 128
_USE_GET = "use GET"  # remediation for a 405

log = logging.getLogger("govee_cli.web")

CSP = (
    "default-src 'self'; script-src 'self'; "
    # Inline style attributes are written by the console's charts; scripts stay self-only.
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; "
    "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
)
_SECURITY_HEADERS = (
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("X-Frame-Options", "DENY"),
    ("Content-Security-Policy", CSP),
)
_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".txt": "text/plain; charset=utf-8",
}
_METHOD_RE = re.compile(r"[A-Z]{1,16}")


# --- errors ----------------------------------------------------------------------------------


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, remediation: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.remediation = remediation

    def body(self) -> dict[str, Any]:
        return {
            "error": {"code": self.code, "message": self.message, "remediation": self.remediation}
        }


def _bad(message: str, remediation: str = "check the request body and query") -> ApiError:
    return ApiError(400, "bad_request", message, remediation)


def _caused_by(exc: BaseException, kind: type) -> bool:
    seen: Optional[BaseException] = exc
    depth = 0
    while seen is not None and depth < 10:
        if isinstance(seen, kind):
            return True
        seen = seen.__cause__ or seen.__context__
        depth += 1
    return False


def map_exception(exc: BaseException) -> ApiError:
    """Map a service failure to the HTTP error contract; never a traceback or a secret."""
    if isinstance(exc, ApiError):
        return exc
    if isinstance(exc, CliError):
        if _caused_by(exc, RateLimited) or "rate limit" in exc.message.lower():
            return ApiError(429, "rate_limited", exc.message, exc.remediation)
        if exc.code == EXIT_USER_ERROR:
            return ApiError(400, "bad_request", exc.message, exc.remediation)
        if _caused_by(exc, MissingKey) or "GOVEE_API_KEY" in exc.message:
            return ApiError(503, "unreachable", exc.message, exc.remediation)
        return ApiError(502, "unreachable", exc.message, exc.remediation)
    if isinstance(exc, ValueError):
        return _bad(str(exc))
    return ApiError(
        500, "internal", "internal server error", "check the `govee serve` log and retry"
    )


# --- helpers ---------------------------------------------------------------------------------


Network = Union[ipaddress.IPv4Network, ipaddress.IPv6Network]
ENV_TRUST = "GOVEE_TRUSTED_NETWORKS"  # comma-separated --trust values (for the systemd unit)
TRUST_TAILSCALE = ("100.64.0.0/10", "fd7a:115c:a1e0::/48")  # NOSONAR - Tailscale's ranges


def parse_trusted(specs: Iterable[str]) -> list[Network]:
    """``--trust`` values as networks: CIDRs, ``lan`` (the default route's /24) or ``tailscale``.

    Raises ``CliError(1)`` on a value that is none of those.
    """
    nets: list[Network] = []
    for raw in specs:
        for spec in (p.strip().lower() for p in str(raw).split(",")):
            if spec:
                nets.extend(_parse_trust_spec(spec))
    return nets


def _parse_trust_spec(spec: str) -> list[Network]:
    """One non-empty, lower-cased ``--trust`` value as its network(s)."""
    if spec == "tailscale":
        return [ipaddress.ip_network(n) for n in TRUST_TAILSCALE]
    if spec == "lan":
        try:
            spec = f"{lan.default_route_ip()}/24"
        except (OSError, lan.LanError) as exc:
            raise CliError(
                EXIT_ENV_ERROR,
                f"cannot find the LAN to trust ({exc})",
                "pass the network as a CIDR instead, e.g. --trust 192.168.1.0/24",
            ) from None
    try:
        return [ipaddress.ip_network(spec, strict=False)]
    except ValueError:
        raise CliError(
            EXIT_USER_ERROR,
            f"--trust {spec!r} is not a network",
            "use a CIDR such as 192.168.1.0/24, or the words lan or tailscale",
        ) from None


def in_networks(address: str, nets: Iterable[Network]) -> bool:
    """True when ``address`` (IPv4-mapped included) is inside one of ``nets``."""
    try:
        ip = ipaddress.ip_address(str(address).split("%", 1)[0])
    except ValueError:
        return False
    ip = getattr(ip, "ipv4_mapped", None) or ip
    return any(ip.version == n.version and ip in n for n in nets)


def is_loopback(address: str) -> bool:
    """True for 127.0.0.0/8, ::1 and IPv4-mapped loopback; False for anything unparsable."""
    try:
        ip = ipaddress.ip_address(str(address).split("%", 1)[0])
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)
    return bool((mapped or ip).is_loopback)


def _summary(verb: str, payload: Any) -> str:
    """One line for the activity feed, from an event's logged request."""
    req = payload.get("request") if isinstance(payload, dict) else None
    if not isinstance(req, dict):
        return verb
    parts = []
    if req.get("scene"):
        parts.append(f"scene {req['scene']}")
    if req.get("on") is True:
        parts.append("on")
    if isinstance(req.get("brightness"), int):
        parts.append(f"brightness {req['brightness']}%")
    if isinstance(req.get("color"), str):
        parts.append(f"color {req['color']}")
    if isinstance(req.get("kelvin"), int):
        parts.append(f"{req['kelvin']} K")
    if req.get("on") is False:
        parts.append("off")
    fade = req.get("fade_seconds")
    if isinstance(fade, (int, float)) and fade > 0:
        parts.append(f"fade {fade:g} s")
    if not parts:
        return verb
    text = ", ".join(parts)
    return text[0].upper() + text[1:]


def _rgb_hex(value: Any) -> Optional[str]:
    if isinstance(value, (list, tuple)) and len(value) == 3:
        try:
            return hex_color(tuple(int(c) for c in value))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
    return None


def _check_keys(body: dict, allowed: set[str]) -> None:
    extra = sorted(set(body) - allowed)
    if extra:
        raise _bad(
            f"unknown field(s): {', '.join(extra)[:200]}",
            f"allowed fields: {', '.join(sorted(allowed))}",
        )


def _opt_bool(body: dict, key: str) -> Optional[bool]:
    value = body.get(key)
    if value is not None and not isinstance(value, bool):
        raise _bad(f"'{key}' must be true or false")
    return value


def _opt_int(body: dict, key: str) -> Optional[int]:
    value = body.get(key)
    if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
        raise _bad(f"'{key}' must be an integer")
    return value


def _opt_str(body: dict, key: str) -> Optional[str]:
    value = body.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > _MAX_FIELD:
        raise _bad(f"'{key}' must be a non-empty string of at most {_MAX_FIELD} characters")
    return value


# --- route handlers --------------------------------------------------------------------------

Ctx = dict[str, Any]


def _query_device(query: dict[str, list[str]]) -> Optional[str]:
    return _opt_str({"device": query["device"][0]}, "device") if "device" in query else None


def _get_state(service: LampService, ctx: Ctx, query: dict, body: dict) -> dict:
    return service.state(_query_device(query), **ctx).to_dict()


def _get_devices(service: LampService, ctx: Ctx, query: dict, body: dict) -> dict:
    refresh = query.get("refresh", ["0"])[0].lower() in ("1", "true", "yes")
    return {
        "devices": [
            {
                "id": d.id,
                "name": d.name,
                "model": d.model,
                "ip": d.ip,
                "transports": list(d.transports),
            }
            for d in service.devices(refresh=refresh, **ctx)
        ]
    }


def _get_scenes(service: LampService, ctx: Ctx, query: dict, body: dict) -> dict:
    return {"scenes": [s.to_dict() for s in service.scenes(_query_device(query), **ctx)]}


def _get_fade(service: LampService, ctx: Ctx, query: dict, body: dict) -> dict:
    return {"active": service.active_fade(_query_device(query), **ctx)}


def _get_events(service: LampService, ctx: Ctx, query: dict, body: dict) -> dict:
    raw = query.get("limit", ["20"])[0]
    try:
        limit = int(raw)
    except ValueError:
        raise _bad("'limit' must be an integer") from None
    if limit < 1:
        raise _bad("'limit' must be at least 1")
    events = store.recent_events(limit=min(limit, MAX_EVENTS))
    return {
        "events": [
            {
                "id": e["id"],
                "ts": e["ts"],
                "source": e["source"],
                "actor": e["actor"],
                "verb": e["verb"],
                "summary": _summary(e["verb"], e["payload"]),
                "status": e["status"],
            }
            for e in events
        ]
    }


def _get_stats(service: LampService, ctx: Ctx, query: dict, body: dict) -> dict:
    tz = LocalTimezone()
    now = datetime.now(timezone.utc)
    avg = store.avg_brightness_today(now=now, tz=tz)
    samples = []
    for s in store.day_samples(hours=24, now=now, tz=tz):
        if not s.get("gap"):
            s = {**s, "rgb": _rgb_hex(s.get("rgb"))}
        samples.append(s)
    return {
        "tiles": {
            "on_today_h": store.on_today_h(now=now, tz=tz),
            "on_week_h": store.on_week_h(now=now, tz=tz),
            "commands_7d": store.commands_7d(now=now, tz=tz),
            "agent_commands_7d": store.agent_commands_7d(now=now, tz=tz),
            "avg_brightness": round(avg) if avg is not None else None,
        },
        "hours_per_day": store.hours_per_day(days=14, now=now, tz=tz),
        "commands_by_source": store.commands_by_source(days=7, now=now, tz=tz),
        "day_samples": samples,
        "heatmap": store.heatmap(days=28, now=now, tz=tz),
    }


_LIGHT_KEYS = {"on", "brightness", "color", "kelvin", "apply", "device", "fade_seconds"}


def _post_light(service: LampService, ctx: Ctx, query: dict, body: dict) -> dict:
    _check_keys(body, _LIGHT_KEYS)
    color = body.get("color")
    if color is not None and not isinstance(color, str):
        raise _bad("'color' must be a string like #FF6A2B")
    fade = body.get("fade_seconds")
    if fade is not None and (isinstance(fade, bool) or not isinstance(fade, (int, float))):
        raise _bad("'fade_seconds' must be a number of seconds 0-3600")
    change = Change(
        on=_opt_bool(body, "on"),
        brightness=_opt_int(body, "brightness"),
        color=parse_hex_color(color) if color is not None else None,
        kelvin=_opt_int(body, "kelvin"),
    )
    result = service.apply(
        change,
        apply=bool(_opt_bool(body, "apply")),
        device=_opt_str(body, "device"),
        fade_seconds=fade,
        **ctx,
    )
    return result.to_dict()


def _post_scene(service: LampService, ctx: Ctx, query: dict, body: dict) -> dict:
    _check_keys(body, {"scene", "apply", "device"})
    wanted = _opt_str(body, "scene")
    if wanted is None:
        raise _bad("'scene' is required", "pick a scene id or name from GET /api/scenes")
    apply = bool(_opt_bool(body, "apply"))
    device = _opt_str(body, "device")
    # Approved deviation d9: the console sends the id from /api/scenes; match id, then name.
    known = service.scenes(device, **ctx)
    match = next((s for s in known if s.id == wanted), None) or next(
        (s for s in known if s.name.casefold() == wanted.strip().casefold()), None
    )
    name = match.name if match is not None else wanted
    return service.set_scene(name, apply=apply, device=device, **ctx).to_dict()


def _post_fade_cancel(service: LampService, ctx: Ctx, query: dict, body: dict) -> dict:
    _check_keys(body, {"device"})
    return {"cancelled": bool(service.cancel_fade(_opt_str(body, "device"), **ctx))}


Handler = Callable[[LampService, Ctx, dict, dict], dict]
ROUTES: dict[str, dict[str, Handler]] = {
    "/api/state": {"GET": _get_state},
    "/api/devices": {"GET": _get_devices},
    "/api/scenes": {"GET": _get_scenes},
    "/api/events": {"GET": _get_events},
    "/api/stats": {"GET": _get_stats},
    "/api/fade": {"GET": _get_fade},
    "/api/light": {"POST": _post_light},
    "/api/scene": {"POST": _post_scene},
    "/api/fade/cancel": {"POST": _post_fade_cancel},
}
HEALTH_PATH = "/api/health"


# --- HTTP plumbing ---------------------------------------------------------------------------


class GoveeHandler(BaseHTTPRequestHandler):
    server_version = SERVICE_NAME
    sys_version = ""
    timeout = REQUEST_TIMEOUT
    server: "GoveeServer"
    _log_path = "-"  # a known route or static path; never the raw request path

    # -- entry points --

    def do_GET(self) -> None:  # noqa: N802 - http.server naming
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def _handle(self, method: str) -> None:
        self._log_path = "-"
        self._raw_body: Optional[bytes] = None
        self._body_error: Optional[ApiError] = None
        try:
            if method == "POST":
                self._read_raw_body()
            path = urlsplit(self.path).path
            if path == "/api" or path.startswith("/api/"):
                self._api(method, path, urlsplit(self.path).query)
            elif method == "GET":
                self._static(path)
            else:
                raise ApiError(405, "method_not_allowed", "only GET is allowed here", _USE_GET)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as exc:  # noqa: BLE001 - nothing may leak a traceback
            err = map_exception(exc)
            if err.status == 500:
                log.warning("internal error on %s (%s)", self._log_path, type(exc).__name__)
            try:
                self._send_error(err)
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True

    # -- request body --

    def _read_raw_body(self) -> None:
        """Read a POST body of at most ``MAX_BODY`` bytes; larger bodies are never read.

        Errors are recorded and raised only after authentication, so an unauthenticated
        client learns nothing beyond 401.
        """
        if self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            self._body_error = _bad(
                "chunked request bodies are not supported", "send a Content-Length header"
            )
            return
        raw_len = self.headers.get("Content-Length")
        if raw_len is None:
            self._raw_body = b""
            return
        try:
            length = int(raw_len)
        except ValueError:
            length = -1
        if length < 0:
            self.close_connection = True
            self._body_error = _bad("invalid Content-Length")
            return
        if length > MAX_BODY:
            self.close_connection = True
            self._body_error = ApiError(
                413, "too_large", f"request body is over {MAX_BODY} bytes", "send a smaller body"
            )
            return
        # The body is read before authentication (so an early 401 never resets the connection),
        # so an unauthenticated client must not hold this thread for the full REQUEST_TIMEOUT.
        self.connection.settimeout(BODY_TIMEOUT)
        try:
            data = self.rfile.read(length) if length else b""
        except OSError:  # TimeoutError included
            data = b""
        finally:
            self.connection.settimeout(self.timeout)
        if len(data) != length:
            self.close_connection = True
            self._body_error = _bad("request body is shorter than its Content-Length")
            return
        self._raw_body = data

    def _json_body(self) -> dict:
        if self._body_error is not None:
            raise self._body_error
        raw = self._raw_body or b""
        if not raw.strip():
            return {}
        try:
            body = json.loads(raw.decode("utf-8"))
        except (ValueError, RecursionError):  # UnicodeDecodeError is a ValueError
            raise _bad("request body is not valid JSON") from None
        if not isinstance(body, dict):
            raise _bad("request body must be a JSON object")
        return body

    # -- API --

    def _client_is_loopback(self) -> bool:
        return is_loopback(self.client_address[0])

    def _client_is_trusted(self) -> bool:
        return in_networks(self.client_address[0], self.server.trusted)

    def _authenticate(self) -> str:
        scheme, _, secret = (self.headers.get("Authorization") or "").partition(" ")
        secret = secret.strip()
        name = None
        if not secret and self._client_is_trusted():
            # `govee serve --trust`: a device on a network the owner trusts needs no token.
            return f"trusted {self.client_address[0]}"[:64]
        if scheme.lower() == "bearer" and secret:
            try:
                name = tokens.verify(secret)
            except tokens.TokenStoreError:
                log.error("token store is unreadable; refusing every request")
                raise ApiError(
                    500,
                    "internal",
                    "the token store is unreadable",
                    "fix or remove tokens.json on the serve host, then pair again",
                ) from None
        if not name:
            raise ApiError(
                401,
                "unauthorized",
                "a valid pairing token is required",
                "pair this client: `govee token new --name <label>` on the serve host",
            )
        return name

    def _check_origin(self) -> None:
        origin = (self.headers.get("Origin") or "").strip().lower()
        host = (self.headers.get("Host") or "").strip().lower()
        # The console is plain HTTP on the LAN/Tailscale by design (d20); the Origin must match.
        if not origin or not host or origin != f"http://{host}":  # NOSONAR
            raise ApiError(
                403,
                "forbidden",
                "cross-origin write refused",
                "send writes from the console page itself (Origin must match the server)",
            )

    def _attribution(self, token_name: str) -> Ctx:
        source, actor = "web", token_name
        if self._client_is_loopback():
            raw_source = self.headers.get("X-Govee-Source")
            if raw_source is not None:
                raw_source = raw_source.strip()
                if raw_source not in store.SOURCES:
                    raise _bad(f"X-Govee-Source must be one of {', '.join(store.SOURCES)}")
                source = raw_source
            raw_actor = self.headers.get("X-Govee-Actor")
            if raw_actor is not None:
                clean = "".join(c for c in raw_actor if c.isprintable()).strip()[:64]
                actor = clean or token_name
        return {"source": source, "actor": actor}

    def _api(self, method: str, path: str, query_string: str) -> None:
        if path == HEALTH_PATH:
            self._log_path = HEALTH_PATH
            if method != "GET":
                raise ApiError(405, "method_not_allowed", _USE_GET, _USE_GET)
            self._send_json(200, {"ok": True, "service": SERVICE_NAME})
            return
        token_name = self._authenticate()
        methods = ROUTES.get(path)
        if methods is None:
            raise ApiError(404, "not_found", "no such API route", "see the HTTP contract")
        self._log_path = path
        handler = methods.get(method)
        if handler is None:
            allowed = ", ".join(sorted(methods))
            raise ApiError(405, "method_not_allowed", f"use {allowed}", f"use {allowed}")
        body: dict = {}
        if method == "POST":
            self._check_origin()
            body = self._json_body()
        ctx = self._attribution(token_name)
        query = parse_qs(query_string, keep_blank_values=True, max_num_fields=20)
        self._send_json(200, handler(self.server.service, ctx, query, body))

    # -- static console --

    def _static(self, path: str) -> None:
        file = self.server.static_files.get(path)
        if file is None:
            raise ApiError(404, "not_found", "not found", "open / for the console")
        self._log_path = path
        data = file.read_bytes()
        ctype = _CONTENT_TYPES.get(file.suffix, "application/octet-stream")
        cache = "no-store" if file.suffix == ".html" else "no-cache"
        self._send(200, data, ctype, cache)

    # -- responses --

    def _send(
        self, status: int, data: bytes, ctype: str, cache: str = "no-store", extra=()
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        for key, value in _SECURITY_HEADERS:
            self.send_header(key, value)
        for key, value in extra:
            self.send_header(key, value)
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _send_json(self, status: int, obj: Any, extra=()) -> None:
        self._send(status, json.dumps(obj).encode("utf-8"), "application/json", extra=extra)

    def _send_error(self, err: ApiError) -> None:
        extra = []
        if err.status == 401:
            extra.append(("WWW-Authenticate", 'Bearer realm="govee-serve"'))
        if err.status == 405:
            extra.append(("Allow", "GET, POST"))
        self._send_json(err.status, err.body(), extra=extra)

    def send_error(self, code: int, message: Optional[str] = None, explain=None) -> None:
        """http.server's own errors (bad request line, unsupported method): JSON, no echo."""
        self.close_connection = True
        names = {400: "bad_request", 404: "not_found", 414: "too_large", 431: "too_large"}
        err = ApiError(
            code,
            names.get(code, "bad_request" if code < 500 else "internal"),
            f"HTTP {code}",
            "send a well-formed GET or POST request",
        )
        if code == 501:
            err = ApiError(405, "method_not_allowed", "only GET and POST are supported", "")
        try:
            self._send_error(err)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # -- logging: never the query, an unknown path or a header --

    def log_request(self, code: Any = "-", size: Any = "-") -> None:
        command = getattr(self, "command", None) or "-"
        if not _METHOD_RE.fullmatch(command):
            command = "-"
        status = getattr(code, "value", code)
        log.info('%s "%s %s" %s', self.client_address[0], command, self._log_path, status)

    def log_error(self, format: str, *args: Any) -> None:  # noqa: A002 - http.server signature
        log.debug("%s request error", self.client_address[0])

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        log.debug("%s http.server message", self.client_address[0])


class GoveeServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        service: LampService,
        static_dir: Path = STATIC_DIR,
        trusted: Iterable[Network] = (),
    ) -> None:
        self.trusted = list(trusted)
        if ":" in address[0]:
            import socket

            self.address_family = socket.AF_INET6
        self.service = service
        self.static_files = _static_whitelist(static_dir)
        super().__init__(address, GoveeHandler)


def _static_whitelist(static_dir: Path) -> dict[str, Path]:
    """URL path -> file, for the regular, non-hidden files directly in ``static_dir``."""
    files: dict[str, Path] = {}
    if static_dir.is_dir():
        for p in sorted(static_dir.iterdir()):
            if p.is_file() and not p.is_symlink() and not p.name.startswith("."):
                files["/" + p.name] = p
    if "/index.html" in files:
        files["/"] = files["/index.html"]
    return files


def make_server(
    host: str,
    port: int,
    service: LampService,
    *,
    static_dir: Path = STATIC_DIR,
    trusted: Iterable[Network] = (),
) -> GoveeServer:
    """Bind (but do not start) the server; ``port=0`` picks a free port (tests)."""
    return GoveeServer((host, port), service, static_dir, trusted)


# --- run / rotate ----------------------------------------------------------------------------


def _pairing_host(host: str) -> str:
    if host not in ("", "::", DEFAULT_HOST):
        return host
    try:
        return lan.default_route_ip()
    except (lan.LanError, OSError):
        return "127.0.0.1"


def rotate_tokens(host: str, port: int, out: Optional[TextIO] = None) -> str:
    """Revoke every token, mint ``console-<date>`` and print its pairing link once to ``out``.

    The secret rides only in the link's #fragment and is never logged.
    """
    out = out if out is not None else sys.stdout
    revoked = tokens.revoke_all()
    name = "console-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    secret = tokens.new(name)
    link = tokens.pairing_link(secret, _pairing_host(host), port)
    log.info("revoked %d token(s); minted %s", revoked, name)
    print(f"Pairing link for {name} (shown once): {link}", file=out)
    out.flush()
    return link


def _build_service() -> LampService:
    return LampService.direct(source="web", background_fades=True)


def _configure_logging() -> None:
    if not log.handlers and not logging.getLogger().handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        log.addHandler(handler)
        log.setLevel(logging.INFO)


def run(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    rotate_token: bool = False,
    trust: Iterable[str] = (),
) -> int:
    """Serve the console and API until interrupted; ``govee serve`` calls this.

    ``trust`` (plus ``GOVEE_TRUSTED_NETWORKS``) names networks whose devices need no token.
    """
    trusted = parse_trusted([*trust, os.environ.get(ENV_TRUST, "")])
    _configure_logging()
    service = _build_service()
    try:
        httpd = make_server(host, port, service, trusted=trusted)
    except OSError as exc:
        raise CliError(
            EXIT_ENV_ERROR,
            f"cannot listen on {host}:{port} ({exc.strerror or type(exc).__name__})",
            "stop the other `govee serve` (or whatever holds the port), or pick another --port",
        ) from None
    actual_port = httpd.server_address[1]
    poller = None
    # systemd stops the unit with SIGTERM: take the same clean path as Ctrl-C (stop the poller,
    # cancel fades so no event is left mid-ramp) instead of dying in the default handler.
    previous_term = _install_term_handler()
    try:
        if rotate_token:
            rotate_tokens(host, actual_port)
        poller = Poller(service)
        poller.start()
        log.info("govee serve listening on %s:%d (Ctrl-C to stop)", host, actual_port)
        if trusted:
            log.info("no token needed from: %s", ", ".join(str(n) for n in trusted))
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        # A second signal (a repeated stop, or `uv run` forwarding the same SIGTERM) must not
        # interrupt the cleanup itself; found live when one did, skipping the fade cancel.
        previous_int = _ignore_signals_during_shutdown()
        if poller is not None:
            poller.stop()
        try:
            service.cancel_fade()
        except Exception:  # noqa: BLE001 - shutting down regardless
            log.debug("could not cancel running fades on shutdown")
        httpd.server_close()
        if previous_term is not None:
            signal.signal(signal.SIGTERM, previous_term)
        if previous_int is not None:
            signal.signal(signal.SIGINT, previous_int)
        log.info("govee serve stopped")
    return 0


def _ignore_signals_during_shutdown() -> Any:
    """Ignore SIGTERM and SIGINT until cleanup ends; return the SIGINT handler (None off main)."""
    if threading.current_thread() is not threading.main_thread():
        return None
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    return signal.signal(signal.SIGINT, signal.SIG_IGN)


def _raise_interrupt(signum: int, frame: Any) -> None:
    raise KeyboardInterrupt


def _install_term_handler() -> Any:
    """Route SIGTERM to KeyboardInterrupt; return the previous handler (None off main thread)."""
    if threading.current_thread() is not threading.main_thread():
        return None
    return signal.signal(signal.SIGTERM, _raise_interrupt)
