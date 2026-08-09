# mcp-orchestrator

Manages the local MCP servers on this machine: creates them, starts and stops
them, keeps them running at logon, and restarts Claude Code sessions when the
server set changes.

It runs as one hidden process with a tray icon, started by a Scheduled Task at
logon, and exposes itself as an HTTP MCP server on `http://127.0.0.1:8768/mcp`.

## The constraint everything follows from

**Claude Code attaches MCP servers only at process startup, and never retries.**
A server can be running, healthy and registered, and still be invisible to a
session that was already open. `/mcp reconnect` does not recover it.

So installing a server is not finished when it starts. It is finished when the
sessions have restarted. That is what the Stop hook is for, and why `mcp_add`
and `mcp_scaffold` request a restart by default.

## Install

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\install\install.ps1
```

That registers the Scheduled Task (at logon, as you, no admin) and wires two
hooks into `~/.claude/settings.json`. The hooks apply to sessions started
afterwards, not to the one you ran the installer from.

Then register the orchestrator itself with Claude Code, once:

```powershell
claude mcp add --transport http --scope user orchestrator http://127.0.0.1:8768/mcp
```

Undo all of it with `.\install\uninstall.ps1`.

## Tools

| Tool | What it does |
| --- | --- |
| `mcp_list` | every managed server with live state |
| `mcp_status` | one server in detail |
| `mcp_start` / `mcp_stop` / `mcp_restart` | lifecycle |
| `mcp_autostart` / `mcp_enable` | start at logon, or park a server |
| `mcp_logs` | the tail of a server's log |
| `mcp_stats` | uptime, availability, crashes, memory, CPU |
| `mcp_add` | manage a server that already exists on disk |
| `mcp_scaffold` | create a new one from scratch |
| `mcp_remove` | stop managing one |
| `sessions_list` | running Claude Code sessions and their transcript ids |
| `sessions_restart_pending` | ask every session to restart at its next stop |
| `sessions_restart` | restart one session now |
| `sessions_restart_status` / `sessions_restart_clear` | inspect or cancel |
| `orchestrator_info` | health of the orchestrator itself |

## States

`mcp_list` reports one of these per server. The distinctions matter:

| State | Meaning |
| --- | --- |
| `running` | process alive and the port answers |
| `starting` | started moments ago, port not up yet |
| `unhealthy` | process alive but not listening - crash loop or bad config |
| `crashed` | we started it and it died on its own |
| `external` | the port answers but the orchestrator did not start it |
| `stopped` | not running, and nobody expected it to be |

`crashed` is deliberately not the same as `stopped`. A hidden server has no
window to die in, so this state is the only evidence you get.

## Creating a server

```
mcp_scaffold("weather")
```

Generates `~/Repos/weather-mcp`, builds a venv with the MCP SDK, allocates a
free port, registers it with Claude Code, starts it, and asks the running
sessions to restart. The generated server already has one working tool, so if
the call succeeds the whole chain works and all that is left is writing real
tools in `weather_mcp/server.py`.

After editing tools: `mcp_restart("weather")` for a new process, then
`sessions_restart_pending()` so sessions see the new tool list.

## How the session restart works

1. A change writes `pending-restart.json`.
2. Each session's **Stop hook** fires when its turn ends, sees the marker, and
   restarts that session in a new tab resuming the same transcript id.
3. The marker is consumed **per session**, keyed by a marker id.

Nothing is killed from outside at an arbitrary moment: a session only ever
restarts at its own natural stop, when nothing is in flight. Because the
transcript id is preserved, history and the per-session queue both survive.

The keying matters in both directions. One global "handled" flag would let the
first session to restart cancel it for all the others; no key at all and every
session would restart forever. The marker carries a uuid rather than a
timestamp, so two changes within the same second stay distinguishable.

## Files

Everything mutable lives in `~/.mcp-orchestrator`, outside this repo, because
the hooks have to find it without knowing where the repo was cloned.

| File | Contents |
| --- | --- |
| `servers.json` | the configured servers - hand-editable |
| `running.json` | pids and statistics |
| `sessions.json` | session registry, written by the SessionStart hook |
| `pending-restart.json` | the marker, when one is set |
| `restart-consumed.json` | which session handled which marker |
| `logs/<name>.log` | each server's stdout and stderr |
| `logs/orchestrator.log` | the orchestrator's own log |
| `backups/` | settings.json backups from the installer |

`servers.json` entry:

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

## The tray icon

Green: everything up. Amber: something stopped or starting. Red with an
exclamation: something crashed or is alive but not listening. Grey: nothing
managed yet.

The menu is rebuilt each time it opens, and has start/stop/restart per server,
its log, and a way to ask the sessions to restart. Quitting leaves the managed
servers running.

## When something is wrong

The orchestrator did not come up: `~/.mcp-orchestrator/logs/orchestrator.log`.
It is written even when there is no console, which is the normal case under
Task Scheduler.

A server says `crashed` or `unhealthy`: `mcp_logs("<name>")`. That is where its
stdout and stderr go.

A server says `external`: something else already holds that port. The
orchestrator refuses to start or kill it, because it did not start it.

A new tool is not showing up: the session has not restarted. Check
`sessions_restart_status`.

`sessions_list` shows sessions under `unregistered`: those started before the
SessionStart hook was installed. They cannot be restarted by session id until
they have started once with it in place.

## Not a Windows Service

Services run in session 0, isolated from the desktop, so a service-hosted
orchestrator could not show a tray icon or open a terminal tab. A Scheduled Task
with an at-logon trigger runs as the user in their own desktop session, needs no
admin, survives reboot, and has restart-on-failure built in.
