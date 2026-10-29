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

Every `argv` starts with the absolute path of the `govee` binary and ends with
`--apply --json`. Rules act on purpose, so the dry-run default is switched
off. No `argv[0]` is a shell or interpreter, so the culture-rules inline guard
(`culture_rules/actors/inline_guard.py`) never refuses them.

## Import

The definition assumes `govee` is at `/home/spark/.local/bin/govee`, where
`uv tool install govee-cli` puts it. If yours lives elsewhere, edit
`argv[0]` in all six commands before importing (`which govee`).

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

## Secrets

The runner starts commands with a minimal environment (`PATH` and `LANG`
only). The LAN commands need no key. `lamp_scene` goes through the Govee cloud
and needs `GOVEE_API_KEY`. Never put the key in the actor JSON: culture-rules
refuses literal secrets, and a `grant:<NAME>` reference only helps where
culture-rules injects the value into the process. As of this writing the
code runner does not forward any environment to registered commands, so
verify that `lamp_scene` can see the key on your setup before relying on it
(see "How secrets work here" in the culture-rules `docs/actors/README.md`).
