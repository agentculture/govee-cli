"""Integration artifacts: culture-rules actor, systemd unit and MCP config."""

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BIN = "/opt/govee/bin/govee"  # any absolute path: `govee rules actor` fills in the real one
UNIT = ROOT / "docs" / "systemd" / "govee-serve.service"
MCP_JSON = ROOT / ".mcp.json"

# Mirrors culture_rules/actors/inline_guard.py (_SHELLS, _EVAL_FLAGS keys, _WRAPPERS).
REFUSED = {
    "sh", "bash", "zsh", "dash", "ksh", "mksh", "ash", "fish", "csh", "tcsh",
    "python", "python3", "pypy", "node", "nodejs", "bun", "perl", "ruby", "php", "lua",
    "su", "runuser", "env", "sudo", "doas", "nice", "nohup", "setsid", "timeout",
    "stdbuf", "ionice", "chrt", "taskset", "time", "command", "exec", "busybox",
    "xargs", "flock", "chroot",
}  # fmt: skip

EXPECTED = {
    "lamp_on": {},
    "lamp_fade_on": {"seconds": "int"},
    "lamp_fade_off": {"seconds": "int"},
    "lamp_fade_on_to": {"pct": "int", "seconds": "int"},
    "lamp_off": {},
    "lamp_brightness": {"pct": "int"},
    "lamp_color": {"hex": "string"},
    "lamp_temp": {"kelvin": "int"},
    "lamp_scene": {"name": "string"},
}


def _actor():
    from govee_cli.cli._commands.rules import actor_definition

    return actor_definition(BIN, "spark")


def test_actor_is_runner_on_spark():
    actor = _actor()
    assert actor["kind"] == "runner"
    assert actor["machine"] == "spark"
    assert actor["id"]
    assert actor["name"]


def test_actor_has_the_eight_commands_with_params():
    commands = _actor()["params"]["commands"]
    assert set(commands) == set(EXPECTED)
    for name, params in EXPECTED.items():
        assert commands[name]["params"] == params, name
        assert commands[name]["timeout"] > 0


def test_every_argv_is_absolute_govee_applying_json_and_not_an_interpreter():
    for name, cmd in _actor()["params"]["commands"].items():
        argv = cmd["argv"]
        assert os.path.isabs(argv[0]), name
        assert os.path.basename(argv[0]) == "govee", name
        assert os.path.basename(argv[0]).rstrip("0123456789.") not in REFUSED
        assert argv[-2:] == ["--apply", "--json"], name
        for placeholder in cmd["params"]:
            assert "{%s}" % placeholder in argv, name


def test_no_secret_literals_in_actor():
    assert "GOVEE_API_KEY=" not in json.dumps(_actor())


def test_no_actor_json_with_a_machine_path_is_committed():
    # The shared portability lint fails committed /home/<user>/ paths (PR #3 CI).
    assert not (ROOT / "docs" / "culture-rules" / "govee-actor.json").exists()


def test_mcp_json_runs_govee_mcp_serve():
    server = json.loads(MCP_JSON.read_text())["mcpServers"]["govee"]
    assert server["command"] == "govee"
    assert server["args"] == ["mcp", "serve"]
    assert "env" not in server


def test_unit_restarts_on_failure_with_env_file_outside_repo():
    text = UNIT.read_text()
    assert "Restart=on-failure" in text
    env_lines = [ln for ln in text.splitlines() if ln.startswith("EnvironmentFile=")]
    assert env_lines
    for line in env_lines:
        path = line.split("=", 1)[1].lstrip("-")
        assert path.startswith("%h/")
        assert "git" not in path
    assert "ExecStart=%h/.local/bin/govee serve" in text
    assert "WantedBy=default.target" in text


def test_every_command_logs_as_rules():
    for name, cmd in _actor()["params"]["commands"].items():
        argv = cmd["argv"]
        i = argv.index("--source")
        assert argv[i + 1] == "rules", name
