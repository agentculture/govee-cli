"""Static checks for the web console assets shipped under govee_cli/web/static."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "govee_cli" / "web" / "static"
FILES = ("index.html", "app.js", "app.css")


def _read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("name", FILES)
def test_static_file_exists(name: str) -> None:
    assert (STATIC / name).is_file(), f"missing govee_cli/web/static/{name}"


@pytest.mark.parametrize("name", FILES)
def test_static_file_is_pure_ascii(name: str) -> None:
    data = (STATIC / name).read_bytes()
    bad = [i for i, b in enumerate(data) if b > 0x7F]
    assert not bad, f"{name} has non-ASCII bytes at offsets {bad[:5]}"


def test_index_is_full_document() -> None:
    html = _read("index.html")
    assert html.lstrip().lower().startswith("<!doctype html>")
    assert '<meta charset="utf-8">' in html.lower()
    assert 'name="viewport"' in html
    assert "<title>Govee Lamp Console</title>" in html


def test_index_references_assets_relatively() -> None:
    html = _read("index.html")
    assert re.search(r'<link[^>]+rel="stylesheet"[^>]+href="app\.css"', html)
    assert re.search(r'<script[^>]+src="app\.js"', html)


@pytest.mark.parametrize("name", FILES)
def test_no_quiet_hours(name: str) -> None:
    text = _read(name).lower()
    assert "quiet hours" not in text
    assert "quiet-hours" not in text
    assert "quiet_hours" not in text


def test_index_has_no_quiet_text() -> None:
    # The CSS token --quiet is a colour name; the markup itself must not mention it.
    assert "quiet" not in _read("index.html").lower()


def test_no_external_scripts() -> None:
    html = _read("index.html")
    for src in re.findall(r'<script[^>]*\ssrc="([^"]+)"', html):
        assert not re.match(r"^(https?:)?//", src), f"external script: {src}"


def test_only_google_fonts_stylesheet() -> None:
    html = _read("index.html")
    hrefs = re.findall(r'<link[^>]*rel="stylesheet"[^>]*href="([^"]+)"', html)
    hrefs += re.findall(r'<link[^>]*href="([^"]+)"[^>]*rel="stylesheet"', html)
    external = {re.sub(r"^(https?:)?//([^/]+).*$", r"\2", h) for h in hrefs if "//" in h}
    assert external <= {"fonts.googleapis.com"}, external
    css = _read("app.css")
    assert "@import" not in css


def test_hidden_rule_kept() -> None:
    css = _read("app.css").replace(" ", "")
    assert "[hidden]{display:none!important;}" in css


@pytest.mark.parametrize(
    "route", ["/api/state", "/api/light", "/api/scene", "/api/scenes", "/api/stats", "/api/events"]
)
def test_app_calls_api_routes(route: str) -> None:
    assert f'"{route}' in _read("app.js")


def test_app_uses_bearer_token_from_local_storage() -> None:
    js = _read("app.js")
    assert "localStorage" in js
    assert "Bearer " in js
    assert "#token=" in js or "token=" in js


def test_no_sample_data_left() -> None:
    js = _read("app.js")
    for marker in ("spark-daria", "Desk lamp", "rnd()", "H6008"):
        assert marker not in js, f"sample data marker {marker!r} still in app.js"
