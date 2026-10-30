# govee-cli

Agent-first CLI that controls a Govee lamp at home: discover it, switch it
on and off, set brightness, color, color temperature and scenes, and fade
between them, over the local LAN API with Govee's cloud Developer API as a
fallback. It also runs a web console with usage stats and an MCP server for
agents. Writes are dry runs unless `--apply`, and every command has `--json`.

Built for a Govee **H6076** floor lamp and tested against it; other Govee
lights that offer **LAN Control** should work the same way.

## Quickstart

```bash
uv sync
uv run govee devices                   # find lamps (LAN first, cloud fallback)
uv run govee light on                  # dry run: prints what it would send
uv run govee light on --apply          # actually turn it on (fades in over 5 s)
uv run govee light color FF6A2B --fade 5 --apply
uv run govee light temp 2700 --apply
uv run govee stats                     # usage tiles and recent events
```

The installed command is **`govee`**; `python -m govee_cli` works too.

Before the first run, open the lamp in the Govee Home app, go to its
settings and turn on **LAN Control**. That is all the LAN path needs.

## Three ways in

| Entry point | For | Command |
|-------------|-----|---------|
| CLI | you and scripts | `govee light ...`, `govee devices`, `govee stats` |
| Web console | phones and browsers on the LAN or Tailscale | `govee serve`, then open the pairing link |
| MCP server | AI agents (Claude Code, the AgentCulture mesh) | `govee mcp serve` (see [`docs/mcp.md`](docs/mcp.md)) |

Culture-rules can drive the lamp too, through a runner actor; see
[`docs/culture-rules/README.md`](docs/culture-rules/README.md). Schedules
live in culture-rules, not here: govee-cli has no timers of its own.

## Safety rules

- **Every write is a dry run unless you say otherwise:** `--apply` on the
  CLI, `apply: true` over MCP, "Send to lamp" (or Live mode) in the console.
  A dry run prints the exact command that would go on the wire.
- **Every command is logged** with its source (`cli`, `mcp`, `web`, `rules`)
  and the actor. Web console previews are the one exception: they are not
  logged.
- **The API key never leaves the environment.** It is read from
  `GOVEE_API_KEY` (or from `$XDG_CONFIG_HOME/govee-cli/env` (default `$HOME/.config`) by the systemd unit) and
  never printed or committed.

## Commands

| Verb | What it does |
|------|--------------|
| `devices [--refresh]` | List lamps found on the LAN and in the Govee account. |
| `light on`, `light off` | Power. Fades by default (`GOVEE_FADE_DEFAULT`, 5 s); `--fade 0` snaps. `light on --brightness N` ends at that level. |
| `light brightness N` | Brightness 1-100. |
| `light color HEX` | RGB color, e.g. `FF6A2B`. |
| `light temp K` | White color temperature, 2000-9000 K. |
| `light scene NAME` | A Govee scene (cloud only; needs `GOVEE_API_KEY`). |
| `light state`, `light scenes` | Read the lamp and its scene list. |
| `light fade-status`, `light fade-stop` | Watch or stop a running fade. |
| `stats` | On-time today and this week, commands, recent events. |
| `token new --name N [--save]`, `token list`, `token revoke N` | Pair browsers, apps and MCP clients with `govee serve`. |
| `serve [--host H] [--port P] [--rotate-token] [--trust NET]` | Run the web console and API (default `0.0.0.0:8742`). |
| `mcp serve` | Run the MCP server on stdio. |
| `rules actor` | Print the culture-rules runner actor for this machine (read-only). |
| `doctor [--no-scan]` | Health checks, including LAN interface, lamp discovery, API key presence and the stats database. |
| `whoami`, `learn`, `explain <path>`, `overview`, `cli overview` | Identity and self-description for agents. |

Light verbs take `--device NAME|ID`, `--fade SECONDS` (0-3600) and `--json`.
Every command supports `--json`. Results go to stdout, errors and
diagnostics to stderr (never mixed). Exit codes: `0` success, `1` user
error, `2` environment error (lamp unreachable, missing key, rate limited),
`3+` reserved.

## How it reaches the lamp

- **LAN first.** Discovery tries the lamp's last known address, Govee's
  multicast scan, and, if nothing answers, a quick unicast sweep of the
  local /24 (multicast does not reach every lamp on every Wi-Fi). Pin the
  interface with `GOVEE_LAN_INTERFACE` or the addresses with
  `GOVEE_LAN_HOSTS`. LAN writes are checked against the lamp's reported
  state and logged as confirmed or unconfirmed.
- **Cloud as fallback** (`GOVEE_API_KEY`), and always for scenes. Fades need
  LAN; over the cloud a change applies instantly with a warning.
- **After a fade-out the lamp rests at brightness 1.** A brightness command
  would switch it back on, so govee cannot restore the level while it is off.
  `govee light on` (and the console) bring back the earlier level; turning it
  on from the Govee app or the lamp's button lights it at 1 %.
- **`govee serve` owns the lamp while it runs.** The CLI and the MCP server
  then send everything through it, using a token from `GOVEE_TOKEN` or the
  file `govee token new --name me --save` writes. Only serve needs the key.

## The web console

```bash
govee serve                              # listens on 0.0.0.0:8742
govee token new --name phone             # prints a one-time pairing link
```

Open the link on the phone. The token travels in the URL fragment, so it
never reaches the server's logs. Every API call, reads included, needs a
paired device. The console shows the lamp, a dry-run "What will be sent"
panel, fades, recent activity and usage charts. `govee serve
--rotate-token` revokes every paired device.

To share control with everyone at home without handing out tokens, trust the
networks you consider safe:

```bash
govee serve --trust lan --trust tailscale   # or GOVEE_TRUSTED_NETWORKS=lan,tailscale
```

A device on a trusted network opens `http://<host>:8742/` and needs no token
(it is logged as `trusted <ip>`). `lan` is the /24 of the default route,
`tailscale` is 100.64.0.0/10 and its IPv6 range, and any CIDR works. Anyone on
those networks (guests on the Wi-Fi, every device in the tailnet) can then
control the lamp. Writes still need a same-origin request, and other
networks still need a paired token.

To keep it running, install the user unit in
[`docs/systemd/govee-serve.service`](docs/systemd/govee-serve.service).
Put `GOVEE_API_KEY=...` in `$XDG_CONFIG_HOME/govee-cli/env` (default `$HOME/.config`) (mode 0600) if you use
scenes.

Stats live in `$XDG_STATE_HOME/govee-cli/stats.db` (default `$HOME/.local/state`). Gaps in the charts mean
`govee serve` was not sampling, never that the lamp was off.

The CLI is cited from [teken](https://github.com/agentculture/teken)
(`afi-cli`), and the runtime package has **no third-party dependencies**.

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
