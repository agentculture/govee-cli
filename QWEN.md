# QWEN.md

This file provides guidance to Qwen Code when working with code in this
repository. Qwen Code's context loader reads exactly `QWEN.md` and `AGENTS.md`
in a directory. This repo deliberately ships only `QWEN.md`, with no
`AGENTS.md` (each harness gets its own file; see "Prompt files by harness"
below), so this file is the only project guidance a Qwen Code session gets.

## What this project is

`govee-cli` is the **govee-cli** AgentCulture mesh agent: an agent-first CLI
that controls Govee smart lights at home — discover devices, on/off,
brightness, color, color temperature and scenes — via Govee's official
Developer API (cloud) and the local LAN API. Writes are safe by default
(dry-run unless `--apply`), and every command supports `--json`.

**Current state:** the Govee domain is built and was tested against the real
lamp (a Govee H6076 floor lamp). Verbs: `devices`, `light` (on, off,
brightness, color, temp, scene, state, scenes, fade-status, fade-stop),
`stats`, `token`, `serve` (web console and API) and `mcp serve` (MCP over
stdio), plus the identity verbs (`whoami`, `learn`, `explain`, `overview`,
`doctor`, `cli overview`). Every lamp operation goes through one module,
`govee_cli/govee/service.py`.

## Prompt files by harness

This repo's root carries one prompt file per agent harness, and each is read
by exactly one of them. There is no shared base file to inherit from:

- **Claude Code** → [`CLAUDE.md`](CLAUDE.md): the fullest write-up and the
  mesh resident's prompt. Read it for CLI architecture and workflow details.
- **Pi / associate** → [`AGENTS.override.md`](AGENTS.override.md) for context,
  plus [`.pi/SYSTEM.md`](.pi/SYSTEM.md) for its system prompt.
- **colleague** → [`AGENTS.colleague.md`](AGENTS.colleague.md).
- **Qwen Code** → this file.

Do not add a root `AGENTS.md`: it would shadow the other cascades, and the CI
harness smoke check fails on it.

## Identity

`culture.yaml` declares `suffix: govee-cli`, `backend: claude`, so the mesh
runtime reads `CLAUDE.md`, not this file. A Qwen Code session here is a
separate, local tool session. It reads `QWEN.md` whatever `culture.yaml`
declares, and running Qwen Code does not require or change that declaration.

## Commands

```bash
uv sync
uv run pytest -n auto                         # full suite
uv run pytest tests/test_cli.py::test_whoami_json -v   # single test
uv run govee whoami                           # the console script is `govee`
GOVEE_DOCTOR_NO_SCAN=1 uv run teken cli doctor . --strict  # rubric gate, no LAN scan
```

Lint (CI): `python3 scripts/scan-secrets.py`, then `black --check`,
`isort --check-only` and `flake8` over `govee_cli tests` (line length 100),
`bandit -c pyproject.toml -r govee_cli`, and markdownlint.

## The CLI

Cited (cite-don't-import) from teken's `python-cli` reference. The runtime
package has **no third-party dependencies**; the Govee clients use the stdlib
(`socket` for the LAN API, `urllib` for the cloud API). Every command supports
`--json`. Results go to stdout and errors/diagnostics to stderr. Exit codes are
`0` success, `1` user error, `2` environment error (lamp unreachable, missing
key, rate limited), and `3+` reserved. Handlers raise `CliError(code, message,
remediation)`. New verbs register through a `register(sub)` function in
`govee_cli/cli/_commands/` and need an `explain` catalog entry. Writes are dry
runs unless `--apply`; tests never touch the real network
(`tests/conftest.py`).

## Conventions

- **Every PR bumps the version** (`version-bump` skill); the `version-check`
  CI job blocks merge otherwise.
- The vendored skills under `.claude/skills/` are cited **verbatim** from
  guildmaster. Don't reformat or edit their scripts; re-sync instead (see
  `docs/skill-sources.md`).
- **Never commit a Govee API key.** `scripts/scan-secrets.py` fails CI on
  credential-shaped strings and on non-localhost URLs in JSON config.
- **Deploy:** pushing to `main` publishes to PyPI via Trusted Publishing;
  PRs do a TestPyPI dry-run.

This file describes the repository **as it exists on disk today**. When you
edit it, keep claims grounded in checked-in reality and mark anything not yet
built as `(planned)`. For the full workflow conventions (worktree layout,
memory discipline, `ask-colleague` usage), see [`CLAUDE.md`](CLAUDE.md).
