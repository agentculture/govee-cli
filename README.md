# govee-cli

Agent-first CLI that controls Govee smart lights at home: discover devices, on/off, brightness, color, color temperature and scenes, via Govee's official Developer API and the local LAN API. Safe-by-default writes (dry-run unless --apply), --json output for agents.

> **Status: early scaffold.** The repo was generated from
> `culture-agent-template` and so far ships the agent-first CLI skeleton
> (identity and introspection verbs), the mesh identity, the skill kit, and
> CI/deploy. The Govee device verbs are **planned** — see [Roadmap](#roadmap).

## Quickstart

```bash
uv sync
uv run pytest -n auto                 # run the test suite
uv run govee whoami                   # identity from culture.yaml
uv run govee learn                    # self-teaching prompt (add --json)
uv run teken cli doctor . --strict    # the agent-first rubric gate CI runs
```

The installed command is **`govee`**; `python -m govee_cli` works too.

## CLI

Available today:

| Verb | What it does |
|------|--------------|
| `whoami` | Report this agent's nick, version, backend, and model from `culture.yaml`. |
| `learn` | Print a structured self-teaching prompt. |
| `explain <path>` | Markdown docs for any noun/verb path. |
| `overview` | Read-only descriptive snapshot of the agent. |
| `doctor` | Check the agent-identity invariants (prompt-file-present, backend-consistency). |
| `cli overview` | Describe the CLI surface itself. |

Every command supports `--json`. Results go to stdout, errors/diagnostics to
stderr (never mixed). Exit codes: `0` success, `1` user error, `2` environment
error, `3+` reserved.

The CLI is cited from [teken](https://github.com/agentculture/teken)
(`afi-cli`), and the runtime package has **no third-party dependencies**.

## Roadmap

(planned) Govee device control:

- **Discovery** of lights on the LAN and on the Govee account.
- **Control:** on/off, brightness, color, color temperature, and scenes.
- **Two transports:** Govee's official Developer API (cloud, API key) and
  the local LAN API (no key).
- **Safe-by-default writes:** every state-changing verb is a dry-run that
  reports what it would send, unless `--apply` is passed.
- **`--json`** on every verb, so agents can drive it.

## Prompt files by harness

Four harnesses, four root files, no shared base. Each file is read by
exactly one harness:

| Harness | File(s) |
|---------|---------|
| Claude Code | [`CLAUDE.md`](CLAUDE.md) |
| Pi / associate | [`AGENTS.override.md`](AGENTS.override.md) + [`.pi/SYSTEM.md`](.pi/SYSTEM.md) |
| colleague | [`AGENTS.colleague.md`](AGENTS.colleague.md) |
| Qwen Code | [`QWEN.md`](QWEN.md) |

**Claude Code:** `CLAUDE.md` is the fullest write-up of the repo's
conventions and the mesh resident's prompt, so read it first.

**Pi / associate:** `AGENTS.override.md` replaces this directory's
`AGENTS.md`/`CLAUDE.md` in Pi's context layer, so Pi does not inherit
`CLAUDE.md`. `.pi/SYSTEM.md` replaces Pi's default system prompt with the
non-coding `associate` identity (read/find/summarize only).

**colleague:** colleague's prompt cascade is `AGENTS.md` →
`AGENTS.colleague.md` → `AGENTS.colleague.<model>.md`. This repo ships only
the middle layer. There is no `AGENTS.md` (a shared base across harnesses was
considered and rejected) and no per-model override file.

**Qwen Code:** Qwen Code reads `QWEN.md` and `AGENTS.md`. Since there is no
`AGENTS.md`, `QWEN.md` is its only source of guidance.

There is intentionally **no `AGENTS.md`** at the root. Each harness gets its
own file rather than cascading from a shared base, and the CI harness smoke
check fails if one appears.

## Two selections, not one

It is tempting to read "switch harness" as one decision. It is actually two:

1. **The interactive harness:** which binary you run (`claude`, `pi`,
   `colleague`, `qwen`). `cd` into the clone and run any of them. All four
   are live at the same time, and none needs a file edit or a switch.
   A harness can be force-selected for one invocation
   (e.g. a CI smoke check) without ever touching `culture.yaml`. See
   [`docs/automation-contract.md`](docs/automation-contract.md).
2. **The mesh resident:** the single `backend` that `culture.yaml` declares
   (`claude` here). The Culture daemon starts this resident, and
   `steward doctor` checks it. `guild harness use <name>` changes only this
   selection.

`culture.yaml`'s `backend` affects (2) only. It never affects which harness
you can invoke interactively in (1). See
[`docs/harness-selection.md`](docs/harness-selection.md) for the full
writeup.

## The `gate` section

`culture.yaml` carries a top-level `gate` section that declares how to set
up and test this repo, so tooling (for example a PR-fixer that re-runs the
checks before pushing) needs no per-repo knowledge:

```yaml
gate:
  setup:
  - [uv, sync]
  test:
  - [uv, run, pytest, -n, auto]
```

`setup` and `test` are each a list of commands, and each command is an argv
list (never a shell string), run in order from the repo root. The values are
configuration only; no tool supplies a default. Culture's loader ignores
unknown top-level keys, so the section is safe alongside `agents`.

## Development

See [`CLAUDE.md`](CLAUDE.md) for the full conventions: the lint commands CI
runs, the CLI's error/output contract, version-bump-every-PR, the `cicd` PR
lane, and deploy setup. In short:

- **Every PR bumps the version** (`version-bump` skill); CI's
  `version-check` job blocks merge otherwise.
- **Deploy:** a push to `main` publishes to PyPI via Trusted Publishing.
  PRs publish a dev build to TestPyPI.
- **Skills:** `.claude/skills/` vendors the guildmaster skill kit
  (cite-don't-import). See [`docs/skill-sources.md`](docs/skill-sources.md).

## License

Apache 2.0 — see [`LICENSE`](LICENSE).
