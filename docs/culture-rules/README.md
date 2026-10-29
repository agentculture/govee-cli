# govee-cli as a culture-rules actor

`govee-actor.json` registers the lamp with
[culture-rules](https://github.com/agentculture/culture-rules) as an actor of
kind `runner` on machine `spark`. Rules and workflows can then call named lamp
commands. govee-cli ships no schedules; when a command fires is the rule's
business.

## Commands

The runner registry lives in `params.commands`. Each entry has an `argv` list
(started with `shell=False`), typed `params`, and a `timeout`.

| Command | Calls | Params |
|---------|-------|--------|
| `lamp_on` | `govee light on` | none |
| `lamp_off` | `govee light off` | none |
| `lamp_brightness` | `govee light brightness {pct}` | `pct` (int) |
| `lamp_color` | `govee light color {hex}` | `hex` (string, no `#`) |
| `lamp_temp` | `govee light temp {kelvin}` | `kelvin` (int) |
| `lamp_scene` | `govee light scene {name}` | `name` (string) |
| `lamp_fade_on` | `govee light on --fade {seconds}` | `seconds` (int, 0-3600) |
| `lamp_fade_off` | `govee light off --fade {seconds}` | `seconds` (int, 0-3600) |

`lamp_on` and `lamp_off` already fade over the default 1.5 s. The two fade
commands have a 3660 s timeout so a long fade (a 20-minute wake-up, say) is
never killed mid-way; with `govee serve` running they return at once anyway.

Every `argv` starts with the absolute path of the `govee` binary and ends with
`--source rules --apply --json`. Rules act on purpose, so the dry-run default is switched
off. No `argv[0]` is a shell or interpreter, so the culture-rules inline guard
(`culture_rules/actors/inline_guard.py`) never refuses them.

## Import

The definition assumes `govee` is at `/home/spark/.local/bin/govee`, where
`uv tool install govee-cli` puts it. If yours lives elsewhere, edit
`argv[0]` in all eight commands before importing (`which govee`).

Run the import with your `CULTURE_RULES_TOKEN` in the environment. Without
`--apply` it is a dry run:

```bash
culture-rules actors create --body @docs/culture-rules/govee-actor.json          # dry run
culture-rules actors create --body @docs/culture-rules/govee-actor.json --apply
```

A bundle directory works with `culture-rules actors import <dir> --apply`
too, if you copy the JSON into one.

## Example rule

CI failed, so turn the lamp red. The step invokes the registered command by
name with typed arguments:

```json
{ "actor": "spark-govee", "command": "lamp_color", "args": { "hex": "FF0000" } }
```

That runs `/home/spark/.local/bin/govee light color FF0000 --apply --json`.
Wire it to your CI-failed trigger in the rule or workflow as usual.

## Run it with govee serve

Keep `govee serve` running on spark (see `docs/systemd/govee-serve.service`).
While it runs, every lamp command routes through it, so:

- the API key only has to live in serve's environment
  (`~/.config/govee-cli/env`), which also makes `lamp_scene` work even though
  runner commands start with only `PATH` and `LANG`;
- fades run inside serve, outlive the command, and a later command cancels
  them;
- the runner needs a client token. Its environment has no `GOVEE_TOKEN`, so
  save one to the file govee reads instead, once, as the same user:

  ```bash
  govee token new --name culture-rules --save
  ```

Every command passes `--source rules`, so the event log and the console's
"Who changes the lamp" chart show these changes as Rules. Without serve, LAN commands still work directly, but
`lamp_scene` cannot see `GOVEE_API_KEY` (the runner forwards no environment)
and a long fade blocks the step until it finishes.
