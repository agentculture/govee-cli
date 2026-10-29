# govee lamp integration

> govee-cli controls our Govee lamp from the CLI, an MCP server agents and the culture mesh can connect to, and a local web console with usage statistics

## Audience

- Ori at home (CLI + phone browser on LAN/Tailscale) and AgentCulture agents — the govee-cli resident and any Claude-backed mesh agent — via MCP

## Before → After

- Before: The repo is a template skeleton: no Govee code; the lamp is only controllable from the Govee Home phone app; agents cannot touch it; no usage history exists
- After: 'govee devices', 'govee light on|off|brightness|color|temp|scene --apply', 'govee stats', 'govee mcp serve' and 'govee serve' all drive the lamp through one core; the web console shows live state, controls and usage charts

## Why it matters

- Agents can make the lamp an ambient signal for the mesh (and Ori gets a faster console than the app), while dry-run defaults and a write log keep agent actions safe and auditable

## Requirements

- Govee transport clients (cloud Developer API over HTTPS, LAN API over UDP 4001/4002/4003) built on stdlib urllib/socket/json, since pyproject.toml has dependencies = \[\] and CLAUDE.md forbids third-party runtime deps
  - honesty: pyproject.toml dependencies stays \[\] after the change; transports import only stdlib
- New noun groups (devices, light, stats, mcp, serve) register via cli/`__init__.py`::`_build_parser` like `_commands`/cli.py, raise CliError with exit 2 for unreachable device/missing key, and update learn, overview `_VERBS` and explain catalog together for teken cli doctor --strict
  - honesty: teken cli doctor . --strict passes and every new verb has an explain catalog entry
- Mutating verbs (on/off/brightness/color/temp/scene) dry-run unless --apply, in CLI and MCP alike; every verb has --json
  - honesty: Without --apply (CLI) / apply:true (MCP, web) no packet or HTTPS write leaves the host — asserted with a fake transport that records sends
- Statistics: every applied command and periodic state poll is logged to a local sqlite3 store under XDG state dir (outside git); web console charts usage from it
  - honesty: Stats DB path resolves under `XDG_STATE_HOME` (default ~/.local/state/govee-cli/), never inside the repo
- Same-PR update of AGENTS.override.md, AGENTS.colleague.md, QWEN.md alongside CLAUDE.md; version bump
  - honesty: AGENTS.override.md, AGENTS.colleague.md, QWEN.md are updated in the same PR and pyproject version is bumped
- Web console write endpoints require a bearer token generated on first 'govee serve' and stored 0600 in the XDG config dir; reads (state, stats) are open; the token is shown once in the terminal and as a QR-free pairing URL ?token=... that the page stores in localStorage
  - instruction: test: POST /api/light without token -> 401; with token -> 200 (dry-run unless apply:true)
  - honesty: Write endpoint returns 401 without a valid token; token file mode is 0600
- Stats views: on-time per day (bar), brightness/color-temp over time (line), commands by source cli/mcp/web/agent (bar), last-24h state timeline; data from sqlite events + state samples taken by 'govee serve' every 60s
  - honesty: Each of the four charts renders from real sqlite rows, with an empty state when there is no data
- govee-cli ships a culture-rules 'runner' actor definition (docs/culture-rules/govee-actor.json) registering argv commands `lamp_on`, `lamp_off`, `lamp_brightness` {pct}, `lamp_color` {hex}, `lamp_temp` {kelvin}, `lamp_scene` {name}, each 'govee light ... --apply --json'; it is imported with 'culture-rules actors import' (dry run unless --apply) by the user, not by govee-cli
  - instruction: validate the JSON against `culture_rules`.model.actor.Actor (kind=runner) and `culture_rules`/actors/code.py's command registry shape
  - honesty: The definition passes 'culture-rules actors create --json' dry run (the user runs it with their `CULTURE_RULES_TOKEN`); no argv entry is a shell/interpreter (`inline_guard` would refuse it)
- Web console: the custom color picker previews the color on the lamp visual while it is still open (before selection); the color is committed or sent only on pick. Showing or hiding the dry-run preview note must not move the lamp visual (user feedback on prototype v1, 2026-10-29)
  - honesty: A Playwright check of the real console: dispatching 'input' on the custom color picker changes the lamp visual's color without sending a command, 'change' commits it, and the bulb's bounding box is identical with the preview note shown and hidden
- One LAN listener per host: Govee devices reply to UDP 4002 regardless of source port, so while 'govee serve' runs, the CLI and MCP server send LAN reads/writes through serve's loopback API; without serve they bind 4002 themselves, and a busy port exits 2 with 'port 4002 is in use (govee serve or another Govee integration)'
  - honesty: With a fake serve holding 4002, a CLI LAN call goes through the loopback API; with 4002 held by a foreign socket and no serve, the CLI exits 2 with the port-in-use message
- LAN writes are UDP with no acknowledgement, so an applied write is logged 'sent', then verified by a devStatus read within 3 s and updated to 'confirmed' or 'unconfirmed'; stats and the activity feed show the difference
  - honesty: A fake LAN device that drops the packet yields an event logged 'unconfirmed'; one that applies it yields 'confirmed'
- LAN multicast is pinned to an interface (`GOVEE_LAN_INTERFACE` or --interface, default: the interface of the default route); 'govee doctor' reports the interface used, whether discovery got any reply, and the remediation list (enable LAN Control in Govee Home, allow inbound UDP 4002, pick another interface)
  - honesty: govee doctor --json lists the interface used and a lan-discovery check with remediation when no device replies
- Web console token hardening: the pairing link carries the token in the URL fragment (#token=, never sent to the server or logged); the page moves it to localStorage and clears the hash; writes need the Authorization header (no cookie, so no CSRF) and a same-origin Origin header; comparison uses hmac.`compare_digest`; 'govee serve --rotate-token' invalidates every paired device; request bodies capped at 16 KiB
  - instruction: tests: POST without token 401, wrong Origin 403, rotated token 401, 17 KiB body 413
  - honesty: The four web tests (401 no token, 403 wrong Origin, 401 after rotate, 413 oversize) pass; server access log never contains the token
- Stats store opens sqlite in WAL mode with `busy_timeout` 5000 ms because serve's poller, the CLI and the MCP server write concurrently; a `schema_version` table makes later migrations explicit
  - honesty: Two processes writing events concurrently for 5 s produce no 'database is locked' error; PRAGMA `journal_mode` returns wal
- The culture-rules runner actor definition pins machine: spark (the host on the lamp's LAN) and uses the absolute path to the govee binary, because runner commands start with a minimal environment (`culture_rules`/actors/code.py docstring); LAN verbs need no key, and the scene command documents that `GOVEE_API_KEY` must be injected via grant or it exits 2
  - honesty: The actor JSON has machine 'spark', every argv\[0\] is absolute, and no argv is a shell or interpreter
- Cloud usage stays far under the Developer API's daily cap (10,000 requests/key/day per community docs, unverified officially): state polling uses LAN; when only cloud is available serve polls every 5 min at most; HTTP 429 exits 2 with the reset time from the response headers
  - honesty: A fake cloud returning 429 with reset headers makes the CLI exit 2 and print the reset time; serve's cloud-only poll interval is >= 300 s
- docs ship a systemd --user unit for 'govee serve' (restart on failure, EnvironmentFile for `GOVEE_API_KEY` outside the repo), since stats sampling only happens while serve runs; stats show gaps where serve was down instead of interpolating
  - honesty: systemd-analyze verify passes on the shipped unit; a stats chart over a serve outage shows a gap, not 'off'

## Honesty conditions

- One `govee_cli` core module is the only code path to the lamp; CLI, MCP and web handlers all call it (no duplicated transport code)
- scripts/scan-secrets.py passes; `GOVEE_API_KEY` is only read from the environment
- Both audiences have a working entry point: a browser URL for Ori and an MCP tool list for agents
- git ls-files at 341ad19 contains no Govee transport code
- Each listed verb exists in govee --help and has a test
- Every applied write records source (cli/mcp/web) and actor in the event log
- No BLE, no account-password login, no third-party runtime import anywhere in `govee_cli`/
- Measured in a manual LAN test on the real lamp after the build
- MCP integration test drives tools/list + tools/call over stdio against a fake transport
- CI run on the PR shows pytest, lint, teken doctor and scan-secrets green

## Success signals

- From a phone on the LAN, toggling the lamp in the web console changes the physical lamp in < 2 s over LAN, and the action appears in stats within 60 s
- A Claude agent with only .mcp.json can list devices and turn the lamp on in 2 tool calls (dry-run then apply); 100% of applied writes from cli/mcp/web land in the event log
- uv run pytest -n auto passes with coverage >= 60% using fake LAN/cloud transports (no lamp needed in CI); teken cli doctor --strict and scan-secrets stay green

## Scope / boundaries

- API key is read from env (`GOVEE_API_KEY`), never committed; device IPs/MACs never in committed JSON; scripts/scan-secrets.py stays green
- No third-party runtime deps; no Govee account login/password flow (API key only); no BLE transport; no committed key, IP or MAC

## Non-goals

- Authoring specific culture-rules rules (e.g. CI failed -> lamp red) is the user's work in the rules editor; this spec ships only the runner actor definition and one documented example rule
- govee-cli has no scheduler, timers, quiet hours or time-of-day rules; any schedule is a culture-rules rule invoking the govee runner actor

## Assumptions

- MCP server is a stdlib JSON-RPC-over-stdio 'govee mcp serve' exposed via a committed .mcp.json. Only the govee-cli resident loads it: cultureagent's Claude runner uses `setting_sources`=\['project'\] with cwd = the agent's own repo (cultureagent/clients/claude/`agent_runner.py`:146-150); other agents reach the lamp via the culture-rules runner actor or by asking govee-cli on the mesh
- Web console is static HTML/JS shipped as package data and served by stdlib http.server via 'govee serve' on localhost, with a small JSON API over the same core as CLI/MCP

## Scope exploration

- `s1` — `pyproject.toml + CLAUDE.md 'CLI architecture'`: runtime deps are empty and must stay so; clients, MCP and web server must be stdlib
  - seeds: `c2`
- `s2` — `govee_cli/cli/__init__.py, _errors.py, learn.py, overview.py, explain/catalog.py`: registration/error/rubric contract applies to every new verb; template strings still say 'clonable template'
  - seeds: `c3`
- `s3` — `CLAUDE.md 'Govee domain constraints'`: safe-by-default writes and --json are project rules; MCP tools must honour the same apply flag
  - seeds: `c4`
- `s4` — `scripts/scan-secrets.py`: fails CI on credential-shaped strings and non-localhost URLs in JSON; key from env only
  - seeds: `c5`
- `s5` — `~/git/culture/culture (grep mcp)`: no MCP handling in culture; Claude-backend agents load project .mcp.json
  - seeds: `c6`
- `s6` — `stdlib sqlite3 / .gitignore`: stats store must live outside the repo; sqlite3 is stdlib
  - seeds: `c7`
- `s7` — `pyproject.toml hatch wheel packages=[govee_cli]`: files under `govee_cli`/ ship in the wheel, so the site can be package data
  - seeds: `c8`
- `s8` — `CLAUDE.md 'Harnesses and identity' + Conventions`: four harness files must stay in sync; every PR bumps version
  - seeds: `c9`
- `s9` — `~/git/culture-rules (model/actor.py, actors/code.py, docs/actors/http.md; live service culture-rules serve on spark)`: culture-rules is the mesh rules engine (rules -> workflows -> actors); a runner actor runs registered argv commands with typed params, shell=False, so govee-cli plugs in as config, no culture-rules code change; the API needs `CULTURE_RULES_TOKEN` which this session lacks
  - seeds: `c25`
- `s10` — `artifact prototype https://claude.ai/artifact/LpqHehb6KuL5S51YCV26wD`: clickable console prototype (embodiment design tokens, sample data) built so the user reviews the site before the spec converges; stats charts: today color strip + brightness line, hours on per day, commands by source (validated CLI/Web/MCP palette), weekday x hour heatmap
  - seeds: `c21`
- `s11` — `challenge pass / concurrency+hardware lens: Govee LAN protocol (replies on UDP 4002, per openHAB/homebridge-govee docs) + ss -ulnp on spark (4001-4003 free today)`: multiple govee processes cannot all receive LAN replies; poller in serve would race CLI/MCP queries
  - seeds: `c29`
- `s12` — `challenge pass / failure-mode lens: LAN API turn/brightness/colorwc (no reply documented)`: 'applied' in the log would overclaim; the lamp may have dropped the packet
  - seeds: `c30`
- `s13` — `challenge pass / adjacent-systems lens: spark network (ip -4 addr: ~20 interfaces incl. docker bridges, tailscale; LAN scan got no reply on default and wlP9s9)`: multicast can leave on the wrong interface; inbound firewall for 4002 could not be checked without sudo
  - seeds: `c31`
- `s14` — `challenge pass / security lens: cultureagent agent_runner.py:148 permission_mode=bypassPermissions`: agents run with no permission prompts, so the server-side dry-run default and the write log are the only guard on agent writes; reinforces c4
  - seeds: `c6`
- `s15` — `challenge pass / security lens: c20 + prototype pairing copy (?token= query string)`: query-string tokens leak into browser history and proxy logs; no revoke path existed
  - seeds: `c32`
- `s16` — `challenge pass / concurrency lens: c7 stats store, writers = serve poller + CLI + MCP + culture-rules runner invocations`: default rollback-journal sqlite would raise 'database is locked' under concurrent writers
  - seeds: `c33`
- `s17` — `challenge pass / operations lens: culture_rules/actors/code.py (minimal env, temp cwd, shell=False) + culture-rules machines spark/thor/spark2`: a step scheduled on thor or with a bare 'govee' argv would fail; cloud key absent in runner env
  - seeds: `c34`
- `s18` — `challenge pass / operations lens: Govee cloud API limits (web search: 10k/day/key, ~10/min/device, community sources)`: 60 s cloud polling + agent writes could approach per-device limits
  - seeds: `c35`
- `s19` — `challenge pass / lifecycle lens: c21 stats sampling 'taken by govee serve every 60s'`: no process lifecycle was specified; downtime would silently look like 'lamp off'
  - seeds: `c36`
- `s20` — `challenge pass / reversibility lens: all lamp writes (on/off/brightness/color/temp/scene)`: every write is reversible by another write; no destructive operation in scope; clean, residual risk is only an unwanted visible change, bounded by dry-run default
- `s21` — `challenge pass / migration lens: repo at 341ad19 (template skeleton, no prior Govee data or users)`: greenfield, nothing to migrate now; `schema_version` (F8) covers future stats migrations
- `s22` — `challenge pass / observability lens: govee doctor + event log + stats gaps`: covered by F2/F3/F11 findings; not examined: alerting when the lamp goes unreachable (left to culture-rules)

## Decisions

- Transport: both — LAN API first, cloud Developer API fallback; user enables LAN Control in Govee Home and supplies `GOVEE_API_KEY` (user answer, 2026-10-29)
- Agents: stdio MCP server via committed .mcp.json plus a mesh skill/doc stating the rules — dry-run by default, --apply to write, every write logged (user answer)
- Web console binds 0.0.0.0 so LAN/Tailscale devices can reach it (user answer); the no-auth risk must be mitigated in-spec
- Process: full devague spec + plan before building (user answer)
- No schedules or time-based behavior in govee-cli (quiet hours dropped). Schedules live in culture-rules; govee-cli stays a stateless actor it can call (user decision 2026-10-29)

## Open parks

- [unknown_nonblocking] Which port the devStatus query must target (4003 per most clients; one homebridge-govee comment says 4002) — verify against the real lamp before relying on F2's verification
- [unknown_nonblocking] Stats retention: ~525k sample rows per year at 60 s; no pruning in v1, revisit if the DB passes 100 MB

## Resolved vagueness

- [unknown_blocking] LAN scan (multicast 239.255.255.250:4001, pinned to wlP9s9 192.168.1.157) got no reply: lamp model, LAN-control toggle, or firewall unknown — resolved: User chose both transports, LAN first; user will enable LAN Control and provide an API key. Why the scan got no reply is diagnosed by 'govee doctor' at build time, not a spec blocker.
