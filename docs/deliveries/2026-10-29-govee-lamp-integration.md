# Delivery Summary — govee lamp integration

plan: `govee-lamp-integration` · run: `complete` (with user follow-ups, `d26`) · date: `2026-10-29`
baseline: `devague summary skeleton`

## Intent

> govee-cli controls our Govee lamp from the CLI, an MCP server agents and the culture mesh can connect to, and a local web console with usage statistics

After: 'govee devices', 'govee light on|off|brightness|color|temp|scene --apply', 'govee stats', 'govee mcp serve' and 'govee serve' all drive the lamp through one core; the web console shows live state, controls and usage charts

The run executed the approved split (gate 2, `bd56e16`) on branch
`lamp/integration`. It went through seven waves with one worktree per task
under `../.worktrees.govee-cli/`, and every merge passed the tests before and
after. Two extra work items came from deviations: fades (`d11`, the user's
requirement) and the LAN discovery fallback (`d15`). Every task is delivered. Three `t14` checks need the user: a real phone, a
visual check of the fades, and the culture-rules import. Deviation `d26`
(proposed) moved them to post-merge follow-ups the user owns. If the user
rejects `d26`, `t14` and the run go back to partial. `t13` is complete;
every check on PR #3 passed.

## Planned Work

Quoted verbatim from the `devague summary` skeleton:

- `t1` — Core types and a recording fake transport
- `t2` — Stats store: sqlite in WAL with events, samples and chart queries
- `t3` — Web console static UI in embodiment design
- `t4` — Integration artifacts: culture-rules actor, systemd unit, .mcp.json and MCP snippet doc
- `t5` — LAN transport: discovery, control and status over UDP
- `t6` — Cloud transport: Govee Developer API over urllib
- `t7` — LampService: one code path, dry run by default, verified writes, logged
- `t8` — CLI verbs: devices, light, stats, token, serve and mcp entry points, doctor LAN checks
- `t9` — MCP server: stdio JSON-RPC with lamp tools
- `t10` — Web server: token-gated API, static console, loopback LAN owner
- `t11` — State poller: 60 s LAN samples, 5 min cloud-only, gaps not zeros
- `t12` — Docs, harness files and version bump
- `t13` — Verification: e2e checks, boundaries and CI gates
- `t14` — Live acceptance on the real lamp
- `t15` — Token store: named, hashed, revocable pairing tokens

## Actual Delivery

| Plan task | Status | What actually landed |
|-----------|--------|----------------------|
| `t1` | delivered | `govee_cli/govee/types.py`, `fake.py`; merge `1945739` |
| `t2` | delivered | `govee_cli/stats/store.py` (WAL, events, samples, chart queries); merge `4fc5dce`. The main agent fixed a test that forked (`98a72a2`) and a race on the first concurrent open (`115ea44`). |
| `t3` | delivered | `govee_cli/web/static/` console; merge `f070df5` after the first merge was reverted on a scan-secrets false positive. Layout fixes followed in `t13` (`62cffe6`). |
| `t4` | delivered | culture-rules actor (committed JSON, later replaced by the `govee rules actor` generator after PR #3's portability CI, `d24`) + README, `docs/systemd/govee-serve.service`, `.mcp.json`, `docs/mcp.md`; merge `1ddf31e`. The fade and `--source rules` commands were added in `t12`. |
| `t5` | delivered | `govee_cli/govee/lan.py`; merge `ab0cc2d`. The discovery fallback (`d15`) was merged in `7d2ea7e`, and an early return was added in `1657e45`. |
| `t6` | delivered | `govee_cli/govee/cloud.py` against the router/api/v1 endpoints confirmed in Govee's docs; merge `f0ffbc5` |
| `t7` | delivered | `govee_cli/govee/service*.py` with routing through serve (`d3`); merge `e44e683`. Fades (`d11`) were merged in `0a4fa3c`. |
| `t8` | delivered | the full CLI verb map, doctor LAN checks and the teken rubric; merge `718d964`. A CI no-scan fix followed in `7f28580`. |
| `t9` | delivered | `govee_cli/mcp/server.py` with 7 tools; merge `af9878c` |
| `t10` | delivered | `govee_cli/web/server.py`, token-gated; merge `e695733`. SIGTERM handling was added in `2a6e764`. |
| `t11` | delivered | `govee_cli/web/poller.py`; merge `1e184b4`. A cloud-quota fix followed in `a5cf8cd`. |
| `t12` | delivered | README, CLAUDE.md and the three harness files, version 0.11.0 and the changelog; `3d41622` |
| `t13` | delivered | boundary tests, MCP fade and scene tests, Playwright e2e and two layout fixes (`62cffe6`). On PR #3 every check passed at `d525079`: test (both workflows), lint, harness-smoke, version-check, SonarCloud gate (new-code coverage 80.1 %) and GitGuardian. Getting there took fixes `9a25d3f`, `4536bae` and `d525079` (`d24`, `d25`). |
| `t14` | delivered (agent part); user checks deferred (`d26`) | The agent's live run is recorded in `docs/live-test-results/2026-10-29-h6076.md` (`ef5689d`); 3 defects were found and fixed. The LAN-address check (`e22`) is done. The real phone, the visual fade check and the culture-rules import are follow-ups for the user (`d26`). |
| `t15` | delivered | `govee_cli/auth/tokens.py`; merge `e451e9d`. `pairing_link` was added in `d19ba1a`. |

## Mid-work Decisions

The approved deviations below are quoted from their records. The agent
recorded the rest as proposed, and the user reviews them on the PR.

- `d3` (approved) — while serve runs, every lamp operation goes through it,
  so only serve holds the key and UDP 4002. The reason: the culture-rules
  runner passes no environment. `/assess` gave route-via-serve 0.631.
- `d4` (approved) — the systemd unit could only be verified once `govee` was
  installed. It was verified in `t14`.
- `d5` (approved) — the stats queries bucket in the server's local timezone.
- `d7` (approved) — web console dry-run previews are not logged. `/assess`
  gave this 0.496, a lean and not a verdict.
- `d11` (approved, the user's requirement) — fades. On and off fade by
  default (1.5 s), `--fade` covers brightness, colour and temperature, and
  serve runs the fades.
- Proposed `d1`, `d2`, `d6`, `d8`, `d9`, `d10`, `d12`–`d27`. Each record is
  listed by `devague deviate --list`. The notable ones:
  - `d10`: no `SO_REUSEADDR` (the agent's brief was wrong).
  - `d15`: the discovery fallback, after the real lamp ignored multicast
    (`/assess` 0.806).
  - `d16`: the `.gitignore` lines that devague added got committed.
  - `d21`: `--source rules`.
  - `d22`: after a fade-out the lamp rests at brightness 1.
  - `d23`: the live-test fixes.
  - `d24`: `govee rules actor` replaces the committed actor JSON (PR CI).
  - `d25`: the devague records are waived in harness-smoke by exact path.
  - `d26`: the user-only `t14` checks become follow-ups.
  - `d27`: the static console is excluded from Sonar's coverage metric only
    (Python new-code coverage is 92.3 %).
- **The main agent mislabelled ids, without a record at the time.** In its
  messages, agent briefs, code comments and deltas `b2` and `b10`, it called
  the cloud-key decision "d4" and the systemd follow-up "d3" (lapse `l6`).
  The user approved both ids, so no approval changed meaning. The references
  were corrected in `ed3048d`, and `b12`/`b13` supersede `b2`/`b10`. The
  `d11` record's own text still says "approved d4: serve owns the lamp",
  which means `d3`.

## Drift From Plan

| Plan item | Reason for divergence | Classification |
|-----------|-----------------------|----------------|
| `t7` (`d3`) | culture-rules runner commands get only PATH and LANG (`culture_rules/actors/code.py:65,175`), so `lamp_scene` could not see the key. /assess: `route_cloud_via_serve` 0.631. | risky |
| `t4` (`d4`) | binary absent during t4 | needs-follow-up |
| `t2` (`d5`) | t2 agent choice (UTC epoch REAL) | needs-follow-up |
| `t10` (`d7`) | t3 previews each committed control change (10-30/min while adjusting). /assess: `skip_web_previews` 0.496. | acceptable |
| `t7` (`d11`) | User: fade-in and fade-out are critical for working with the lamp; design choices answered by the user. Govee APIs expose no transition time. | needs-follow-up |
| `t10` | The plan's loopback-only `/api/lan` proxy was replaced by attribution headers that are honoured only from loopback on the public routes (follows `d3`). | acceptable |
| `t5` (`d15`, proposed) | The real lamp never answered multicast. Discovery now goes cache, then `GOVEE_LAN_HOSTS`, then multicast, then a /24 sweep. | acceptable |
| `t4` / `t12` (`d21`, proposed) | The actor gained `lamp_fade_on`/`lamp_fade_off` (`d11`), and every command passes `--source rules`. | acceptable |
| `t3` | Two layout defects survived the `t3` merge and were fixed in `t13`: the bulb shifted 22 px when the panes switched, and the controls overflowed at 390 px. | acceptable |
| `t14` (`d26`, proposed) | The user-only checks were moved to post-merge follow-ups. The agent has no phone and cannot see the lamp, and access to the user's culture-rules token was refused. /assess: `defer_to_gate3` 0.824. | needs-follow-up |
| `t14` (`d22`, proposed) | A fade-out leaves the lamp off at brightness 1. The Govee app or the button then light it at 1 %; govee itself restores the level. | needs-follow-up |

## Evidence

- tests: the full suite is 605 passed and 1 skipped (the pre-existing
  cross-repo report) at `ed3048d`; coverage is 92.21 % (floor 60 %).
- evidence records `e1`–`e21` against obligations `o1`–`o19`. All pass. They
  are filed as proposed, so they await the user's confirmation. The tests
  behind them are named in each record and were run at `ef5689d`.
- sensitivity shown: `tests/test_stats_store.py::test_concurrent_first_connect_never_raises_locked`
  failed 3 of 3 runs before `115ea44`. The Playwright layout check failed
  before `62cffe6`.
- lint: black, isort, flake8, bandit, `scripts/scan-secrets.py`, markdownlint,
  and `GOVEE_DOCTOR_NO_SCAN=1 teken cli doctor . --strict` (26/26) all pass.
- live: `docs/live-test-results/2026-10-29-h6076.md`, measured on the real
  H6076.
- behavioural deltas: `b1`, `b3`–`b9`, `b11`–`b13` (`b2` and `b10` are
  superseded).
- commits: `341ad19..391c778` on `lamp/integration`, including the
  ask-colleague review fixes in `391c778`. That review raised 6 findings:
  3 were real and fixed, 3 were checked and rejected.
- PRs: #3. Every check passed at `9549939` (SonarCloud new-code coverage
  92.3 %), and it is mergeable.

## Delivery Claims

| Claim | Confidence | Evidence |
|-------|------------|----------|
| The runtime has no third-party dependencies, and the transports use only the stdlib (`c2`) | high | `tests/test_boundaries.py::test_every_import_is_stdlib_or_first_party` · `e1` |
| Every write is a dry run unless `--apply`/`apply:true`, and apply sends exactly the previewed commands (`c4`) | high | `tests/test_lamp_service.py::test_apply_sends_exactly_the_previewed_commands` · `e3` · live dry run left the lamp unchanged |
| LAN control of the real H6076 works, with verified writes (`c30`) | high | live run: 14 applied writes were all confirmed · `e13`, `e14` |
| Fades work on the real lamp and can be cancelled (`d11`) | high | live: 33-step ramp confirmed; background fade cancelled at 34 % · `tests/test_fade.py` |
| Every API route except health needs a token, writes need the same Origin, bodies are capped, and the token never reaches the logs (`c20`, `c32`) | high | `e7`, `e16` · live: token in 0 log lines; a revoked token got 401 |
| The MCP server lets an agent dry-run and then apply (`c23`) | high | `tests/test_mcp_server.py::test_subprocess_dry_run_then_apply` · `e9` · live MCP read and dry run |
| The console previews colour without a request, and the bulb never moves (`c26`) | high | `e11` (sensitivity, Playwright) |
| A web toggle changes the lamp in under 2 s (`c22`) | medium | measured over loopback: 0.62–0.72 s API, 65 ms console tap. **A real phone on the LAN was not tested.** |
| The stats charts render from sqlite rows and show an empty state when there are none (`c21`) | medium | `e8` is at coverage strength only. The queries and the API are tested; the rendering was only observed (t3 Playwright with a fake API). |
| The culture-rules actor imports cleanly (`c25`, `c34`) | low | shape tests only (`e10`, `e18`). `culture-rules actors create` was never run; it needs the user's token (lapse `l3`, still proposed). |
| Cloud fallback, scenes and rate-limit handling (`c35`) | medium | `e19` against a fake HTTP server only. **No real key was available**, and the header names are unverified (risk `r2`). |
| `systemd-analyze verify` passes on the shipped unit (`c36`) | high | `e21` · exit 0 after `uv tool install` |

Lapse ledger: `l1`–`l6` are all **proposed**, so they await the user and are
not yet evidence. If approved, `l3` caps `c25` (already low), `l2` bears on
the token store's concurrency, and `l5`/`l6` record process failures that
have since been fixed.

## Remaining Work / Follow-up

- `t14` follow-ups (`d26`). Owner: the user.
  - The phone toggle over Wi-Fi or Tailscale.
  - A visual check of whether the firmware smooths instant changes.
  - `govee rules actor > /tmp/a.json`, then
    `culture-rules actors create --body @/tmp/a.json` as a dry run.
- Adjudication — confirm or reject the proposed deviations (`d1`, `d2`, `d6`,
  `d8`–`d10`, `d12`–`d23`), lapses `l1`–`l6`, evidence `e1`–`e21` and deltas.
  Owner: the user, at gate 3.
- `d16` — decide whether the devague-added `.gitignore` lines
  (`.devague/reviews/`, `.devague/questions/`) stay.
- Cloud verification — once `GOVEE_API_KEY` is set, run `govee light scenes`
  and a scene apply, and confirm the rate-limit header names (risk `r2`).
- Known limitations, documented and not fixed:
  - after a govee fade-out, the Govee app or the button turn the lamp on at
    1 %;
  - there is no favicon;
  - fades cannot be cancelled across two CLI processes without serve;
  - the console is not supported behind an HTTPS reverse proxy (`d20`).
