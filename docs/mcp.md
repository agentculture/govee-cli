# MCP server

`govee mcp serve` exposes the lamp to MCP clients over stdio. This repo
commits a `.mcp.json` that launches it, so an agent working in this checkout
gets the tools automatically.

## Snippet for other agent repos

Add this to the repo's `.mcp.json` (or your client's MCP config). `govee` must
be on `PATH`.

```json
{
  "mcpServers": {
    "govee": {
      "command": "govee",
      "args": ["mcp", "serve"]
    }
  }
}
```

Do not put the API key in the config. The server reads `GOVEE_API_KEY` from
its environment.

## Tools

| Tool | Kind | Purpose |
|------|------|---------|
| `list_devices` | read | List known Govee devices |
| `get_state` | read | Current state of a device |
| `set_light` | write | On/off, brightness, color, color temperature |
| `set_scene` | write | Activate a scene |
| `get_stats` | read | Usage statistics |

## Dry-run rule

`set_light` and `set_scene` only describe what they would send unless the call
passes `apply: true`. Read tools need no flag.

## Attribution

Writes are attributed to the `GOVEE_ACTOR` environment variable, or to the MCP
client name when it is unset.

## Fades

A `set_light` call with a fade returns as soon as the fade starts, whether or
not `govee serve` runs: without serve, the MCP server runs the fade on a
background thread, so `get_fade` and `cancel_fade` keep working while it runs.
When the client closes stdin, the server waits for a running fade to finish
before it exits.

## Using `govee serve`

The MCP server checks for `govee serve` on every tool call, so starting or
stopping the daemon during a session takes effect on the next call.

When the MCP server routes through the `govee serve` daemon, each agent needs
its own token:

```bash
govee token new --name <agent>
```

To run the daemon as a user service, see `docs/systemd/govee-serve.service`.
Put `GOVEE_API_KEY=...` in `$XDG_CONFIG_HOME/govee-cli/env` (default `$HOME/.config`) (outside the repo, mode
600); the unit reads it with `EnvironmentFile`.
