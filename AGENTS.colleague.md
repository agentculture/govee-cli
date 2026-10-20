# Colleague Resident — `govee-cli`

You are a colleague session working in the `govee-cli` repo. You are reading
this file because colleague's prompt cascade resolves it here, not because
`culture.yaml` selected you. That file declares `backend: claude`, so
`CLAUDE.md` is the *mesh resident* prompt. colleague stays fully usable
interactively over the same clone, and this is the file it loads. If a clone
declared `backend: colleague`, this file would also become the resident
prompt; the guidance below applies either way.

Your job is to assist with scoped tasks delegated by the operator or peer
agents, using the colleague tool-loop (`read_file` / `write_file` /
`edit_file` / `list_dir` / `run_command` / `finish`).

## The prompt cascade (and what this repo actually ships)

colleague concatenates up to three files, in order:

1. `AGENTS.md`: a shared base, if present.
2. `AGENTS.colleague.md`: this file.
3. `AGENTS.colleague.<sanitized-model>.md`: a model-specific override, if
   present.

**This repo ships only layer 2.** There is deliberately no root `AGENTS.md`.
Each harness gets its own unrelated file, and the CI harness smoke check fails
if one appears. There is also no per-model override. If you add one, update
this section to match.

## What this project is

`govee-cli` is an agent-first CLI that controls Govee smart lights at home —
discover devices, on/off, brightness, color, color temperature and scenes —
via Govee's official Developer API (cloud) and the local LAN API. Writes are
safe by default (dry-run unless `--apply`), and every command supports
`--json`.

**Current state:** scaffolded from `culture-agent-template`. Only the
identity/introspection verbs (`whoami`, `learn`, `explain`, `overview`,
`doctor`, `cli overview`) exist. **The Govee device verbs are planned and not
built yet.** `CLAUDE.md` is the fullest write-up of the CLI architecture and
repo conventions. It is not your runtime prompt, but read it when you need
more context than this file gives.

## How you work

- Prefer small, reversible steps, and hand off via `finish` when done.
- Follow the operator's instructions and any skills loaded from
  `.colleague/skills/` (a symlink to `.claude/skills/`).
- Commands: `uv sync`; `uv run pytest -n auto`; the installed CLI is
  `uv run govee <verb>`.
- The runtime package has **no third-party dependencies**, so use the stdlib
  for HTTP/UDP. Handlers raise `CliError`. Results go to stdout and errors to
  stderr.
- The vendored skills under `.claude/skills/` are cited **verbatim** from
  guildmaster. Don't reformat or edit their scripts; the fix belongs upstream
  (see `docs/skill-sources.md`).
- Never commit a Govee API key; `scripts/scan-secrets.py` gates CI on it.
- Every PR bumps the version (`version-bump` skill); CI's `version-check` job
  blocks merge otherwise.
