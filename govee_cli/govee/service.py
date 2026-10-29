"""LampService: the single code path from govee-cli to the lamp.

CLI, MCP and web handlers call :class:`LampService`; nothing else touches a transport.

* ``LampService.connect(source, actor)`` is what CLI and MCP use. When ``govee serve`` answers
  ``GET {GOVEE_SERVE_URL}/api/health`` (default ``http://127.0.0.1:8742``) every operation, LAN
  and cloud, is routed through serve's loopback API with the client token (approved deviation d4);
  otherwise the transports run directly in this process with the environment's key.
* ``LampService.direct(...)`` is what ``govee serve`` itself uses (it never routes to itself).

Writes are dry runs unless ``apply=True``: a dry run returns the planned commands and sends
nothing. Every call made with ``apply=True`` sends exactly the commands a dry run would plan.
Transport failures surface as ``CliError`` (exit 2 for environment problems, with remediation).
"""

from __future__ import annotations

from typing import Any, Optional

from govee_cli.cli._errors import EXIT_ENV_ERROR, CliError
from govee_cli.govee.service_direct import DirectBackend, state_matches
from govee_cli.govee.service_serve import (
    DEFAULT_SERVE_PORT,
    DEFAULT_SERVE_URL,
    ENV_SERVE_URL,
    ENV_TOKEN,
    HEALTH_TIMEOUT,
    ServeBackend,
    client_token,
    client_token_path,
    serve_base_url,
    serve_running,
)
from govee_cli.govee.service_types import (
    REMEDIATION_TOKEN,
    ApplyResult,
    Change,
    LampStatus,
    SceneInfo,
    check_source,
    hex_color,
    map_error,
    parse_hex_color,
)
from govee_cli.govee.types import Device

__all__ = [
    "ApplyResult",
    "Change",
    "DEFAULT_SERVE_PORT",
    "DEFAULT_SERVE_URL",
    "ENV_SERVE_URL",
    "ENV_TOKEN",
    "LampService",
    "LampStatus",
    "SceneInfo",
    "client_token",
    "client_token_path",
    "hex_color",
    "map_error",
    "parse_hex_color",
    "serve_base_url",
    "serve_running",
    "state_matches",
]


class LampService:
    """Facade over a Direct or Serve backend. ``source``/``actor`` are per-instance defaults that
    every method also accepts per call (serve handles many web/loopback clients on one instance).
    """

    def __init__(self, backend: Any, *, source: str = "cli", actor: Optional[str] = None) -> None:
        self.backend = backend
        self.source = check_source(source)
        self.actor = actor

    @property
    def routed(self) -> bool:
        """True when operations go through ``govee serve``."""
        return bool(getattr(self.backend, "routed", False))

    # -- construction --

    @classmethod
    def direct(
        cls,
        *,
        source: str = "cli",
        actor: Optional[str] = None,
        lan: Any = None,
        cloud: Any = None,
        **options: Any,
    ) -> "LampService":
        """Run the transports in-process. ``options``: ``verify_timeout``, ``poll_interval``,
        ``cache_ttl``, ``clock``, ``sleep`` (see ``DirectBackend``)."""
        return cls(DirectBackend(lan=lan, cloud=cloud, **options), source=source, actor=actor)

    @classmethod
    def connect(
        cls,
        source: str = "cli",
        actor: Optional[str] = None,
        *,
        health_timeout: float = HEALTH_TIMEOUT,
        **direct_options: Any,
    ) -> "LampService":
        """Route through ``govee serve`` when it runs, else run directly (``direct_options`` are
        passed to :meth:`direct`). Serve running without a client token is ``CliError(2)``."""
        check_source(source)
        base = serve_base_url()
        if serve_running(base, timeout=health_timeout):
            token = client_token()
            if not token:
                raise CliError(
                    EXIT_ENV_ERROR,
                    f"govee serve is running at {base} but this client has no token",
                    f"{REMEDIATION_TOKEN} (or set {ENV_TOKEN})",
                )
            return cls(ServeBackend(base, token), source=source, actor=actor)
        return cls.direct(source=source, actor=actor, **direct_options)

    # -- operations --

    def _ctx(self, source: Optional[str], actor: Optional[str]) -> dict[str, Any]:
        return {
            "source": check_source(source) if source is not None else self.source,
            "actor": actor if actor is not None else self.actor,
        }

    def devices(
        self, *, refresh: bool = False, source: Optional[str] = None, actor: Optional[str] = None
    ) -> list[Device]:
        return self.backend.devices(refresh=refresh, **self._ctx(source, actor))

    def state(
        self,
        device: Optional[str] = None,
        *,
        source: Optional[str] = None,
        actor: Optional[str] = None,
    ) -> LampStatus:
        return self.backend.state(device, **self._ctx(source, actor))

    def scenes(
        self,
        device: Optional[str] = None,
        *,
        source: Optional[str] = None,
        actor: Optional[str] = None,
    ) -> list[SceneInfo]:
        return self.backend.scenes(device, **self._ctx(source, actor))

    def apply(
        self,
        change: Change,
        *,
        apply: bool = False,
        device: Optional[str] = None,
        source: Optional[str] = None,
        actor: Optional[str] = None,
    ) -> ApplyResult:
        """Plan (and with ``apply=True`` send, log and verify) a light change."""
        return self.backend.apply(change, apply=apply, device=device, **self._ctx(source, actor))

    def set_scene(
        self,
        name: str,
        *,
        apply: bool = False,
        device: Optional[str] = None,
        source: Optional[str] = None,
        actor: Optional[str] = None,
    ) -> ApplyResult:
        """Plan (and with ``apply=True`` send and log) a scene; scenes always go over the cloud."""
        return self.backend.set_scene(name, apply=apply, device=device, **self._ctx(source, actor))
