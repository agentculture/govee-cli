"""t8: ``govee serve`` and ``govee mcp serve`` import their servers lazily."""

from __future__ import annotations

import json
import sys
import types

import pytest

from govee_cli.cli import main


def fake_module(monkeypatch, name, run):
    mod = types.ModuleType(name)
    mod.run = run
    monkeypatch.setitem(sys.modules, name, mod)


def test_serve_passes_args(monkeypatch):
    seen = {}

    def run(**kw):
        seen.update(kw)
        return 0

    fake_module(monkeypatch, "govee_cli.web.server", run)
    assert main(["serve", "--host", "127.0.0.1", "--port", "9001", "--rotate-token"]) == 0
    assert seen == {"host": "127.0.0.1", "port": 9001, "rotate_token": True}


def test_serve_defaults_leave_host_to_server(monkeypatch):
    seen = {}
    fake_module(monkeypatch, "govee_cli.web.server", lambda **kw: seen.update(kw) or 0)
    assert main(["serve"]) == 0
    assert seen == {"rotate_token": False}


def test_serve_missing_module_is_env_error(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "govee_cli.web.server", None)
    rc = main(["serve", "--json"])
    assert rc == 2
    assert json.loads(capsys.readouterr().err)["code"] == 2


def test_mcp_serve(monkeypatch):
    fake_module(monkeypatch, "govee_cli.mcp.server", lambda: 7)
    assert main(["mcp", "serve"]) == 7


def test_mcp_serve_missing_module(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "govee_cli.mcp.server", None)
    assert main(["mcp", "serve"]) == 2
    assert "hint:" in capsys.readouterr().err


def test_mcp_overview(capsys):
    assert main(["mcp", "overview"]) == 0
    assert "mcp" in capsys.readouterr().out


def test_serve_importing_nothing_at_parse_time():
    # building the parser must not import the server modules
    for m in ("govee_cli.web.server", "govee_cli.mcp.server"):
        sys.modules.pop(m, None)
    from govee_cli.cli import _build_parser

    _build_parser()
    assert "govee_cli.web.server" not in sys.modules
    assert "govee_cli.mcp.server" not in sys.modules


@pytest.mark.parametrize("verb", ["devices", "stats", "serve", "light", "token", "mcp"])
def test_verbs_in_help(verb, capsys):
    with pytest.raises(SystemExit):
        main(["--help"])
    assert verb in capsys.readouterr().out


def test_every_verb_has_an_explain_entry():
    import argparse

    from govee_cli.cli import _build_parser
    from govee_cli.explain import known_paths

    known = set(known_paths())
    missing = []

    def walk(parser, path):
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, sp in action.choices.items():
                    p = (*path, name)
                    if p not in known:
                        missing.append(p)
                    walk(sp, p)

    walk(_build_parser(), ())
    assert not missing, missing
