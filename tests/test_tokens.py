"""Tests for the named pairing-token store."""

import json
import os
import stat

import pytest

from govee_cli.auth import tokens


@pytest.fixture
def path(tmp_path):
    return tmp_path / "cfg" / "govee-cli" / "tokens.json"


def test_new_returns_secret_and_stores_only_hash(path):
    secret = tokens.new("phone", path=path)
    assert len(secret) >= 43  # 32 bytes urlsafe
    raw = path.read_text()
    assert secret not in raw
    data = json.loads(raw)
    assert "phone" in raw
    assert tokens.hash_secret(secret) in raw
    assert data


def test_file_mode_0600_and_dir_0700(path):
    tokens.new("a", path=path)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700


def test_mode_stays_0600_after_update(path):
    tokens.new("a", path=path)
    tokens.new("b", path=path)
    tokens.revoke("a", path=path)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_list_never_returns_secrets(path):
    secret = tokens.new("phone", path=path)
    rows = tokens.list_tokens(path=path)
    assert [r["name"] for r in rows] == ["phone"]
    assert set(rows[0]) == {"name", "created", "last_used"}
    assert secret not in json.dumps(rows)
    assert rows[0]["last_used"] is None


def test_duplicate_name_refused(path):
    tokens.new("phone", path=path)
    with pytest.raises(ValueError, match="already exists"):
        tokens.new("phone", path=path)


@pytest.mark.parametrize("bad", ["", "a b", "x" * 65, "ü", "a/b"])
def test_invalid_names(path, bad):
    with pytest.raises(ValueError, match="name"):
        tokens.new(bad, path=path)


def test_verify_returns_name_and_updates_last_used(path):
    secret = tokens.new("phone", path=path)
    assert tokens.verify(secret, path=path) == "phone"
    assert tokens.list_tokens(path=path)[0]["last_used"] is not None


def test_verify_rejects_wrong_and_empty(path):
    tokens.new("phone", path=path)
    assert tokens.verify("nope", path=path) is None
    assert tokens.verify("", path=path) is None


def test_verify_uses_compare_digest_on_every_hash(path, monkeypatch):
    s1 = tokens.new("a", path=path)
    tokens.new("b", path=path)
    calls = []
    real = tokens.hmac.compare_digest

    def spy(x, y):
        calls.append(1)
        return real(x, y)

    monkeypatch.setattr(tokens.hmac, "compare_digest", spy)
    assert tokens.verify(s1, path=path) == "a"
    assert len(calls) == 2


def test_last_used_throttled(path):
    secret = tokens.new("phone", path=path)
    tokens.verify(secret, path=path, now=1000.0)
    first = tokens.list_tokens(path=path)[0]["last_used"]
    tokens.verify(secret, path=path, now=1030.0)
    assert tokens.list_tokens(path=path)[0]["last_used"] == first
    tokens.verify(secret, path=path, now=1100.0)
    assert tokens.list_tokens(path=path)[0]["last_used"] != first


def test_revoked_token_fails_immediately(path):
    secret = tokens.new("phone", path=path)
    tokens.revoke("phone", path=path)
    assert tokens.verify(secret, path=path) is None
    assert tokens.list_tokens(path=path) == []


def test_revoke_unknown_raises_keyerror(path):
    with pytest.raises(KeyError):
        tokens.revoke("ghost", path=path)


def test_revoke_all(path):
    s1 = tokens.new("a", path=path)
    s2 = tokens.new("b", path=path)
    assert tokens.revoke_all(path=path) == 2
    assert tokens.verify(s1, path=path) is None
    assert tokens.verify(s2, path=path) is None


def test_name_reusable_after_revoke(path):
    old = tokens.new("phone", path=path)
    tokens.revoke("phone", path=path)
    new = tokens.new("phone", path=path)
    assert tokens.verify(old, path=path) is None
    assert tokens.verify(new, path=path) == "phone"


def test_missing_file_means_no_tokens(path):
    assert tokens.list_tokens(path=path) == []
    assert tokens.verify("anything", path=path) is None
    assert not path.exists()


@pytest.mark.parametrize("content", ["{not json", "[]", '{"tokens": 5}', '{"tokens": [1]}'])
def test_corrupt_file_raises_and_is_not_wiped(path, content):
    path.parent.mkdir(parents=True)
    path.write_text(content)
    for call in (
        lambda: tokens.verify("x", path=path),
        lambda: tokens.list_tokens(path=path),
        lambda: tokens.new("n", path=path),
    ):
        with pytest.raises(tokens.TokenStoreError):
            call()
    assert path.read_text() == content


def test_no_temp_files_left(path):
    tokens.new("a", path=path)
    assert sorted(p.name for p in path.parent.iterdir()) == [".tokens.lock", "tokens.json"]


def test_default_path_honours_xdg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert tokens.default_path() == tmp_path / "govee-cli" / "tokens.json"
    secret = tokens.new("x")
    assert tokens.verify(secret) == "x"


def test_default_path_falls_back_to_home(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert tokens.default_path() == tmp_path / ".config" / "govee-cli" / "tokens.json"


def test_pairing_link_puts_secret_in_fragment_only():
    link = tokens.pairing_link("abc_DEF-123", "192.0.2.7", 8742)
    assert link.startswith("http://192.0.2.7:8742/#")
    assert "?" not in link
    assert link.split("#", 1)[1] == "token" + "=" + "abc_DEF-123"


@pytest.mark.parametrize("bad", ["", "a#b", "a b", "a&b"])
def test_pairing_link_rejects_unsafe_secret(bad):
    with pytest.raises(ValueError):
        tokens.pairing_link(bad, "192.0.2.7")


def test_lock_file_is_0600(path):
    # colleague review: the lock file was created 0664 under the default umask.
    tokens.new("a", path=path)
    lock = path.parent / ".tokens.lock"
    assert stat.S_IMODE(os.stat(lock).st_mode) == 0o600
