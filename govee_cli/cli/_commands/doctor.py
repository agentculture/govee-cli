"""``govee-cli doctor`` — check the agent-identity invariants.

Mirrors the two invariants ``steward doctor`` verifies for a mesh agent:

* **prompt-file-present** — the repo declares an agent in ``culture.yaml`` and
  has the matching prompt file on disk;
* **backend-consistency** — the declared ``backend`` is one this template
  knows, and the *resident* prompt file that backend actually reads is on disk
  (``claude`` → ``CLAUDE.md``, ``colleague`` → ``AGENTS.colleague.md``,
  ``acp``/``codex``/``copilot`` → ``AGENTS.md``, ``gemini`` → ``GEMINI.md``).

Additionally a **harness-prompts** info check reports which *other* recognized
harness prompt files are present. Those files (``AGENTS.override.md``,
``.pi/SYSTEM.md``, ``QWEN.md``) belong to interactively available harnesses
that ride the same backend name; they are never accepted as substitutes for
the resident prompt, because the Culture daemon does not read them.

Plus a **skills-present** check (the vendored ``.claude/skills/`` kit). Read-only.

Reports the rubric-shaped contract
``{healthy, checks: [{id, passed, severity, message, remediation}]}`` so the
agent-first rubric's bundle 7 passes. When run from a wheel install (no
``culture.yaml`` alongside the package), it reports a single info check and
exits 0 — there is nothing to diagnose.
"""

from __future__ import annotations

import argparse
import os
import tempfile
from typing import Any, Optional

from govee_cli.cli._commands.whoami import find_culture_yaml, read_agent_fields
from govee_cli.cli._errors import CliError
from govee_cli.cli._output import emit_result

#: Shared by every AGENTS.md-reading backend (codex, acp, copilot).
_AGENTS_MD = "AGENTS.md"

# backend → every prompt file RECOGNIZED under that backend name.
#
# Values are tuples because four harnesses occupy only three backend names in
# this template: Qwen Code runs on ``acp`` (QWEN.md), and associate/Pi runs on
# ``colleague`` (AGENTS.override.md as context plus .pi/SYSTEM.md as the system
# prompt). This is a *recognition* table — it answers "does some harness on
# this backend read this file?", which is what tooling needs when it walks a
# checkout and attributes files to backends.
#
# It is deliberately NOT the health check. See ``_RESIDENT_PROMPT`` below.
#
# This mirrors ``backends[*].prompt`` in
# ``.claude/skills/agent-config/data/backend-fingerprints.yaml``;
# ``tests/test_harness_registries.py`` asserts the two never drift apart.
_PROMPT_FILE = {
    "claude": ("CLAUDE.md",),
    "colleague": ("AGENTS.colleague.md", "AGENTS.override.md", ".pi/SYSTEM.md"),
    "acp": (_AGENTS_MD, "QWEN.md"),
    "codex": (_AGENTS_MD,),
    "copilot": (_AGENTS_MD,),
    "gemini": ("GEMINI.md",),
}

# backend → the ONE prompt file the Culture daemon reads for a resident on
# that backend. This is what ``prompt_file_present`` requires.
#
# The distinction from ``_PROMPT_FILE`` is load-bearing. Several files are
# recognized under ``colleague`` and ``acp`` because *interactive harnesses*
# ride those backend names — Pi reads ``AGENTS.override.md`` + ``.pi/SYSTEM.md``
# and Qwen Code reads ``QWEN.md`` — but the mesh resident reads neither. A
# clone declaring ``backend: colleague`` with only Pi's files on disk has no
# resident prompt at all, and treating the harness files as interchangeable
# alternatives let exactly that pass as healthy.
_RESIDENT_PROMPT = {
    "claude": "CLAUDE.md",
    "colleague": "AGENTS.colleague.md",
    "acp": _AGENTS_MD,
    "codex": _AGENTS_MD,
    "copilot": _AGENTS_MD,
    "gemini": "GEMINI.md",
}


def _diagnose() -> dict[str, object]:
    cfg = find_culture_yaml()
    if cfg is None:
        check = {
            "id": "source_checkout",
            "passed": True,
            "severity": "info",
            "message": "no culture.yaml found alongside the package; identity checks skipped",
            "remediation": "",
        }
        return {"healthy": True, "checks": [check]}

    root = cfg.parent
    fields = read_agent_fields()
    backend = fields["backend"]
    checks: list[dict[str, object]] = []

    # 1. backend-consistency: the RESIDENT prompt file for the declared
    #    backend exists. Other harness prompt files recognized under the same
    #    backend name are reported separately (check 2) and never substituted
    #    here — the mesh daemon does not read them.
    resident = _RESIDENT_PROMPT.get(backend)
    if resident is None:
        checks.append(
            {
                "id": "backend_consistency",
                "passed": False,
                "severity": "error",
                "message": f"unknown backend '{backend}' in culture.yaml",
                "remediation": f"set backend to one of: {', '.join(sorted(_RESIDENT_PROMPT))}",
            }
        )
    else:
        present = (root / resident).is_file()
        checks.append(
            {
                "id": "prompt_file_present",
                "passed": present,
                "severity": "error",
                "message": (
                    f"backend '{backend}' reads {resident} as its resident prompt — "
                    + ("present" if present else "missing")
                ),
                "remediation": "" if present else f"create {resident} at the repo root",
            }
        )

        # 2. harness-prompts: report the other recognized prompt files on
        #    disk for this backend. Informational — an interactively available
        #    harness is not a health requirement, and its absence is not a
        #    failure — but it must never stand in for the resident prompt.
        others = [
            name
            for name in _PROMPT_FILE.get(backend, ())
            if name != resident and (root / name).is_file()
        ]
        checks.append(
            {
                "id": "harness_prompts",
                "passed": True,
                "severity": "info",
                "message": (
                    "other harness prompt files on this backend: "
                    + (", ".join(others) if others else "none")
                    + " (not substitutes for the resident prompt)"
                ),
                "remediation": "",
            }
        )

    # 3. skills-present: the vendored skill kit is on disk.
    skills_dir = root / ".claude" / "skills"
    has_skills = skills_dir.is_dir() and any(skills_dir.iterdir())
    checks.append(
        {
            "id": "skills_present",
            "passed": has_skills,
            "severity": "warning",
            "message": (
                ".claude/skills/ vendored" if has_skills else ".claude/skills/ missing or empty"
            ),
            "remediation": (
                "" if has_skills else "vendor the skill kit (see docs/skill-sources.md)"
            ),
        }
    )

    healthy = all(c["passed"] for c in checks)
    return {"healthy": healthy, "checks": checks}


# --- environment checks (LAN, cloud key, stats DB) -------------------------------------------
#
# Advisory (severity "warning"): a lamp that is off the network must not make the agent identity
# report unhealthy. They are appended by ``cmd_doctor`` only; ``_diagnose`` stays identity-only.

REMEDIATION_LAN = (
    "enable LAN Control for the lamp in the Govee Home app, allow inbound UDP 4002 on this host, "
    "set GOVEE_LAN_INTERFACE to the interface on the lamp's network, and/or set GOVEE_LAN_HOSTS "
    "to the lamp's IP address (unicast)"
)
ENV_NO_SCAN = "GOVEE_DOCTOR_NO_SCAN"


def _interface_info() -> dict[str, Any]:
    from govee_cli.govee.lan import interface_info

    return interface_info()


def _lan_scan() -> dict[str, Any]:
    from govee_cli.govee.lan import LanTransport

    return LanTransport().scan_report()


def _check(cid: str, passed: bool, message: str, remediation: str = "", severity: str = "warning"):
    return {
        "id": cid,
        "passed": passed,
        "severity": severity,
        "message": message,
        "remediation": "" if passed else remediation,
    }


def _check_interface() -> dict[str, Any]:
    from govee_cli.govee.lan import LanError

    try:
        info = _interface_info()
    except (LanError, OSError) as exc:
        return _check(
            "lan-interface",
            False,
            f"cannot pick a LAN interface: {exc}",
            "set GOVEE_LAN_INTERFACE to the interface name or IPv4 address on the lamp's network",
        )
    name = info.get("interface") or "default route"
    return _check(
        "lan-interface", True, f"LAN interface {name} ({info.get('ip')}, from {info.get('source')})"
    )


def _check_discovery(scan: bool) -> dict[str, Any]:
    if not scan or os.environ.get(ENV_NO_SCAN):
        return _check(
            "lan-discovery",
            True,
            "LAN scan skipped (--no-scan or GOVEE_DOCTOR_NO_SCAN)",
            severity="info",
        )
    served = _discovery_via_serve()
    if served is not None:
        return served
    report = _lan_scan()
    tried = ", ".join(report.get("methods_tried") or []) or "none"
    if report.get("replied"):
        found = "; ".join(
            f"{d['name']} ({d['model']}) at {d['ip']} via {d['method']}" for d in report["devices"]
        )
        return _check("lan-discovery", True, f"found {found}; methods tried: {tried}")
    why = f" ({report['error']})" if report.get("error") else ""
    return _check(
        "lan-discovery",
        False,
        f"no lamp answered on the LAN{why}; methods tried: {tried}",
        REMEDIATION_LAN,
    )


def _discovery_via_serve() -> Optional[dict[str, Any]]:
    """While ``govee serve`` runs it owns UDP 4002, so ask it instead of scanning (None: not up)."""
    from govee_cli.govee.service import LampService
    from govee_cli.govee.service_serve import serve_base_url, serve_running

    base = serve_base_url()
    if not serve_running(base):
        return None
    try:
        devices = LampService.connect().devices()
    except CliError as exc:
        return _check(
            "lan-discovery", False, f"govee serve at {base}: {exc.message}", exc.remediation
        )
    if not devices:
        return _check(
            "lan-discovery", False, f"govee serve at {base} sees no lamp", REMEDIATION_LAN
        )
    found = "; ".join(f"{d.name} ({d.model}) via {'/'.join(d.transports)}" for d in devices)
    return _check("lan-discovery", True, f"found {found} through govee serve at {base}")


def _check_api_key() -> dict[str, Any]:
    present = bool(os.environ.get("GOVEE_API_KEY", "").strip())  # presence only, never the value
    return _check(
        "api-key-present",
        present,
        (
            "GOVEE_API_KEY is set"
            if present
            else "GOVEE_API_KEY is not set (cloud API and scenes off)"
        ),
        "set GOVEE_API_KEY (request a key in the Govee Home app) to enable scenes and the cloud",
    )


def _check_stats_db() -> dict[str, Any]:
    from govee_cli.stats import store

    path = store.db_path()
    try:
        conn = store.connect()
        conn.close()
        probe = tempfile.NamedTemporaryFile(dir=path.parent, prefix=".doctor.")
        probe.close()
    except Exception as exc:  # noqa: BLE001 - sqlite3.Error or OSError; report, never crash doctor
        return _check(
            "stats-db-writable",
            False,
            f"stats database {path} is not writable ({type(exc).__name__})",
            "make the directory writable or point XDG_STATE_HOME at a writable location",
        )
    return _check("stats-db-writable", True, f"stats database {path} is writable")


def _environment_checks(scan: bool = True) -> list[dict[str, Any]]:
    return [_check_interface(), _check_discovery(scan), _check_api_key(), _check_stats_db()]


def cmd_doctor(args: argparse.Namespace) -> int:
    report = _diagnose()
    report["checks"] = list(report["checks"]) + _environment_checks(
        scan=not getattr(args, "no_scan", False)
    )
    # Environment checks are advisory; only identity checks decide health.
    report["healthy"] = all(
        c["passed"] for c in report["checks"] if c["severity"] != "warning"
    ) and (report["healthy"])
    json_mode = bool(getattr(args, "json", False))
    if json_mode:
        emit_result(report, json_mode=True)
    else:
        status = "healthy" if report["healthy"] else "unhealthy"
        lines = [f"govee-cli doctor: {status}", ""]
        for check in report["checks"]:
            mark = "ok" if check["passed"] else "FAIL"
            lines.append(f"[{mark}] {check['id']}: {check['message']}")
            if not check["passed"] and check["remediation"]:
                lines.append(f"  hint: {check['remediation']}")
        emit_result("\n".join(lines), json_mode=False)
    return 0 if report["healthy"] else 1


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "doctor",
        help="Check the agent-identity invariants (prompt-file-present, backend-consistency).",
    )
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")
    p.add_argument(
        "--no-scan",
        action="store_true",
        help="Skip the LAN scan (also: GOVEE_DOCTOR_NO_SCAN=1).",
    )
    p.set_defaults(func=cmd_doctor)
