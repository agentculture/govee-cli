# AGENTS.override.md

This file is the **context layer** for the Pi harness (the `pi` CLI, and the
`associate` non-coding harness modelled on it) when it runs inside this repo.
Pi's context loader concatenates `AGENTS.md` or `CLAUDE.md` from its user-level
config directory, each parent directory, and the working directory. An
`AGENTS.override.md` in a directory replaces that directory's
`AGENTS.md`/`CLAUDE.md` entry outright instead of adding to it. That is why
this repo ships this file instead of an `AGENTS.md`: Pi must **not** inherit
`CLAUDE.md`, the Claude Code guidance file. `CLAUDE.md` assumes a coding
session with full repo-write authority, and Pi's non-coding lane has no such
authority.

The identity and behavioral limits for that lane live one layer up, in Pi's
**system prompt** file, [`.pi/SYSTEM.md`](.pi/SYSTEM.md). That file replaces
Pi's default system prompt entirely. This file is project *context* only: what
the repo is and how it is laid out, not who is reading it.

## What this project is

`govee-cli` is an AgentCulture mesh agent: an agent-first CLI that controls
Govee smart lights at home — discover devices, on/off, brightness, color,
color temperature and scenes — via Govee's official Developer API (cloud) and
the local LAN API. Writes are safe by default (dry-run unless `--apply`), and
every command supports `--json`.

**Current state:** the Govee domain is built and was tested against the real
lamp (a Govee H6076 floor lamp). Verbs: `devices`, `light` (on, off,
brightness, color, temp, scene, state, scenes, fade-status, fade-stop),
`stats`, `token`, `serve` (web console and API) and `mcp serve` (MCP over
stdio), plus the identity verbs (`whoami`, `learn`, `explain`, `overview`,
`doctor`, `cli overview`). Every lamp operation goes through one module,
`govee_cli/govee/service.py`. Writes are dry runs unless `--apply`; the web
console needs a paired token for every call (except from networks the owner
passes to `govee serve --trust`); schedules live in culture-rules,
not here.

## Four harnesses, four files, no shared base

- **Claude Code** reads [`CLAUDE.md`](CLAUDE.md).
- **Pi / associate** reads this file for context, plus
  [`.pi/SYSTEM.md`](.pi/SYSTEM.md) for its system prompt.
- **colleague** reads [`AGENTS.colleague.md`](AGENTS.colleague.md).
- **Qwen Code** reads [`QWEN.md`](QWEN.md).

If you are reading this as a human, `CLAUDE.md` is the fullest write-up of the
repo's conventions; read it first.

## Identity

`culture.yaml` declares `suffix: govee-cli`, `backend: claude`, so `CLAUDE.md`
is the live mesh resident prompt. A Pi session in this repo is a **local tool
session**, not the mesh resident. It reads this file and `.pi/SYSTEM.md`
whatever `culture.yaml` declares.

## Layout (what you can read/find/summarize here)

```text
govee_cli/                agent-first CLI (cited from teken's python-cli reference)
  cli/                    parser, error/output contract, _commands/ (one module per verb)
  explain/                markdown catalog for `explain`
  govee/                  LampService (the one path to the lamp), LAN + cloud transports, fades
  web/                    `govee serve`: HTTP API, static console, state poller
  mcp/                    MCP server over stdio
  auth/                   named, hashed pairing tokens
  stats/                  sqlite event log and state samples
tests/                    pytest suite
scripts/                  harness-smoke.py, scan-secrets.py (CI gates)
.claude/skills/           vendored guildmaster skill kit (cite-don't-import)
docs/                     specs, plans, MCP + culture-rules + systemd docs, harness docs
culture.yaml              mesh identity (suffix + backend) and test gate
.github/workflows/        tests + deploy (PyPI Trusted Publishing)
```

## Conventions worth knowing before you answer a question about this repo

- The installed console script is `govee`, though help text and docs call the
  tool `govee-cli`.
- The runtime package has no third-party dependencies.
- The vendored skills under `.claude/skills/` are cited **verbatim** from
  guildmaster. Never propose editing their scripts; the fix belongs upstream
  (`docs/skill-sources.md` has the re-sync procedure).
- Every PR bumps the version (`version-bump` skill), and CI's `version-check`
  job blocks merge otherwise. SonarCloud must also show no unresolved issue on the PR
  (`scripts/sonar-pr-issues.py`): fix each one, or accept it in SonarCloud with a
  reason; accepted and false-positive issues pass.
- This file describes the repo **as it exists on disk today**. If you are
  asked to update it, keep claims grounded in checked-in reality.
