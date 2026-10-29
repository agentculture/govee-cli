"""Tests for the sqlite stats store."""

from __future__ import annotations

import multiprocessing
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from govee_cli.stats import store

NOW = datetime(2026, 3, 11, 12, 0, 0, tzinfo=timezone.utc)  # a Wednesday


def at(h, m=0, day=11):
    return datetime(2026, 3, day, h, m, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    return tmp_path / "state"


def test_db_path_under_xdg_state_home(state):
    assert store.db_path() == state / "govee-cli" / "stats.db"


def test_db_path_default(monkeypatch):
    monkeypatch.delenv("XDG_STATE_HOME")
    assert store.db_path() == Path.home() / ".local/state/govee-cli/stats.db"


def test_db_path_never_in_repo(state):
    repo = Path(store.__file__).resolve().parents[2]
    assert repo not in store.db_path().resolve().parents


def test_connection_pragmas_and_schema():
    conn = store.connect()
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 1
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"events", "samples", "schema_version"} <= names
    conn.close()
    store.connect().close()  # reopen is idempotent
    assert store.db_path().exists()


def _writer(duration, source, out):
    end = time.time() + duration
    n = 0
    try:
        while time.time() < end:
            store.record_event(source, "tester", "on", {"n": n}, "sent")
            n += 1
        out.put(("ok", n))
    except Exception as exc:  # noqa: BLE001
        out.put(("err", str(exc)))


def test_two_processes_write_5s_no_lock_error():
    store.connect().close()
    # spawn, not fork: the pytest-xdist worker is multi-threaded, and fork() there can deadlock
    ctx = multiprocessing.get_context("spawn")
    q = ctx.Queue()
    procs = [ctx.Process(target=_writer, args=(5, s, q)) for s in ("cli", "web")]
    for p in procs:
        p.start()
    results = [q.get(timeout=20) for _ in procs]
    for p in procs:
        p.join(10)
    assert all(r[0] == "ok" for r in results), results
    conn = store.connect()
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == sum(r[1] for r in results)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_event_roundtrip_and_status_update():
    eid = store.record_event("cli", "me", "brightness", {"value": 50}, "dry_run", now=at(10))
    store.update_event_status(eid, "confirmed")
    ev = store.recent_events(limit=5)
    assert len(ev) == 1
    assert ev[0]["status"] == "confirmed" and ev[0]["source"] == "cli"
    assert ev[0]["payload"] == {"value": 50} and ev[0]["id"] == eid


def test_invalid_source_and_status_rejected():
    with pytest.raises(ValueError):
        store.record_event("bogus", "a", "on", None, "sent")
    with pytest.raises(ValueError):
        store.record_event("cli", "a", "on", None, "nope")
    eid = store.record_event("cli", "a", "on", None, "sent")
    with pytest.raises(ValueError):
        store.update_event_status(eid, "nope")


def test_recent_events_newest_first_and_limit():
    for i in range(5):
        store.record_event("cli", "a", f"v{i}", None, "sent", now=at(1, i))
    ev = store.recent_events(limit=3)
    assert [e["verb"] for e in ev] == ["v4", "v3", "v2"]


def test_hours_per_day_interval_capped_and_gaps_not_off():
    # on at 10:00, next sample 10:02 -> 2 min; then 10:02 sample with gap after -> capped 3 min
    store.record_sample(True, 80, (1, 2, 3), None, now=at(10, 0))
    store.record_sample(True, 80, (1, 2, 3), None, now=at(10, 2))
    store.record_sample(False, 0, None, None, now=at(11, 0))
    out = store.hours_per_day(days=2, now=NOW)
    assert [d["date"] for d in out] == ["2026-03-10", "2026-03-11"]
    assert out[0]["hours"] == 0
    assert out[1]["hours"] == pytest.approx(5 / 60)


def test_hours_per_day_splits_across_midnight():
    store.record_sample(
        True, 50, None, 3000, now=datetime(2026, 3, 10, 23, 59, tzinfo=timezone.utc)
    )
    store.record_sample(True, 50, None, 3000, now=at(0, 1))
    out = store.hours_per_day(days=2, now=NOW)
    assert out[0]["hours"] == pytest.approx(1 / 60)
    assert out[1]["hours"] == pytest.approx(4 / 60)  # 1 min carried over + 3 min cap


def test_commands_by_source_counts_applied_only():
    store.record_event("cli", "a", "on", None, "sent", now=at(9))
    store.record_event("cli", "a", "on", None, "dry_run", now=at(9, 1))
    store.record_event("mcp", "a", "on", None, "confirmed", now=at(9, 2))
    store.record_event("rules", "a", "on", None, "unconfirmed", now=at(9, 3))
    store.record_event("web", "a", "on", None, "sent", now=at(9, 4, day=10))
    out = store.commands_by_source(days=2, now=NOW)
    assert out == [
        {"date": "2026-03-10", "cli": 0, "web": 1, "mcp": 0, "rules": 0},
        {"date": "2026-03-11", "cli": 1, "web": 0, "mcp": 1, "rules": 1},
    ]


def test_day_samples_gaps():
    store.record_sample(True, 80, (1, 2, 3), None, now=at(10, 0))
    store.record_sample(True, 70, None, 4000, now=at(10, 2))
    store.record_sample(False, 0, None, None, now=at(10, 30))
    out = store.day_samples(hours=24, now=NOW)
    kinds = ["gap" if "gap" in e else "s" for e in out]
    # leading gap, s, s, gap (10:02 -> 10:30), s, trailing gap
    assert kinds == ["gap", "s", "s", "gap", "s", "gap"]
    assert out[1]["on"] is True and out[1]["brightness"] == 80 and out[1]["rgb"] == [1, 2, 3]
    assert out[2]["kelvin"] == 4000
    assert out[3]["gap"] is True and "ts" in out[3]
    # a gap is not an off sample
    assert all("on" not in e for e in out if e.get("gap"))


def test_day_samples_empty_is_single_gap():
    out = store.day_samples(hours=24, now=NOW)
    assert len(out) == 1 and out[0]["gap"] is True


def test_heatmap_average_minutes():
    # Wed 2026-03-11 10:00-10:20 on (samples every 2 min), plus same hour previous Wed
    for day in (11, 4):
        for m in range(0, 20, 2):
            store.record_sample(True, 50, None, None, now=at(10, m, day=day))
        store.record_sample(False, 0, None, None, now=at(10, 20, day=day))
    out = store.heatmap(days=28, now=NOW)
    cell = [c for c in out if c["weekday"] == 2 and c["hour"] == 10]
    assert len(cell) == 1
    assert cell[0]["minutes"] == pytest.approx(40 / 4)  # 2 Wednesdays x 20 min, 4 Wednesdays in 28d
    assert len(out) == 7 * 24


def test_tiles():
    store.record_sample(True, 100, None, None, now=at(8, 0))
    store.record_sample(True, 50, None, None, now=at(8, 2))
    store.record_sample(False, 0, None, None, now=at(8, 10))
    store.record_event("cli", "a", "on", None, "sent", now=at(9))
    store.record_event("mcp", "a", "on", None, "sent", now=at(9, 1))
    store.record_event("rules", "a", "on", None, "confirmed", now=at(9, 2))
    store.record_event("mcp", "a", "on", None, "dry_run", now=at(9, 3))
    assert store.on_today_h(now=NOW) == pytest.approx(5 / 60)
    assert store.on_week_h(now=NOW) == pytest.approx(5 / 60)
    assert store.commands_7d(now=NOW) == 3
    assert store.agent_commands_7d(now=NOW) == 2
    assert store.avg_brightness_today(now=NOW) == pytest.approx((100 * 2 + 50 * 3) / 5)
    assert store.avg_brightness_today(now=at(0, 0, day=1)) is None


def test_concurrent_first_connect_never_raises_locked():
    # Switching a brand-new database to WAL can return SQLITE_BUSY without honouring
    # busy_timeout; many threads opening a fresh DB at once must all still succeed.
    import threading

    errors = []

    def opener():
        try:
            store.connect().close()
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))

    for _ in range(15):
        store.db_path().unlink(missing_ok=True)
        threads = [threading.Thread(target=opener) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert errors == []
