# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

`govee-cli` is the **govee-cli** AgentCulture mesh agent: an agent-first CLI that
controls Govee smart lights at home — discover devices, on/off, brightness,
color, color temperature and scenes — via Govee's official Developer API (cloud)
and the local LAN API. Writes are **safe by default** (dry-run unless `--apply`)
and every command emits `--json` for agent consumers.

**Current state:** the repo was scaffolded from `culture-agent-template`
(0.10.0) and so far carries only that template's agent-first skeleton — the
identity/introspection verbs (`whoami`, `learn`, `explain`, `overview`,
`doctor`, `cli overview`), the harness prompt files, the vendored skill kit, and
CI/deploy. **No Govee device code exists yet** (see [Roadmap](#roadmap)). Several
in-code strings still describe the package as "a clonable template for
AgentCulture mesh agents" — the parser `description` in
`govee_cli/cli/__init__.py`, `_TEXT`/`_as_json_payload` in `learn.py`, `_ROOT`
in `explain/catalog.py`, and `_ARTIFACTS` in `overview.py` (which also names the
wrong resident prompt, `AGENTS.colleague.md`). Update them as the domain verbs
land.

Siblings in the Organic Development framework: `guildmaster` (skills
supplier), `steward` (alignment — `steward doctor`), and `teken` (the afi-cli
scaffolder this CLI is cited from).

## Commands

```bash
uv sync                                    # install (runtime has zero deps; dev group adds pytest, linters, teken)
uv run govee whoami                        # run the CLI — the console script is `govee`
uv run python -m govee_cli whoami          # equivalent module entry point

uv run pytest -n auto                      # full suite (what culture.yaml's gate and CI run)
uv run pytest tests/test_cli.py::test_whoami_json -v   # single test
uv run pytest -n auto --cov=govee_cli --cov-report=term  # with coverage (fail_under = 60)
```

**The installed command is `govee`** (`[project.scripts]` in `pyproject.toml`),
while argparse's `prog`, help text, tests, and docs say `govee-cli`. `uv run
govee-cli …` fails with "Failed to spawn". Tests call `govee_cli.cli.main([...])`
in-process, so they never exercise the script name.

Lint — exactly what the CI `lint` job runs:

```bash
python3 scripts/scan-secrets.py            # committed secrets + non-localhost endpoints in JSON config
uv run black --check govee_cli tests
uv run isort --check-only govee_cli tests
uv run flake8 govee_cli tests
uv run bandit -c pyproject.toml -r govee_cli
markdownlint-cli2 "**/*.md" "#node_modules" "#.local" "#.claude/skills" "#.teken"
uv run teken cli doctor . --strict         # agent-first rubric gate
```

Harness smoke check (CI `harness-smoke` job): `uv run python scripts/harness-smoke.py
--stage all --require config`. Only the offline `config` stage runs on a stock
runner; `live`/`toolchain` report SKIP / NOT VERIFIED rather than passing.

Line length is 100 for black, isort, and flake8.

## CLI architecture

The CLI is cited (cite-don't-import) from teken's `python-cli` reference, so the
**runtime package has no third-party dependencies** — keep it that way. The
Govee clients (HTTPS to the Developer API, UDP to the LAN API) belong on the
stdlib (`urllib`, `socket`, `json`). `culture.yaml` is parsed by hand in
`whoami.py` for the same reason, and `pyyaml` is a dev-only dependency.

The parts that span several files:

- **Registration.** `govee_cli/cli/__init__.py::_build_parser` imports each
  module under `cli/_commands/` and calls its `register(sub)`. A module adds its
  subparser, its own `--json` flag, and `set_defaults(func=handler)`. New noun
  groups (e.g. a future `devices` or `light`) register there the same way;
  `_commands/cli.py` is the worked example of a noun with a sub-verb.
- **Error contract.** Handlers return `None`/an int exit code, or **raise
  `CliError(code, message, remediation)`** (`cli/_errors.py`). `_dispatch`
  catches it and wraps any other exception into a `CliError`, so a traceback
  never reaches the user. Exit codes: `0` success, `1` user error, `2`
  environment error (use this for an unreachable device or API, or a missing
  API key), `3+` reserved.
- **Argparse errors honour `--json`.** `_CliArgumentParser.error()` routes parse
  errors through `emit_error`. Because parsing fails before `args.json` exists,
  `main()` pre-scans raw argv for `--json` into the class-level `_json_hint`.
  Subparsers inherit the class via `parser_class=` — pass `parser_class=type(p)`
  when adding nested subparsers, as `cli.py` does.
- **Output split.** `cli/_output.py`: `emit_result` → stdout, `emit_error` /
  `emit_diagnostic` → stderr, never mixed. Text-mode errors print
  `error: …` then `hint: …`; the `hint:` line is required by the rubric.
- **`explain` catalog.** `govee_cli/explain/catalog.py` maps command-path tuples
  to markdown (`ENTRIES`). Add an entry for every new noun/verb. The test only
  checks that existing entries resolve, so a missing entry is caught by the
  `teken cli doctor` rubric gate rather than pytest.
- **Rubric-shaped verbs.** `teken cli doctor . --strict` checks, among other
  things, that `learn` prints at least 200 characters and covers purpose, the
  command map, exit codes, `--json` and `explain`; that any noun with
  action-verbs also exposes `overview`; that descriptive verbs never hard-fail
  on a bad path (`overview <bogus>` exits 0); and that `doctor --json` has the
  `{healthy, checks: [{id, passed, severity, message, remediation}]}` shape.
  When you add domain verbs, update `learn` (both text and JSON payloads),
  `overview`'s `_VERBS`, and the catalog together.
- **Identity resolution.** `whoami.find_culture_yaml()` walks up from the
  module's `__file__`, not the cwd, so the identity is always this agent's own.
  From a wheel install there is no `culture.yaml`: `whoami` falls back to
  literals and `doctor` reports a single info check and exits 0.
- **`doctor` registries.** `_PROMPT_FILE` (every prompt file *recognized* per
  backend) mirrors `.claude/skills/agent-config/data/backend-fingerprints.yaml`,
  and `tests/test_harness_registries.py` fails if they drift. `_RESIDENT_PROMPT`
  is the single file the Culture daemon reads per backend. A harness prompt such
  as `QWEN.md` or `AGENTS.override.md` must never be accepted as a substitute
  for it — `tests/test_doctor_resident_prompt.py` pins that.

## Govee domain constraints

These come from the project description. They are design rules for the code
still to be written, not for code that exists:

- **Mutating verbs dry-run by default.** On/off, brightness, color, color
  temperature and scene changes print what *would* be sent and exit 0; only
  `--apply` performs the write. Read-only verbs (discovery, state) need no flag.
- **Every verb supports `--json`** and keeps the stdout/stderr split above.
- **Two transports:** the Govee Developer API (cloud, needs an API key) and the
  LAN API (local network, no key).
- **Never commit the API key or a real device endpoint.** `scripts/scan-secrets.py`
  fails CI on credential-shaped strings in any tracked file. It also fails on any
  *JSON* file with a `baseUrl`/`endpoint`/`url`/`host` key pointing at a
  non-localhost `http(s)://` URL. Keep API base URLs in Python source or read
  them from the environment, not in committed JSON config. Add test fixtures
  that need such values to `SELF_EXCLUDE` only deliberately.

## Harnesses and identity

`culture.yaml` declares `suffix: govee-cli`, `backend: claude`, so **this file is
the mesh resident's prompt**. `govee doctor` and `steward doctor` check
*prompt-file-present* and *backend-consistency* (`claude` ↔ `CLAUDE.md`).
`culture.yaml` also has a `gate` section (`setup: [uv, sync]`, `test: [uv, run,
pytest, -n, auto]`): argv lists that external tooling runs. Keep it accurate if
the test command changes.

Four harnesses are live interactively over the same clone, each reading exactly
one root file, with **deliberately no `AGENTS.md`**:

| Harness | Reads |
|---------|-------|
| Claude Code | `CLAUDE.md` |
| Pi / associate | `AGENTS.override.md` (context) + `.pi/SYSTEM.md` (replaces Pi's system prompt; read-only `associate` lane) |
| colleague | `AGENTS.colleague.md` |
| Qwen Code | `QWEN.md` |

The interactive harness (which binary you run) and the mesh resident (the
`backend` in `culture.yaml`) are separate selections; see
`docs/harness-selection.md`. Forcing a harness is invocation-level only (see
`docs/automation-contract.md`) and never rewrites `culture.yaml`.

When editing these files:

- Keep all four describing the same project. When the domain or conventions
  change here, update `AGENTS.override.md`, `AGENTS.colleague.md` and `QWEN.md`
  in the same PR, plus `.pi/SYSTEM.md` if the `associate` lane changes.
- **Don't add a root `AGENTS.md`.** It would shadow the colleague and Pi
  cascades, and `scripts/harness-smoke.py` fails on it.
- The live probes in `docs/harness-invocations.yaml` depend on content: Claude
  and Qwen are asked whether `CLAUDE.md` / `QWEN.md` describe the project as
  `govee-cli`. The Pi probe relies on the `lobes-cli` attribution appearing
  **only** in `.pi/SYSTEM.md` — never mention it in `AGENTS.override.md`.
- `.qwen/skills`, `.pi/skills` and `.colleague/skills` are relative symlinks to
  `.claude/skills`. Tests assert this wiring.

## Skills

`.claude/skills/` vendors the guildmaster skill kit (19 skills,
cite-don't-import). Provenance and the re-sync procedure live in
`docs/skill-sources.md`. **Never edit or reformat vendored skill files**; a fix
goes upstream and is re-synced. Every `SKILL.md` must keep `type: command` in
its frontmatter because the culture `core.skill_loader` silently skips skills
without it. Per-machine skill config: copy `.claude/skills.local.yaml.example`
to the git-ignored `skills.local.yaml`.

Tooling prerequisites on PATH: **`devex`** (the `cicd` skill delegates the PR
lifecycle to `devex pr`) and **`agtag`** (`communicate`); **`colleague`** is
optional and only needed when `ask-colleague` runs.

## Conventions

- **Every PR bumps the version**, including docs/config/CI-only PRs. Use the
  `version-bump` skill: it updates `pyproject.toml` and prepends a Keep a
  Changelog entry to `CHANGELOG.md`. CI's `version-check` job blocks merge
  otherwise. `__version__` is read from package metadata, so there is no second
  version string to edit.
- **PRs** go through the `cicd` skill (`devex pr` + SonarCloud gating; the
  project key is `agentculture_govee-cli`). The `cicd`/`communicate` scripts
  sign posts as `- govee-cli (Claude)`, resolving the nick from `culture.yaml`.
- **Deploy:** a push to `main` that touches `pyproject.toml` or `govee_cli/**`
  publishes to PyPI via Trusted Publishing (`.github/workflows/publish.yml`).
  Same-repo PRs publish a `.devN` build to TestPyPI. The `pypi`/`testpypi`
  GitHub environments and the PyPI Trusted Publisher must be configured first.
- **Reach for `ask-colleague` reflexively.** Before presenting or opening a PR
  on a non-trivial committed diff, run `review` for a second opinion from a
  different model. For a fresh read of an unfamiliar area, run `explore`. Both
  are read-only (throwaway worktree). `write --apply`/`--pr` needs the user's
  go-ahead. Treat its output as a second opinion to verify, not authority.
- **Keep docs grounded in what's on disk.** If a section describes work that
  hasn't landed, mark it `(planned)` or move it under a Roadmap heading.

### Worktrees

Every worktree you create by hand goes in
`../.worktrees.govee-cli/<name>/`, never a shared `../worktrees/`:

```bash
git worktree add ../.worktrees.govee-cli/<name> -b <scoped-prefix>/<name>
```

Scope branch prefixes to the work (`lan/discovery`, not `agent/t2`). The
vendored `assign-to-workforce` skill's example uses `../worktrees/` and
`agent/<task-id>`; override both when you follow it, without editing the skill.
`ask-colleague`'s own throwaway worktrees under `${TMPDIR:-/tmp}` are exempt,
because they are deleted on exit. Remove a worktree with
`git worktree remove <path>`. `git worktree prune` only clears metadata, and you
should never `rm -rf` a worktree you didn't create.

### Memory

`/recall` before non-trivial work and `/remember` when a non-obvious decision,
constraint or gotcha comes up. The vendored `remember.sh` defaults records to
scope `govee-cli` with **`--visibility public`**, which writes them to the
committed, team- and mesh-shared `.eidetic/memory/` in this repo. This
overrides the "private" default that the skill's own description claims; trust
the script. Pass `--visibility private` to keep a record in `$HOME` and out of
git. Don't store what the repo already records.

The one record currently committed in `.eidetic/memory/default__public.jsonl`
was inherited from the template. It is about `eidetic-cli`, not this project.

## Roadmap

(planned) The Govee domain itself: device discovery, then on/off, brightness,
color, color temperature and scenes, over both the Developer API and the LAN
API, following the [domain constraints](#govee-domain-constraints) above.
