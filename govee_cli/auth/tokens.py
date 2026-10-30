"""Named, hashed, revocable pairing tokens.

Only the sha256 of each secret is stored, in ``$XDG_CONFIG_HOME/govee-cli/tokens.json``
(mode 0600, directory 0700). Writes are atomic (temp file + ``os.replace``).
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_NAME_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")
LAST_USED_INTERVAL = 60.0  # seconds; min gap between last_used rewrites per token


class TokenStoreError(Exception):
    """tokens.json is unreadable or malformed (never silently treated as empty)."""


def default_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "govee-cli" / "tokens.json"


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load(path: Path) -> list[dict[str, Any]]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise TokenStoreError(f"cannot read {path}: {exc}") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise TokenStoreError(f"{path} is not valid JSON: {exc}") from exc
    items = data.get("tokens") if isinstance(data, dict) else None
    if not isinstance(items, list) or not all(
        isinstance(t, dict) and isinstance(t.get("name"), str) and isinstance(t.get("sha256"), str)
        for t in items
    ):
        raise TokenStoreError(f"{path} has an unexpected structure")
    return items


def _save(path: Path, items: list[dict[str, Any]]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tokens-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:  # mkstemp creates 0600
            json.dump({"tokens": items}, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@contextlib.contextmanager
def _locked(path: Path):
    """Serialise read-modify-write cycles across processes (so a verify can't undo a revoke)."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = path.parent / ".tokens.lock"
    fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o600)
    os.fchmod(fd, 0o600)  # also tighten a lock file an older version created 0664
    with os.fdopen(fd, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def new(name: str, path: Path | str | None = None) -> str:
    """Mint a token; returns the secret (shown once, never stored)."""
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        raise ValueError("invalid token name: use 1-64 characters from [A-Za-z0-9._-]")
    p = Path(path) if path else default_path()
    with _locked(p):
        items = _load(p)
        if any(t["name"] == name for t in items):
            raise ValueError(f"token name already exists: {name}")
        secret = secrets.token_urlsafe(32)
        items.append(
            {
                "name": name,
                "sha256": hash_secret(secret),
                "created": _iso(time.time()),
                "last_used": None,
            }
        )
        _save(p, items)
    return secret


def list_tokens(path: Path | str | None = None) -> list[dict[str, Any]]:
    """Name, created and last_used per token; never hashes or secrets."""
    p = Path(path) if path else default_path()
    return [
        {"name": t["name"], "created": t.get("created"), "last_used": t.get("last_used")}
        for t in _load(p)
    ]


def revoke(name: str, path: Path | str | None = None) -> None:
    p = Path(path) if path else default_path()
    with _locked(p):
        items = _load(p)
        kept = [t for t in items if t["name"] != name]
        if len(kept) == len(items):
            raise KeyError(name)
        _save(p, kept)


def revoke_all(path: Path | str | None = None) -> int:
    p = Path(path) if path else default_path()
    with _locked(p):
        items = _load(p)
        if items:
            _save(p, [])
    return len(items)


def verify(secret: str, path: Path | str | None = None, now: float | None = None) -> str | None:
    """Return the token name if ``secret`` is valid, else None."""
    p = Path(path) if path else default_path()
    items = _load(p)
    if not secret or not isinstance(secret, str):
        return None
    digest = hash_secret(secret)
    match = None
    for t in items:  # no early exit: compare against every stored hash
        if hmac.compare_digest(digest, t["sha256"]) and match is None:
            match = t
    if match is None:
        return None
    ts = time.time() if now is None else now
    if not _is_stale(match.get("last_used"), ts):
        return match["name"]
    with _locked(p):  # re-read under the lock so a concurrent revoke wins
        fresh = _load(p)
        for t in fresh:
            if t["sha256"] == match["sha256"]:
                t["last_used"] = _iso(ts)
                _save(p, fresh)
                return t["name"]
    return None


def _is_stale(last: Any, ts: float) -> bool:
    if not isinstance(last, str):
        return True
    try:
        prev = datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    return ts - prev.timestamp() >= LAST_USED_INTERVAL


DEFAULT_SERVE_PORT = 8742


def pairing_link(secret: str, host: str, port: int = DEFAULT_SERVE_PORT) -> str:
    """The console URL that pairs a browser: the token rides in the #fragment, never the query.

    Fragments are not sent to the server, so the secret never reaches access logs or proxies.
    """
    if not secret or any(c in secret for c in "#&= /"):
        raise ValueError("pairing secret must be a non-empty url-safe token")
    # govee serve speaks plain HTTP on the home LAN by design; the link must match it.
    return f"http://{host}:{int(port)}/#" + "token" + "=" + secret  # NOSONAR
