# Build Plan — govee lamp integration

slug: `govee-lamp-integration` · status: `exported` · from frame: `govee-lamp-integration`

> govee-cli controls our Govee lamp from the CLI, an MCP server agents and the culture mesh can connect to, and a local web console with usage statistics

## Tasks

### t1 — Core types and a recording fake transport

- instruction: Create the `govee_cli`/govee/ package. Keep the Transport Protocol minimal; LAN and cloud transports (t5, t6) and LampService (t7) build on it. No CLI wiring here.
- covers: h1
- acceptance:
  - `govee_cli`/govee/types.py defines frozen dataclasses Device, LampState, Command (verb, transport, payload) and a Transport Protocol (discover, `get_state`, send); `kelvin_to_rgb`(2000..9000) is tested at 2000/6500/9000
  - `govee_cli`/govee/fake.py FakeTransport records every send() and can be told to drop packets or return a scripted state; tests/`test_govee_types.py` passes
  - only stdlib imports

### t2 — Stats store: sqlite in WAL with events, samples and chart queries

- instruction: Stdlib sqlite3 only. tests/`test_stats_store.py` uses `tmp_path` and monkeypatched `XDG_STATE_HOME`. Gaps are data, never interpolated as 'off' (c36).
- covers: c7, h6, c33, h24
- acceptance:
  - `govee_cli`/stats/store.py `db_path`() resolves under `XDG_STATE_HOME`, default ~/.local/state/govee-cli/stats.db, never inside the repo
  - every connection sets `journal_mode`=WAL and `busy_timeout`=5000 and creates a `schema_version` table; two processes writing for 5 s raise no 'database is locked'
  - tables events(ts, source cli|mcp|web|rules, actor, verb, payload, status `dry_run`|sent|confirmed|unconfirmed) and samples(ts, on, brightness, rgb, kelvin); query functions return hours-on per day, commands per day by source, 24h samples with gaps where no sample exists for > 3 min, and weekday x hour minutes-on

### t3 — Web console static UI in embodiment design

- instruction: Start from docs/design/console-prototype.html (embodiment tokens, validated CLI/Web/MCP palette #2a78d6/#eb6834/#7a4fc9 light, #3987e5/#d95926/#9572e0 dark). No CDN except Google Fonts Albert Sans with system fallback. Static files must ship in the wheel (they live under `govee_cli`/).
- covers: c26, h19, c21, h14
- acceptance:
  - `govee_cli`/web/static/{index.html,app.js,app.css} port docs/design/console-prototype.html (Lamp, Stats, Agents views) with sample data removed, fetching /api/state, /api/light, /api/scene, /api/stats, /api/events with an Authorization bearer token from localStorage
  - unpaired (no token or 401): only a pairing screen renders; a #token= fragment is stored and cleared from the URL
  - custom color input previews on the lamp visual without a request; change commits; the bulb's bounding box is identical with the preview note shown and hidden
  - each of the four charts renders an empty state when the API returns no rows; quiet-hours UI is absent; only ASCII in the files (escape non-ASCII)

### t4 — Integration artifacts: culture-rules actor, systemd unit, .mcp.json and MCP snippet doc

- instruction: Shape from ~/git/culture-rules/`culture_rules`/model/actor.py and `culture_rules`/actors/code.py (commands registry: argv, params, timeout). Document in docs/culture-rules/README.md how the user imports it (culture-rules actors import, dry run then --apply) and one example rule: CI failed -> `lamp_color` FF0000. No schedules in govee-cli.
- covers: c25, h18, c34, h25, c36, h27
- acceptance:
  - docs/culture-rules/govee-actor.json is an Actor of kind runner, machine spark, with commands `lamp_on`, `lamp_off`, `lamp_brightness` {pct}, `lamp_color` {hex}, `lamp_temp` {kelvin}, `lamp_scene` {name}; every argv\[0\] is an absolute path to govee, ends with --apply --json, and none is a shell or interpreter; tests/`test_integration_artifacts.py` checks this
  - docs/systemd/govee-serve.service (user unit, Restart=on-failure, EnvironmentFile outside the repo) passes systemd-analyze verify --user
  - a committed .mcp.json runs 'govee mcp serve'; docs/mcp.md has the snippet other agent repos add, the tool list and the dry-run rule; scan-secrets passes

### t5 — LAN transport: discovery, control and status over UDP

- instruction: devStatus target port is an open risk (4003 vs 4002): make it a module constant with a comment pointing at the plan risk, defaulting to 4003.
- depends on: t1
- covers: c2, c31, h22
- acceptance:
  - `govee_cli`/govee/lan.py implements Transport: scan multicasts {cmd:scan} to 239.255.255.250:4001 on the interface from `GOVEE_LAN_INTERFACE` / argument / default-route interface (`IP_MULTICAST_IF`), listens on 4002; send() emits turn/brightness/colorwc JSON to device:4003; `get_state`() sends devStatus and waits for the 4002 reply
  - binding 4002 when it is held raises a typed PortBusy error carrying the port-in-use message; tests use injected sockets / a fake device on 127.0.0.1 and never real multicast
  - only stdlib imports (socket, json, select)

### t6 — Cloud transport: Govee Developer API over urllib

- instruction: Check developer.govee.com for the current endpoint family (router/api/v1 device/control with capability objects vs the older v1 appliance API) before coding; see plan risk. urllib.request only.
- depends on: t1
- covers: c5, h5
- acceptance:
  - `govee_cli`/govee/cloud.py implements Transport plus scenes(device) and `set_scene`(); the key comes only from os.environ\['`GOVEE_API_KEY`'\] and is never logged, echoed or put in an error message
  - HTTP 429 raises a typed RateLimited error with the reset time parsed from response headers; missing key raises a typed MissingKey error; tests run against a local http.server fake, never the real API
  - the base URL lives in Python source (not committed JSON) so scan-secrets stays green

### t15 — Token store: named, hashed, revocable pairing tokens

- instruction: Stdlib only (secrets, hashlib, hmac, json, os). Atomic writes (temp file + os.replace) so concurrent CLI and serve processes never read a half-written file.
- covers: c39, h28
- acceptance:
  - `govee_cli`/auth/tokens.py: new(name) returns a 32-byte urlsafe secret once and stores only its sha256 with name, created, `last_used` in $`XDG_CONFIG_HOME`/govee-cli/tokens.json at mode 0600; list() never returns secrets; revoke(name); `revoke_all`(); verify(secret) uses hmac.`compare_digest` and updates `last_used`
  - duplicate names are refused; a revoked token fails verify() immediately; tests/`test_tokens.py` checks file mode and that the file contains no secret

### t7 — LampService: one code path, dry run by default, verified writes, logged

- instruction: This is the only module that touches transports; CLI, MCP and web call LampService. Missing key / unreachable device / rate limit map to CliError(2, ..., remediation).
- depends on: t1, t2, t5, t6, t15
- covers: c1, h1, c4, h4, c17, h11, c30, h21, c29, h20
- acceptance:
  - `govee_cli`/govee/service.py LampService.apply(change, \*, apply=False, source, actor) returns the planned Command list; with apply=False no Transport.send is called (FakeTransport proves it); with apply=True exactly the previewed commands are sent
  - LAN first, cloud fallback per command (scenes always cloud); an applied LAN write is logged 'sent' then verified with `get_state` within 3 s and updated to 'confirmed' or 'unconfirmed'; dry runs are logged '`dry_run`'; every event carries source and actor
  - when a serve loopback endpoint is reachable (`GOVEE_SERVE_URL`, default <http://127.0.0.1>:`<port>`) with a token, LAN operations route through it; otherwise LAN is used directly and PortBusy maps to CliError exit 2 with the port-in-use message

### t8 — CLI verbs: devices, light, stats, token, serve and mcp entry points, doctor LAN checks

- instruction: This task owns `govee_cli`/cli/\*\* and `govee_cli`/explain/\*\* only, so t9/t10/t11 stay file-disjoint. Use `parser_class`=type(p) for nested subparsers as `_commands`/cli.py does. source='cli', actor = $USER.
- depends on: t7, t15
- covers: c3, h3, c16, h10, c31
- acceptance:
  - `govee_cli`/cli/`_commands`/{devices,light,stats,token,serve,mcp}.py register via `_build_parser`; light on|off|brightness|color|temp|scene print the planned command and exit 0 without --apply, send with --apply; every verb supports --json and keeps the stdout/stderr split
  - serve and 'mcp serve' import `govee_cli`.web.server.run / `govee_cli`.mcp.server.run lazily; token new|list|revoke call `govee_cli`.auth.tokens
  - doctor adds checks lan-interface, lan-discovery (with remediation: enable LAN Control, allow inbound UDP 4002, set `GOVEE_LAN_INTERFACE`), api-key-present (never printing the key), stats-db-writable
  - learn (text + JSON), overview `_VERBS`, explain/catalog.py entries and the template strings named in CLAUDE.md (parser description, learn.`_TEXT`, catalog.`_ROOT`, overview.`_ARTIFACTS`) describe govee-cli; teken cli doctor . --strict passes; tests/`test_cli_light.py` etc. pass

### t9 — MCP server: stdio JSON-RPC with lamp tools

- instruction: Stdlib json + sys.stdin/stdout line-delimited JSON-RPC 2.0. Target MCP protocol version 2025-06-18. Never read `GOVEE_API_KEY` here; LampService owns transports.
- depends on: t7
- covers: c23, h16
- acceptance:
  - `govee_cli`/mcp/server.py run() speaks MCP over stdio (initialize, tools/list, tools/call, ping) with tools `list_devices`, `get_state`, `set_light`, `set_scene`, `get_stats`; `set_`\* default to dry run and only send with apply: true
  - every call logs source=mcp and actor from `GOVEE_ACTOR`, else the initialize clientInfo name; errors return MCP tool errors carrying the CliError message and remediation, never a traceback
  - tests/`test_mcp_server.py` drives the server over a subprocess pipe against FakeTransport: two calls (dry run, then apply) turn the lamp on

### t10 — Web server: token-gated API, static console, loopback LAN owner

- instruction: http.server.ThreadingHTTPServer, stdlib only. Owns `govee_cli`/web/server.py only (static files are t3, poller is t11). source='web', actor = token name.
- depends on: t7, t2, t15
- covers: c20, h13, c32, h23
- acceptance:
  - `govee_cli`/web/server.py run(host='0.0.0.0', port=8742) serves `govee_cli`/web/static and /api/{state,light,scene,stats,events,lan}; every /api route requires a valid bearer token (401 otherwise), including reads
  - writes also require a same-origin Origin header (403 otherwise); bodies over 16 KiB get 413; tokens never appear in the access log; --rotate-token revokes all tokens and prints a fresh pairing link with the token in the #fragment
  - /api/lan is the loopback-only LAN proxy LampService uses while serve owns UDP 4002 (refuses non-loopback clients); tests/`test_web_server.py` covers 401, 403, 413, rotate, loopback-only

### t11 — State poller: 60 s LAN samples, 5 min cloud-only, gaps not zeros

- instruction: Runs as a daemon thread started by web.server.run(); exposes start()/stop(). No schedules or time-of-day logic beyond the sampling interval.
- depends on: t7, t2
- covers: c35, h26, c21, c36
- acceptance:
  - `govee_cli`/web/poller.py samples lamp state every 60 s over LAN, or every 300 s when only cloud is available, writing samples rows; a RateLimited error backs off until the reset time
  - a stopped poller leaves no rows, so the stats queries report a gap instead of 'off'; tests/`test_poller.py` uses a fake clock and FakeTransport

### t12 — Docs, harness files and version bump

- instruction: Keep docs grounded in what's on disk. Never mention lobes-cli in AGENTS.override.md.
- depends on: t3, t4, t8, t9, t10, t11
- covers: c9, h7, c14, h8, c15, h9
- acceptance:
  - README.md documents the three entry points (govee CLI, govee serve URL + pairing, govee mcp serve) and the LAN Control prerequisite; CLAUDE.md drops 'No Govee device code exists yet' and the Roadmap moves to shipped
  - AGENTS.override.md, AGENTS.colleague.md and QWEN.md updated in the same change; no root AGENTS.md; harness-smoke config stage passes
  - version-bump skill run (minor), CHANGELOG entry describes before (template skeleton, git ls-files at 341ad19 has no Govee code) and after; markdownlint clean

### t13 — Verification: e2e checks, boundaries and CI gates

- instruction: Playwright via the playwright MCP or a dev-only script; never a runtime dependency.
- depends on: t12
- covers: c2, h2, c18, h12, c24, h17, c26, h19
- acceptance:
  - tests/`test_boundaries.py` asserts pyproject dependencies == \[\], no `govee_cli` module imports a non-stdlib package, and no BLE or account-password code exists
  - a Playwright check against 'govee serve' with FakeTransport verifies c26 (input previews without a request, change commits, bulb box unchanged) and that an unpaired browser sees only the pairing screen
  - full CI lint job (scan-secrets, black, isort, flake8, bandit, markdownlint, teken cli doctor --strict) and pytest -n auto with coverage >= 60% pass on the PR

### t14 — Live acceptance on the real lamp

- instruction: The main agent live-tests first, before the user: from spark it runs govee devices / light commands against the real lamp over LAN (and cloud if `GOVEE_API_KEY` is set), drives the web console with Playwright at phone width, verifies the devStatus port, and records measured results in docs/live-test-results/. Only then does the user repeat the phone toggle and run the culture-rules dry-run import with `CULTURE_RULES_TOKEN`. Record measured numbers only; a failure is recorded as a failure.
- depends on: t13
- covers: c22, h15, h18
- acceptance:
  - with LAN Control enabled, govee devices finds the lamp; a web console toggle from a phone changes it in < 2 s and the event appears in stats within 60 s; results recorded in docs/live-test-results/ with date and model
  - devStatus target port verified and the lan.py constant updated if needed; an applied write shows 'confirmed'
  - the user runs 'culture-rules actors create --json' as a dry run with docs/culture-rules/govee-actor.json and it validates
  - the main agent's own live run is recorded in docs/live-test-results/ before the user's run is requested

## Risks

- [unknown_nonblocking] devStatus query target port (4003 vs 4002) unverified until the real lamp answers (task t5)
- [unknown_nonblocking] Govee cloud endpoint family (router/api/v1 capabilities vs legacy v1 appliance API) and rate limits come from community sources; confirm on developer.govee.com (task t6)
- [unknown_nonblocking] The lamp has not answered a LAN scan yet (LAN Control off, unsupported model, or firewall on UDP 4002); t14 cannot pass until it does (task t14)
