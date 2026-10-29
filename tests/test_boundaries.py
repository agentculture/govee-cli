"""Spec boundaries (c2, c18): stdlib-only runtime, no BLE, no account login."""

from __future__ import annotations

import ast
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "govee_cli"


def _py_files():
    return sorted(p for p in PKG.rglob("*.py") if "__pycache__" not in p.parts)


def test_runtime_dependencies_stay_empty():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert project.get("dependencies", []) == []


def test_every_import_is_stdlib_or_first_party():
    allowed = set(sys.stdlib_module_names) | {"govee_cli", "__future__"}
    bad = []
    for path in _py_files():
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            else:
                continue
            for name in names:
                if name.split(".")[0] not in allowed:
                    bad.append(f"{path.relative_to(ROOT)}: {name}")
    assert bad == []


def test_no_ble_and_no_account_password_flow():
    # Boundary c18: API key only (no Govee account login), no BLE transport.
    forbidden = ("bleak", "bluetooth", "pygatt", "/user/login", "account_password", "getpass(")
    hits = []
    for path in _py_files():
        text = path.read_text().lower()
        hits += [f"{path.relative_to(ROOT)}: {w}" for w in forbidden if w in text]
    assert hits == []
