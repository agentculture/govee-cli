"""Sqlite stats store: events, state samples and chart queries.

Stdlib only. All timestamps are stored as UTC epoch seconds (REAL). "Day" and
"hour" bucketing is done in the ``tz`` the query is given (a ``tzinfo``; default UTC):
``govee serve`` passes its local timezone (approved deviation d5). Days are bucketed by
calendar midnights in that zone, so a DST day is 23 or 25 hours long. Each sample represents the lamp state for the
interval until the next sample, capped at ``SAMPLE_CAP_S`` (3 min). Time not
covered by a sample is a *gap* (unknown), never interpolated as "off".
"""

from __future__ import annotations

import json
import os
from bisect import bisect_right
import sqlite3
import time
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, Iterator, Optional

SCHEMA_VERSION = 1
SAMPLE_CAP_S = 180
SOURCES = ("cli", "mcp", "web", "rules")
STATUSES = ("dry_run", "sent", "confirmed", "unconfirmed")
APPLIED = ("sent", "confirmed", "unconfirmed")
AGENT_SOURCES = ("mcp", "rules")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('cli','mcp','web','rules')),
    actor TEXT,
    verb TEXT NOT NULL,
    payload TEXT,
    status TEXT NOT NULL CHECK (status IN ('dry_run','sent','confirmed','unconfirmed'))
);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS samples (
    ts REAL PRIMARY KEY,
    "on" INTEGER NOT NULL,
    brightness INTEGER,
    rgb TEXT,
    kelvin INTEGER
);
"""


def db_path() -> Path:
    """Stats DB path: $XDG_STATE_HOME/govee-cli/stats.db (default ~/.local/state)."""
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "govee-cli" / "stats.db"


def connect() -> sqlite3.Connection:
    """Open a connection with WAL, busy_timeout=5000 and the schema in place."""
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    # Switching a fresh database to WAL (and creating the schema) can return SQLITE_BUSY
    # without honouring busy_timeout when several connections open it at once; retry.
    deadline = time.monotonic() + 5.0
    while True:
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(_SCHEMA)
            if conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 0:
                conn.execute(
                    "INSERT INTO schema_version (version) SELECT ? "
                    "WHERE NOT EXISTS (SELECT 1 FROM schema_version)",
                    (SCHEMA_VERSION,),
                )
            return conn
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) or time.monotonic() > deadline:
                conn.close()
                raise
            time.sleep(0.02)


def _utc(now: Optional[datetime]) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    return now if now.tzinfo else now.replace(tzinfo=timezone.utc)


def _local(now: Optional[datetime], tz: Optional[tzinfo]) -> datetime:
    """``now`` (default: the current time) as an aware datetime in ``tz`` (default UTC)."""
    return _utc(now).astimezone(tz or timezone.utc)


def _midnight(dt: datetime) -> datetime:
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def _day_starts(days: int, now: datetime) -> list[datetime]:
    today = _midnight(now)
    return [today - timedelta(days=i) for i in range(days - 1, -1, -1)]


# --- writes ---------------------------------------------------------------


def record_event(
    source: str,
    actor: Optional[str],
    verb: str,
    payload: Any,
    status: str,
    now: Optional[datetime] = None,
) -> int:
    if source not in SOURCES:
        raise ValueError(f"invalid source {source!r}")
    if status not in STATUSES:
        raise ValueError(f"invalid status {status!r}")
    conn = connect()
    try:
        cur = conn.execute(
            "INSERT INTO events (ts, source, actor, verb, payload, status) VALUES (?,?,?,?,?,?)",
            (_utc(now).timestamp(), source, actor, verb, json.dumps(payload), status),
        )
        return int(cur.lastrowid)
    finally:
        conn.close()


def update_event_status(event_id: int, status: str) -> None:
    if status not in STATUSES:
        raise ValueError(f"invalid status {status!r}")
    conn = connect()
    try:
        conn.execute("UPDATE events SET status=? WHERE id=?", (status, event_id))
    finally:
        conn.close()


def update_event_payload(event_id: int, payload: Any) -> None:
    """Replace an event's JSON payload (a cancelled fade records ``"cancelled": true``)."""
    conn = connect()
    try:
        conn.execute("UPDATE events SET payload=? WHERE id=?", (json.dumps(payload), event_id))
    finally:
        conn.close()


def record_sample(
    on: bool,
    brightness: Optional[int] = None,
    rgb: Optional[tuple] = None,
    kelvin: Optional[int] = None,
    now: Optional[datetime] = None,
) -> None:
    conn = connect()
    try:
        conn.execute(
            'INSERT OR REPLACE INTO samples (ts, "on", brightness, rgb, kelvin) VALUES (?,?,?,?,?)',
            (
                _utc(now).timestamp(),
                1 if on else 0,
                brightness,
                json.dumps(list(rgb)) if rgb is not None else None,
                kelvin,
            ),
        )
    finally:
        conn.close()


# --- reads ----------------------------------------------------------------


def recent_events(limit: int = 20) -> list[dict]:
    conn = connect()
    try:
        rows = conn.execute("SELECT * FROM events ORDER BY ts DESC, id DESC LIMIT ?", (limit,))
        return [
            {
                "id": r["id"],
                "ts": datetime.fromtimestamp(r["ts"], timezone.utc).isoformat(),
                "source": r["source"],
                "actor": r["actor"],
                "verb": r["verb"],
                "payload": json.loads(r["payload"]) if r["payload"] is not None else None,
                "status": r["status"],
            }
            for r in rows
        ]
    finally:
        conn.close()


def _samples_between(conn: sqlite3.Connection, start: float, end: float) -> list[sqlite3.Row]:
    return conn.execute(
        'SELECT ts, "on", brightness, rgb, kelvin FROM samples WHERE ts >= ? AND ts <= ? '
        "ORDER BY ts",
        (start - SAMPLE_CAP_S, end),
    ).fetchall()


def _segments(rows: list, start: float, end: float) -> Iterator[tuple[float, float, Any]]:
    """Yield (seg_start, seg_end, row) clipped to [start, end] and capped at 3 min."""
    for i, r in enumerate(rows):
        nxt = rows[i + 1]["ts"] if i + 1 < len(rows) else end
        s, e = r["ts"], min(nxt, r["ts"] + SAMPLE_CAP_S)
        s, e = max(s, start), min(e, end)
        if e > s:
            yield s, e, r


def _on_seconds_per_bucket(rows: list, start: float, end: float, step: int) -> dict[int, float]:
    """On-seconds keyed by floor((t-start)/step) buckets (step in seconds)."""
    out: dict[int, float] = {}
    for s, e, r in _segments(rows, start, end):
        if not r["on"]:
            continue
        t = s
        while t < e:
            b = int((t - start) // step)
            edge = start + (b + 1) * step
            out[b] = out.get(b, 0.0) + (min(e, edge) - t)
            t = min(e, edge)
    return out


def _on_seconds(rows: list, start: float, end: float) -> float:
    return sum(e - s for s, e, r in _segments(rows, start, end) if r["on"])


def hours_per_day(
    days: int = 14, now: Optional[datetime] = None, tz: Optional[tzinfo] = None
) -> list[dict]:
    now = _local(now, tz)
    starts = _day_starts(days, now)
    edges = [d.timestamp() for d in starts] + [now.timestamp()]
    conn = connect()
    try:
        rows = _samples_between(conn, edges[0], edges[-1])
    finally:
        conn.close()
    return [
        {"date": d.strftime("%Y-%m-%d"), "hours": _on_seconds(rows, edges[i], edges[i + 1]) / 3600}
        for i, d in enumerate(starts)
    ]


def commands_by_source(
    days: int = 7, now: Optional[datetime] = None, tz: Optional[tzinfo] = None
) -> list[dict]:
    now = _local(now, tz)
    starts = _day_starts(days, now)
    out = [
        {"date": d.strftime("%Y-%m-%d"), **{s: 0 for s in ("cli", "web", "mcp", "rules")}}
        for d in starts
    ]
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT ts, source FROM events WHERE ts >= ? AND ts <= ? AND status IN (?,?,?)",
            (starts[0].timestamp(), now.timestamp(), *APPLIED),
        ).fetchall()
    finally:
        conn.close()
    edges = [d.timestamp() for d in starts]
    for r in rows:
        idx = bisect_right(edges, r["ts"]) - 1
        if 0 <= idx < days:
            out[idx][r["source"]] += 1
    return out


def day_samples(
    hours: int = 24, now: Optional[datetime] = None, tz: Optional[tzinfo] = None
) -> list[dict]:
    now = _utc(now)
    zone = tz or timezone.utc
    end = now.timestamp()
    start = end - hours * 3600
    conn = connect()
    try:
        rows = conn.execute(
            'SELECT ts, "on", brightness, rgb, kelvin FROM samples WHERE ts >= ? AND ts <= ? '
            "ORDER BY ts",
            (start, end),
        ).fetchall()
    finally:
        conn.close()

    def iso(t: float) -> str:
        return datetime.fromtimestamp(t, zone).isoformat()

    out: list[dict] = []
    prev: Optional[float] = None
    for r in rows:
        if prev is None:
            out.append({"ts": iso(start), "gap": True})
        elif r["ts"] - prev > SAMPLE_CAP_S:
            out.append({"ts": iso(prev + SAMPLE_CAP_S), "gap": True})
        out.append(
            {
                "ts": iso(r["ts"]),
                "on": bool(r["on"]),
                "brightness": r["brightness"],
                "rgb": json.loads(r["rgb"]) if r["rgb"] else None,
                "kelvin": r["kelvin"],
            }
        )
        prev = r["ts"]
    if prev is None:
        out.append({"ts": iso(start), "gap": True})
    elif end - prev > SAMPLE_CAP_S:
        out.append({"ts": iso(prev + SAMPLE_CAP_S), "gap": True})
    return out


def heatmap(
    days: int = 28, now: Optional[datetime] = None, tz: Optional[tzinfo] = None
) -> list[dict]:
    """Average minutes on per (weekday, hour), averaged over the calendar days in the window."""
    now = _local(now, tz)
    starts = _day_starts(days, now)
    start, end = starts[0].timestamp(), now.timestamp()
    conn = connect()
    try:
        rows = _samples_between(conn, start, end)
    finally:
        conn.close()
    hourly = _on_seconds_per_bucket(rows, start, end, 3600)
    totals: dict[tuple[int, int], float] = {}
    for b, secs in hourly.items():
        dt = datetime.fromtimestamp(start + b * 3600, now.tzinfo)
        key = (dt.weekday(), dt.hour)
        totals[key] = totals.get(key, 0.0) + secs
    occurrences = [sum(1 for d in starts if d.weekday() == w) for w in range(7)]
    return [
        {
            "weekday": w,
            "hour": h,
            "minutes": totals.get((w, h), 0.0) / 60 / occurrences[w] if occurrences[w] else 0.0,
        }
        for w in range(7)
        for h in range(24)
    ]


# --- tile helpers ---------------------------------------------------------


def _on_hours_since(start: datetime, now: datetime) -> float:
    conn = connect()
    try:
        rows = _samples_between(conn, start.timestamp(), now.timestamp())
    finally:
        conn.close()
    return (
        sum(_on_seconds_per_bucket(rows, start.timestamp(), now.timestamp(), 10**9).values()) / 3600
    )


def on_today_h(now: Optional[datetime] = None, tz: Optional[tzinfo] = None) -> float:
    now = _local(now, tz)
    return _on_hours_since(_midnight(now), now)


def on_week_h(now: Optional[datetime] = None, tz: Optional[tzinfo] = None) -> float:
    now = _local(now, tz)
    return _on_hours_since(_midnight(now) - timedelta(days=6), now)


def _count_commands(now: datetime, sources: tuple) -> int:
    start = (_midnight(now) - timedelta(days=6)).timestamp()
    marks = ",".join("?" * len(sources))
    conn = connect()
    try:
        return int(
            conn.execute(
                f"SELECT COUNT(*) FROM events WHERE ts >= ? AND ts <= ? "  # nosec B608
                f"AND status IN (?,?,?) AND source IN ({marks})",
                (start, now.timestamp(), *APPLIED, *sources),
            ).fetchone()[0]
        )
    finally:
        conn.close()


def commands_7d(now: Optional[datetime] = None, tz: Optional[tzinfo] = None) -> int:
    return _count_commands(_local(now, tz), SOURCES)


def agent_commands_7d(now: Optional[datetime] = None, tz: Optional[tzinfo] = None) -> int:
    return _count_commands(_local(now, tz), AGENT_SOURCES)


def avg_brightness_today(
    now: Optional[datetime] = None, tz: Optional[tzinfo] = None
) -> Optional[float]:
    """Time-weighted average brightness while on today; None if never on."""
    now = _local(now, tz)
    start, end = _midnight(now).timestamp(), now.timestamp()
    conn = connect()
    try:
        rows = _samples_between(conn, start, end)
    finally:
        conn.close()
    total = weighted = 0.0
    for s, e, r in _segments(rows, start, end):
        if r["on"] and r["brightness"] is not None:
            total += e - s
            weighted += (e - s) * r["brightness"]
    return weighted / total if total else None
