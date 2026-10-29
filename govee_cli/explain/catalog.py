"""Markdown catalog for ``govee-cli explain <path>``.

Each entry is verbatim markdown. Keys are command-path tuples. The empty tuple
and ``("govee-cli",)`` both resolve to the root entry.

Keep bodies self-contained: an agent reading one entry should get enough
context without chaining reads.
"""

from __future__ import annotations

_ROOT = """\
# govee-cli

An agent-first CLI that controls a Govee H6076 floor lamp at home: discover the
device, switch it on/off with fades, set brightness, colour, colour temperature
and scenes. It uses the local LAN API (no key) and Govee's cloud Developer API
(`GOVEE_API_KEY`). Writes are safe by default: without `--apply` they print the
commands they would send and exit 0. Every verb supports `--json`. The
installed command is `govee`.

## Verbs

- `govee devices` — list devices.
- `govee light on|off|brightness|color|temp|scene` — change the lamp.
- `govee light state|scenes|fade-status|fade-stop` — read and manage fades.
- `govee stats` — usage tiles and recent events.
- `govee token new|list|revoke` — client tokens for `govee serve`.
- `govee serve` — web console and API; all lamp traffic then routes through it.
- `govee mcp serve` — MCP server over stdio.
- `govee rules actor` — culture-rules runner actor definition for this machine.
- `govee whoami|learn|explain|overview|doctor|cli overview` — introspection.

## Exit-code policy

- `0` success (a dry run is a success)
- `1` user-input error
- `2` environment / setup error (lamp or API unreachable, missing key or token)
- `3+` reserved
- `130` a foreground fade was stopped with Ctrl-C

## See also

- `govee-cli explain light`
- `govee-cli explain doctor`
"""

_WHOAMI = """\
# govee-cli whoami

Reports the agent's identity from `culture.yaml`: nick (`suffix`), backend,
served model, and the package version. Read-only.

## Usage

    govee-cli whoami
    govee-cli whoami --json
"""

_LEARN = """\
# govee-cli learn

Prints a structured self-teaching prompt covering purpose, command map,
exit-code policy, `--json` support, and the `explain` pointer.

## Usage

    govee-cli learn
    govee-cli learn --json
"""

_EXPLAIN = """\
# govee-cli explain <path>

Prints markdown documentation for any noun/verb path. Unlike `--help` (terse,
positional), `explain` is global and addressable by path.

## Usage

    govee-cli explain govee-cli
    govee-cli explain whoami
    govee-cli explain --json <path>
"""

_OVERVIEW = """\
# govee-cli overview

Read-only descriptive snapshot of the agent: identity (from `culture.yaml`), the
verb surface, and the sibling-pattern artifacts the template carries. Accepts an
ignored `target` so a stray path never hard-fails.

## Usage

    govee-cli overview
    govee-cli overview --json
"""

_DOCTOR = """\
# govee-cli doctor

Checks the agent-identity invariants `steward doctor` verifies:
prompt-file-present and backend-consistency (`claude` → `CLAUDE.md`), plus a
skills-present check. Exits 1 when unhealthy.

It also reports advisory environment checks (they never make the report
unhealthy): `lan-interface` (which interface multicast uses), `lan-discovery`
(scans the LAN and lists the methods tried — cache, hosts, multicast, sweep —
and how each lamp was found), `api-key-present` (whether `GOVEE_API_KEY` is set;
the value is never printed) and `stats-db-writable`. When no lamp answers,
enable LAN Control in the Govee Home app, allow inbound UDP 4002, and set
`GOVEE_LAN_INTERFACE` or `GOVEE_LAN_HOSTS`. `--no-scan` (or
`GOVEE_DOCTOR_NO_SCAN=1`) skips the network scan.

prompt-file-present requires the *resident* prompt the declared backend
actually reads. Other harness prompt files recognized under the same backend
name (`AGENTS.override.md`, `.pi/SYSTEM.md`, `QWEN.md`) belong to
interactively available harnesses the mesh daemon never loads; they are
reported by the informational harness-prompts check and never substituted.

## Usage

    govee-cli doctor
    govee-cli doctor --json
    govee-cli doctor --no-scan
"""

_CLI = """\
# govee-cli cli

Noun group for CLI-surface introspection. `cli overview` describes the CLI
itself (distinct from the global `overview`, which describes the agent).

## Usage

    govee-cli cli overview
    govee-cli cli overview --json
"""

_DEVICES = """\
# govee devices

Lists the Govee devices found by LAN discovery and, with `GOVEE_API_KEY`, the
cloud. Read-only. `--refresh` bypasses the short discovery cache.

## Usage

    govee devices
    govee devices --json
"""

_LIGHT = """\
# govee light

Reads and changes the lamp. **Writes are dry runs unless `--apply`**: they print
the planned commands (`would send:`) and the equivalent `--apply` invocation,
and exit 0. Every verb takes `--device NAME|ID` (required with several lamps)
and `--json`; writes also take `--apply` and `--fade SECONDS`.

Fades: `on`/`off` fade by default over `GOVEE_FADE_DEFAULT` (5 s);
brightness, colour and temperature are instant unless `--fade` is given.
`--fade 0` snaps; the maximum is 3600. Without `govee serve` the fade runs in
the foreground and Ctrl-C stops it (exit 130). A dry run prints the fade plan:
seconds, steps, interval and the first and last step. Fades run on the LAN only;
over the cloud the change is instant with a warning.

## Verbs

- `light on`, `light off` — power.
- `light brightness N` — 1-100.
- `light color RRGGBB` — hex colour, `#` optional.
- `light temp N` — kelvin, 2000-9000.
- `light scene NAME` — cloud scene (needs `GOVEE_API_KEY`).
- `light state`, `light scenes` — read-only.
- `light fade-status`, `light fade-stop` — inspect or stop a running fade.

## Usage

    govee light on
    govee light brightness 40 --fade 3 --apply
    govee light off --apply --json
"""

_LIGHT_ON = """\
# govee light on

Turn the lamp on. Fades by default (`GOVEE_FADE_DEFAULT`, 5 s); `--fade 0`
snaps. Dry run unless `--apply`.

## Usage

    govee light on [--device X] [--fade S] [--apply] [--json]
"""

_LIGHT_OFF = """\
# govee light off

Turn the lamp off. Fades by default (`GOVEE_FADE_DEFAULT`, 5 s); `--fade 0`
snaps. The pre-fade brightness is remembered for the next `light on`. Dry run
unless `--apply`.

## Usage

    govee light off [--device X] [--fade S] [--apply] [--json]
"""

_LIGHT_BRIGHTNESS = """\
# govee light brightness

Set brightness 1-100. Instant unless `--fade S`. Dry run unless `--apply`.

## Usage

    govee light brightness 40 [--device X] [--fade S] [--apply] [--json]
"""

_LIGHT_COLOR = """\
# govee light color

Set an RGB colour given as six hex digits (`FF6A2B` or `#FF6A2B`). Instant
unless `--fade S`. Dry run unless `--apply`.

## Usage

    govee light color FF6A2B [--device X] [--fade S] [--apply] [--json]
"""

_LIGHT_TEMP = """\
# govee light temp

Set the colour temperature in kelvin (2000-9000). Instant unless `--fade S`.
Dry run unless `--apply`.

## Usage

    govee light temp 3000 [--device X] [--fade S] [--apply] [--json]
"""

_LIGHT_SCENE = """\
# govee light scene

Activate a scene by name (case-insensitive). Scenes always go over the cloud and
need `GOVEE_API_KEY`; they change instantly. List names with `light scenes`.
Dry run unless `--apply`.

## Usage

    govee light scene Sunset [--device X] [--apply] [--json]
"""

_LIGHT_STATE = """\
# govee light state

Read the lamp's power, brightness, colour or kelvin, and scene. Read-only; no
`--apply`. A lamp that does not answer is reported as not reachable (exit 0).

## Usage

    govee light state [--device X] [--json]
"""

_LIGHT_SCENES = """\
# govee light scenes

List the scene names the cloud offers for the lamp. Read-only; needs
`GOVEE_API_KEY`.

## Usage

    govee light scenes [--device X] [--json]
"""

_LIGHT_FADE_STATUS = """\
# govee light fade-status

Show the fade currently running (only visible while `govee serve` runs it in the
background). Read-only.

## Usage

    govee light fade-status [--device X] [--json]
"""

_LIGHT_FADE_STOP = """\
# govee light fade-stop

Stop the running fade; the lamp keeps its current level. Reports whether a fade
was running.

## Usage

    govee light fade-stop [--device X] [--json]
"""

_LIGHT_OVERVIEW = """\
# govee light overview

Lists the `light` verbs and their shared flags (`--device`, `--fade`, `--apply`,
`--json`).

## Usage

    govee light overview [--json]
"""

_STATS = """\
# govee stats

Usage tiles (hours on today and this week, commands and agent commands in the
last 7 days, average brightness today) and the 10 most recent events from the
local stats database (`$XDG_STATE_HOME/govee-cli/stats.db`). Read-only.

## Usage

    govee stats
    govee stats --json
"""

_AUTH = """\
# govee token

Client tokens authenticate browsers and the CLI to `govee serve`. Secrets are
shown once and only their hash is stored.

- `token new --name N [--save] [--port P]` — mint a token; prints the secret and
  a pairing link (the secret rides in the `#fragment`). `--save` writes the
  secret to the client token file (mode 0600), which the CLI then uses.
- `token list` — names and timestamps, never secrets.
- `token revoke NAME` or `token revoke --all`.

## Usage

    govee token new --name laptop --save
    govee token list --json
    govee token revoke laptop
"""

_AUTH_NEW = """\
# govee token new

Mint a token. The secret and the pairing link are printed once (not stored in
clear). `--save` writes the secret atomically to the client token file (mode
0600, directory 0700) so `govee` commands route through a running `govee serve`;
otherwise export it as `GOVEE_TOKEN`. The link host is this machine's
default-route address (127.0.0.1 if none); `--port` sets the serve port
(default 8742). A duplicate name is a user error.

## Usage

    govee token new --name N [--save] [--port P] [--json]
"""

_AUTH_LIST = """\
# govee token list

List token names with created and last-used timestamps. Never prints secrets.

## Usage

    govee token list [--json]
"""

_AUTH_REVOKE = """\
# govee token revoke

Revoke one token by name, or all with `--all`. An unknown name is a user error.

## Usage

    govee token revoke NAME [--json]
    govee token revoke --all [--json]
"""

_AUTH_OVERVIEW = """\
# govee token overview

Lists the `token` verbs.

## Usage

    govee token overview [--json]
"""

_SERVE = """\
# govee serve

Runs the web console and loopback API. While it runs, every lamp operation from
the CLI and MCP routes through it, authenticated by `GOVEE_TOKEN` or the client
token file (`govee token new --save`). `--rotate-token` mints a fresh pairing
token at startup. Defaults come from the server (port 8742).

## Usage

    govee serve [--host H] [--port P] [--rotate-token]
"""

_MCP = """\
# govee mcp

Model Context Protocol entry point. `mcp serve` speaks MCP over stdio (stdout is
the protocol channel, so nothing else is printed there); `mcp overview` lists
the verbs.

## Usage

    govee mcp serve
    govee mcp overview
"""

_MCP_SERVE = """\
# govee mcp serve

Run the MCP server over stdio so an agent can drive the lamp through the same
service as the CLI. Writes follow the same dry-run-unless-apply rule.

## Usage

    govee mcp serve
"""

_MCP_OVERVIEW = """\
# govee mcp overview

Lists the `mcp` verbs.

## Usage

    govee mcp overview [--json]
"""

_RULES = """\
# govee rules

Integration with culture-rules, the mesh rules engine. Schedules and triggers
live in culture-rules; govee-cli only describes itself as a runner actor.

## Usage

    govee rules actor [--bin PATH] [--machine NAME] [--json]
    govee rules overview
"""

_RULES_ACTOR = """\
# govee rules actor

Print the culture-rules `runner` actor definition for this machine: commands
lamp_on, lamp_off, lamp_brightness, lamp_color, lamp_temp, lamp_scene,
lamp_fade_on and lamp_fade_off, each running this `govee` binary (absolute
path, since runner commands get only PATH=/usr/local/bin:/usr/bin:/bin) with
`--source rules --apply --json`. Read-only: it changes nothing. Import it with
`culture-rules actors create --body @actor.json` (dry run first).

## Usage

    govee rules actor > actor.json
    govee rules actor --bin /opt/govee/bin/govee --machine spark
"""

_RULES_OVERVIEW = """\
# govee rules overview

Lists the `rules` verbs.

## Usage

    govee rules overview [--json]
"""


ENTRIES: dict[tuple[str, ...], str] = {
    (): _ROOT,
    ("govee-cli",): _ROOT,
    ("govee",): _ROOT,
    ("whoami",): _WHOAMI,
    ("learn",): _LEARN,
    ("explain",): _EXPLAIN,
    ("overview",): _OVERVIEW,
    ("doctor",): _DOCTOR,
    ("cli",): _CLI,
    ("cli", "overview"): _CLI,
    ("devices",): _DEVICES,
    ("light",): _LIGHT,
    ("light", "on"): _LIGHT_ON,
    ("light", "off"): _LIGHT_OFF,
    ("light", "brightness"): _LIGHT_BRIGHTNESS,
    ("light", "color"): _LIGHT_COLOR,
    ("light", "temp"): _LIGHT_TEMP,
    ("light", "scene"): _LIGHT_SCENE,
    ("light", "state"): _LIGHT_STATE,
    ("light", "scenes"): _LIGHT_SCENES,
    ("light", "fade-status"): _LIGHT_FADE_STATUS,
    ("light", "fade-stop"): _LIGHT_FADE_STOP,
    ("light", "overview"): _LIGHT_OVERVIEW,
    ("stats",): _STATS,
    ("token",): _AUTH,
    ("token", "new"): _AUTH_NEW,
    ("token", "list"): _AUTH_LIST,
    ("token", "revoke"): _AUTH_REVOKE,
    ("token", "overview"): _AUTH_OVERVIEW,
    ("serve",): _SERVE,
    ("mcp",): _MCP,
    ("mcp", "serve"): _MCP_SERVE,
    ("mcp", "overview"): _MCP_OVERVIEW,
    ("rules",): _RULES,
    ("rules", "actor"): _RULES_ACTOR,
    ("rules", "overview"): _RULES_OVERVIEW,
}
