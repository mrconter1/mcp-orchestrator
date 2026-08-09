# mcp-orchestrator

Manages the local MCP servers on this machine. Starts them hidden at logon,
tracks whether they are actually up, and restarts Claude Code sessions when the
set of servers changes.

One hidden process with a tray icon, started by a Scheduled Task at logon,
exposing itself as an HTTP MCP server on `http://127.0.0.1:8768/mcp`.

## Install

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\install\install.ps1
claude mcp add --transport http --scope user orchestrator http://127.0.0.1:8768/mcp
```

`install.ps1` registers the Scheduled Task (at logon, as you, no admin) and adds
a SessionStart and a Stop hook to `~/.claude/settings.json`. Hooks apply to
sessions started afterwards. Undo with `.\install\uninstall.ps1`.

## Tools

`mcp_list`, `mcp_status`, `mcp_start`, `mcp_stop`, `mcp_restart`,
`mcp_autostart`, `mcp_enable`, `mcp_logs`, `mcp_stats`, `mcp_add`,
`mcp_scaffold`, `mcp_remove`, `sessions_list`, `sessions_restart_pending`,
`sessions_restart`, `sessions_restart_status`, `sessions_restart_clear`,
`orchestrator_info`.

## Server states

| State | Meaning |
| --- | --- |
| `running` | process alive and the port answers |
| `starting` | started moments ago, port not up yet |
| `unhealthy` | alive but not listening: crash loop or bad config |
| `crashed` | we started it and it died on its own |
| `external` | the port answers but the orchestrator did not start it |
| `stopped` | not running, and nobody expected it to be |

`crashed` is deliberately distinct from `stopped`. A hidden server has no window
to die in, so this state is the only evidence you get.

## Creating a server

```
mcp_scaffold("weather")
```

Generates `~/Repos/weather-mcp`, builds a venv with the MCP SDK, allocates a
free port, registers it with Claude Code, and starts it. The generated server
has one working tool, so a successful call means the whole chain works and all
that is left is writing real tools in `weather_mcp/server.py`.

## When a session needs restarting

Claude Code attaches MCP servers at process startup and never retries. So
**adding or removing** a server is invisible to sessions already running until
they restart. `mcp_add`, `mcp_scaffold` and `mcp_remove` request that restart by
default.

**Restarting** a server needs nothing: the HTTP client reconnects per call, so a
running session keeps working across `mcp_restart`.

The restart itself is decentralised. A change writes `pending-restart.json`;
each session's Stop hook sees it at the end of a turn, when nothing is in
flight, and restarts that session resuming the same transcript id, so history
and the per-session queue survive. The marker is consumed per session and keyed
by a uuid, so one session restarting does not cancel it for the others, and two
changes in the same second stay distinct.

## Files

State lives in `~/.mcp-orchestrator`, outside the repo, because the hooks must
find it without knowing where the repo was cloned.

| File | Contents |
| --- | --- |
| `servers.json` | the configured servers, hand-editable |
| `running.json` | pids and statistics |
| `sessions.json` | session registry, written by the SessionStart hook |
| `pending-restart.json` | the restart marker, when set |
| `restart-consumed.json` | which session handled which marker |
| `logs/<name>.log` | each server's stdout and stderr |
| `logs/orchestrator.log` | the orchestrator's own log |

```json
{
  "name": "weather",
  "command": ["C:/Users/you/Repos/weather-mcp/.venv/Scripts/python.exe", "-m", "weather_mcp.server"],
  "cwd": "C:/Users/you/Repos/weather-mcp",
  "port": 8769,
  "path": "/mcp",
  "env": {},
  "autostart": true,
  "enabled": true
}
```

## Tray icon

Green: everything up. Amber: something stopped or starting. Red with an
exclamation: something crashed or is not listening. Grey: nothing managed.

The menu is rebuilt each time it opens: start, stop and restart per server, its
log, and a way to ask the sessions to restart. Quitting leaves servers running.

## Troubleshooting

| Symptom | Look at |
| --- | --- |
| orchestrator did not come up | `~/.mcp-orchestrator/logs/orchestrator.log`, written even with no console |
| a server is `crashed` or `unhealthy` | `mcp_logs("<name>")` |
| a server is `external` | something else holds that port; the orchestrator will not touch it |
| a new tool is missing | the session has not restarted; check `sessions_restart_status` |
| sessions listed as `unregistered` | they started before the SessionStart hook was installed |

## Known gaps

- A crashed server is reported but not automatically restarted.
- The at-logon trigger is configured but has only been triggered manually.
- The live session restart path (`wt.exe` spawn plus `--resume`) is unproven.
- `orchestrator.log` does not rotate; per-server logs rotate at 5 MB.

## Not a Windows Service

Services run in session 0, isolated from the desktop, so a service could not
show a tray icon or open a terminal tab. A Scheduled Task with an at-logon
trigger runs as the user in their own desktop session, needs no admin, survives
reboot, and has restart-on-failure built in.
