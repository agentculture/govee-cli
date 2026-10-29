"""Govee LAN API transport: discovery, control and status over UDP (stdlib only).

Protocol (community-documented; openHAB govee binding, homebridge-govee ``lan.js``):

* **scan** — ``{"msg":{"cmd":"scan","data":{"account_topic":"reserve"}}}`` multicast to
  ``239.255.255.250:4001``; each lamp answers with a unicast ``scan`` reply to port 4002.
* **control** — ``turn`` / ``brightness`` / ``colorwc`` JSON to ``<device-ip>:4003``. Lamps never
  acknowledge control datagrams, so :meth:`LanTransport.send` returning ``True`` only means the
  datagram left this host; callers verify with :meth:`LanTransport.get_state`.
* **status** — ``devStatus`` to the lamp; the reply arrives on port 4002.

Lamps reply to port 4002 *regardless of the request's source port*, so only one socket per host can
receive replies. The listener is therefore bound **exclusively**: neither ``SO_REUSEPORT`` nor
``SO_REUSEADDR`` is set (on Linux, ``SO_REUSEADDR`` on two UDP sockets lets both bind the same port,
which would silently steal replies from another govee-cli process). A held port raises
:class:`PortBusy` instead.

Interface selection (multicast egress, ``IP_MULTICAST_IF``): explicit argument, else the
``GOVEE_LAN_INTERFACE`` environment variable, else the default-route interface. A value may be an
IPv4 address or a Linux interface name; names are resolved with ``fcntl.ioctl(SIOCGIFADDR)``
(no subprocess). Hosts with many interfaces (docker bridges, tailscale, ...) should pin it.
"""

from __future__ import annotations

import errno
import json
import os
import select
import socket
import struct
import time
from typing import Any, Optional

from govee_cli.govee.types import KELVIN_MAX, KELVIN_MIN, Command, Device, LampState

SCAN_GROUP = "239.255.255.250"
SCAN_PORT = 4001
LISTEN_PORT = 4002
CONTROL_PORT = 4003
# Open plan risk: community docs disagree on whether devStatus goes to 4003 (the control port,
# used by homebridge-govee and openHAB) or 4002. Default to 4003; override per-transport with
# ``LanTransport(status_port=...)`` once verified against the real lamp.
STATUS_PORT = 4003

ENV_INTERFACE = "GOVEE_LAN_INTERFACE"
DEFAULT_SCAN_TIMEOUT = 2.0
DEFAULT_STATUS_TIMEOUT = 1.0

_SIOCGIFADDR = 0x8915  # Linux ioctl: get interface IPv4 address
_ROUTE_PROBE = ("8.8.8.8", 53)  # connect() on UDP sends nothing; it only selects a route
_MAX_DATAGRAM = 65535


class LanError(Exception):
    """Base error for the LAN transport."""


class PortBusy(LanError):
    """The reply port is held by another process (another govee-cli, homebridge, ...)."""

    def __init__(self, port: int) -> None:
        self.port = port
        super().__init__(
            f"UDP port {port} is already in use; Govee LAN replies can only be received by one "
            f"process per host. Stop the other Govee LAN client (another govee-cli, homebridge-"
            f"govee, Home Assistant) or use the cloud transport."
        )


# --- wire messages ---------------------------------------------------------------------------


def _msg(cmd: str, data: dict[str, Any]) -> dict[str, Any]:
    return {"msg": {"cmd": cmd, "data": data}}


def build_turn(on: bool) -> Command:
    """``turn`` command: power on/off."""
    return Command("turn", "lan", _msg("turn", {"value": 1 if on else 0}))


def build_brightness(value: int) -> Command:
    """``brightness`` command; ``value`` must be 1-100."""
    if not 1 <= int(value) <= 100:
        raise ValueError(f"brightness must be 1-100, got {value}")
    return Command("brightness", "lan", _msg("brightness", {"value": int(value)}))


def build_color(rgb: tuple[int, int, int]) -> Command:
    """``colorwc`` command in RGB mode (``colorTemInKelvin`` 0)."""
    r, g, b = (int(c) for c in rgb)
    if not all(0 <= c <= 255 for c in (r, g, b)):
        raise ValueError(f"RGB components must be 0-255, got {rgb}")
    data = {"color": {"r": r, "g": g, "b": b}, "colorTemInKelvin": 0}
    return Command("colorwc", "lan", _msg("colorwc", data))


def build_kelvin(kelvin: int) -> Command:
    """``colorwc`` command in colour-temperature mode (2000-9000 K)."""
    if not KELVIN_MIN <= int(kelvin) <= KELVIN_MAX:
        raise ValueError(f"kelvin must be {KELVIN_MIN}-{KELVIN_MAX}, got {kelvin}")
    data = {"color": {"r": 0, "g": 0, "b": 0}, "colorTemInKelvin": int(kelvin)}
    return Command("colorwc", "lan", _msg("colorwc", data))


SCAN_REQUEST = _msg("scan", {"account_topic": "reserve"})
STATUS_REQUEST = _msg("devStatus", {})


def parse_message(data: bytes) -> Optional[tuple[str, dict[str, Any]]]:
    """Return ``(cmd, data)`` from a LAN datagram, or ``None`` if it is not a Govee message."""
    try:
        msg = json.loads(data)["msg"]
        cmd, body = msg["cmd"], msg.get("data", {})
    except (ValueError, KeyError, TypeError):
        return None
    if not isinstance(cmd, str) or not isinstance(body, dict):
        return None
    return cmd, body


def _device_from_scan(body: dict[str, Any]) -> Optional[Device]:
    dev_id, ip = body.get("device"), body.get("ip")
    if not dev_id or not ip:
        return None
    sku = str(body.get("sku") or "unknown")
    return Device(id=str(dev_id), name=sku, model=sku, ip=str(ip), transports=("lan",))


def _state_from_status(body: dict[str, Any]) -> Optional[LampState]:
    try:
        on = bool(int(body["onOff"]))
        brightness = int(body["brightness"])
    except (KeyError, TypeError, ValueError):
        return None
    kelvin = int(body.get("colorTemInKelvin") or 0)
    if kelvin > 0:
        return LampState(on=on, brightness=brightness, kelvin=kelvin)
    color = body.get("color")
    rgb = None
    if isinstance(color, dict) and {"r", "g", "b"} <= set(color):
        rgb = (int(color["r"]), int(color["g"]), int(color["b"]))
    return LampState(on=on, brightness=brightness, color=rgb)


# --- interface selection ---------------------------------------------------------------------


def _is_ipv4(value: str) -> bool:
    try:
        socket.inet_pton(socket.AF_INET, value)
    except OSError:
        return False
    return True


def default_route_ip() -> str:
    """IPv4 of the default-route interface (a UDP ``connect`` selects a route, sends nothing)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(_ROUTE_PROBE)
            return s.getsockname()[0]
        except OSError as exc:
            raise LanError(f"no default IPv4 route to pick a LAN interface: {exc}") from exc


def resolve_interface(value: str) -> str:
    """Resolve an IPv4 address or a Linux interface name to that interface's IPv4 address."""
    if _is_ipv4(value):
        return value
    try:
        socket.if_nametoindex(value)
        import fcntl  # Linux/Unix only; imported lazily so the module loads everywhere

        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            req = struct.pack("256s", value[:15].encode())
            res = fcntl.ioctl(s.fileno(), _SIOCGIFADDR, req)
    except (OSError, ImportError) as exc:
        raise LanError(
            f"cannot resolve LAN interface {value!r} to an IPv4 address ({exc}); set "
            f"{ENV_INTERFACE} to an interface name with an IPv4 address or to an IPv4 address"
        ) from exc
    return socket.inet_ntoa(res[20:24])


def interface_info(interface: Optional[str] = None) -> dict[str, Optional[str]]:
    """Report the chosen multicast interface: ``{interface, ip, source}``.

    ``source`` is ``argument``, ``env`` or ``default-route``. Raises :class:`LanError` when the
    choice cannot be resolved.
    """
    if interface:
        return {"interface": interface, "ip": resolve_interface(interface), "source": "argument"}
    env = os.environ.get(ENV_INTERFACE, "").strip()
    if env:
        return {"interface": env, "ip": resolve_interface(env), "source": "env"}
    return {"interface": None, "ip": default_route_ip(), "source": "default-route"}


# --- transport -------------------------------------------------------------------------------


class LanTransport:
    """Govee LAN API transport. Ports and addresses are parameters so tests can use 127.0.0.1."""

    def __init__(
        self,
        interface: Optional[str] = None,
        *,
        group: str = SCAN_GROUP,
        scan_port: int = SCAN_PORT,
        listen_host: str = "",
        listen_port: int = LISTEN_PORT,
        control_port: int = CONTROL_PORT,
        status_port: int = STATUS_PORT,
        scan_timeout: float = DEFAULT_SCAN_TIMEOUT,
        status_timeout: float = DEFAULT_STATUS_TIMEOUT,
    ) -> None:
        self.interface = interface
        self.group = group
        self.scan_port = scan_port
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.control_port = control_port
        self.status_port = status_port
        self.scan_timeout = scan_timeout
        self.status_timeout = status_timeout

    # -- sockets --

    def _listener(self) -> socket.socket:
        """Bind the reply port exclusively (no SO_REUSEADDR / SO_REUSEPORT, see module doc)."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # Binding all interfaces is required: lamps unicast replies to whichever host IP
            # they saw. The port is bound exclusively and only Govee JSON is parsed.
            sock.bind((self.listen_host, self.listen_port))  # nosec B104
        except OSError as exc:
            sock.close()
            if exc.errno == errno.EADDRINUSE:
                raise PortBusy(self.listen_port) from exc
            raise LanError(f"cannot bind UDP port {self.listen_port}: {exc}") from exc
        sock.setblocking(False)
        return sock

    @staticmethod
    def _receive(sock: socket.socket, deadline: float):
        """Yield ``(cmd, data, sender_ip)`` for Govee datagrams until ``deadline``."""
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            ready, _, _ = select.select([sock], [], [], remaining)
            if not ready:
                return
            try:
                data, addr = sock.recvfrom(_MAX_DATAGRAM)
            except OSError:
                continue
            parsed = parse_message(data)
            if parsed is not None:
                yield parsed[0], parsed[1], addr[0]

    # -- Transport protocol --

    def discover(self) -> list[Device]:
        """Multicast a scan and collect replies for ``scan_timeout`` seconds."""
        iface_ip = interface_info(self.interface)["ip"]
        sock = self._listener()
        try:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
            sock.setsockopt(
                socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(str(iface_ip))
            )
            sock.sendto(json.dumps(SCAN_REQUEST).encode(), (self.group, self.scan_port))
            found: dict[str, Device] = {}
            deadline = time.monotonic() + self.scan_timeout
            for cmd, body, _ in self._receive(sock, deadline):
                if cmd != "scan":
                    continue
                device = _device_from_scan(body)
                if device is not None and device.id not in found:
                    found[device.id] = device
            return list(found.values())
        except OSError as exc:
            raise LanError(f"LAN scan failed: {exc}") from exc
        finally:
            sock.close()

    def get_state(self, device: Device) -> Optional[LampState]:
        """Send ``devStatus`` and wait ``status_timeout`` seconds; ``None`` when no reply."""
        if not device.ip:
            return None
        sock = self._listener()
        try:
            sock.sendto(json.dumps(STATUS_REQUEST).encode(), (device.ip, self.status_port))
            deadline = time.monotonic() + self.status_timeout
            for cmd, body, sender in self._receive(sock, deadline):
                if cmd == "devStatus" and sender == device.ip:
                    state = _state_from_status(body)
                    if state is not None:
                        return state
            return None
        except OSError:
            return None
        finally:
            sock.close()

    def send(self, device: Device, command: Command) -> bool:
        """Send a LAN control datagram; ``True`` means it was sent (lamps never acknowledge)."""
        if not device.ip or command.transport != "lan":
            return False
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.sendto(json.dumps(command.payload).encode(), (device.ip, self.control_port))
        except OSError:
            return False
        return True

    # -- doctor support --

    def scan_report(self) -> dict[str, Any]:
        """Scan once and report for ``govee doctor``; never raises.

        Returns ``{interface: {interface, ip, source}, replied, devices: [...], error}``.
        """
        report: dict[str, Any] = {
            "interface": {"interface": self.interface, "ip": None, "source": None},
            "replied": False,
            "devices": [],
            "error": None,
        }
        try:
            report["interface"] = interface_info(self.interface)
            devices = self.discover()
        except LanError as exc:
            report["error"] = str(exc)
            return report
        report["devices"] = [
            {"id": d.id, "name": d.name, "model": d.model, "ip": d.ip} for d in devices
        ]
        report["replied"] = bool(devices)
        return report
