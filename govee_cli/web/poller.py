"""State poller (t11): samples the lamp for the usage statistics.

A daemon thread, started by ``govee serve`` via :meth:`Poller.start`, reads the lamp through
:class:`~govee_cli.govee.service.LampService` and writes one ``samples`` row per reading. The
next sample is due after ``lan_interval`` (60 s) when the last reading came over the LAN and
after ``cloud_interval`` (300 s, never lower) when it came from the cloud. A lamp that does not
answer writes nothing: the stats queries report that as a gap, not as "off". A lamp that answers
"off" writes ``on=False``, which is real. There is no schedule or time-of-day logic.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from govee_cli.cli._errors import CliError
from govee_cli.govee.cloud import RateLimited
from govee_cli.stats import store

log = logging.getLogger("govee_cli.poller")

LAN_INTERVAL = 60.0
CLOUD_INTERVAL = 300.0  # the cloud API is rate limited; never poll it faster than this
MAX_BACKOFF = 3600.0  # longest wait for a rate-limit reset
ERROR_BACKOFF = 60.0  # wait after an unexpected error
_WAIT_ISO = re.compile(r"wait until (\S+) and retry")


def _rate_limit_reset(exc: BaseException) -> Optional[tuple[Optional[datetime]]]:
    """``(reset_at,)`` if ``exc`` is, or maps, a cloud rate limit; else ``None``.

    ``LampService`` surfaces ``RateLimited`` as ``CliError`` (``map_error``), which keeps the
    original in ``__context__`` and prints the reset time in its remediation.
    """
    seen: Optional[BaseException] = exc
    while seen is not None:
        if isinstance(seen, RateLimited):
            return (seen.reset_at,)
        seen = seen.__cause__ or seen.__context__
    if isinstance(exc, CliError) and "rate limit" in exc.message.lower():
        m = _WAIT_ISO.search(exc.remediation or "")
        if m:
            try:
                return (datetime.fromisoformat(m.group(1)),)
            except ValueError:
                pass
        return (None,)
    return None


class Poller:
    def __init__(
        self,
        service: Any,
        *,
        lan_interval: float = LAN_INTERVAL,
        cloud_interval: float = CLOUD_INTERVAL,
        clock: Callable[[], float] = time.time,
        wait: Optional[Callable[[float], bool]] = None,
        device: Optional[str] = None,
    ) -> None:
        self.service = service
        self.lan_interval = float(lan_interval)
        self.cloud_interval = max(float(cloud_interval), CLOUD_INTERVAL)
        self.clock = clock
        self.device = device
        self._stop = threading.Event()
        self._wait = wait or self._stop.wait
        self._thread: Optional[threading.Thread] = None
        self._last_interval = self.lan_interval

    # -- one sample --

    def sample_once(self) -> float:
        """Read the lamp, write a row if it answered, return seconds until the next sample."""
        try:
            status = self.service.state(self.device)
        except Exception as exc:  # noqa: BLE001 - the thread must never die
            return self._on_error(exc)
        interval = self.cloud_interval if status.transport == "cloud" else self.lan_interval
        self._last_interval = interval
        s = status.state
        if s is None:
            log.debug("lamp unreachable over %s; no sample (gap)", status.transport)
            return interval
        now = datetime.fromtimestamp(self.clock(), timezone.utc)
        try:
            store.record_sample(
                bool(s.on),
                int(s.brightness),
                None if s.kelvin else s.color,
                s.kelvin,
                now=now,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("could not write sample (%s)", type(exc).__name__)
            return ERROR_BACKOFF
        return interval

    def _on_error(self, exc: Exception) -> float:
        limited = _rate_limit_reset(exc)
        if limited is None:
            log.debug("poll failed (%s); backing off", type(exc).__name__)
            return ERROR_BACKOFF
        reset = limited[0]
        if reset is None:
            return self._last_interval
        if reset.tzinfo is None:
            reset = reset.replace(tzinfo=timezone.utc)
        delay = reset.timestamp() - self.clock()
        delay = min(max(delay, 1.0), MAX_BACKOFF)
        log.info("cloud rate limited; next sample in %.0f s", delay)
        return delay

    # -- loop and lifecycle --

    def run(self) -> None:
        """Sample, wait, repeat until stopped (``wait`` returns true once stop is requested)."""
        while not self._stop.is_set():
            delay = self.sample_once()
            if self._wait(delay):
                break

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self.run, name="govee-poller", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout)
        if t is not None and not t.is_alive():
            self._thread = None
