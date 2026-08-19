# mcp-orchestrator

Manages the local MCP servers on a Windows machine. Installs them, starts them
hidden at logon, restarts them when they crash, and shows the lot in a tray
icon.

One hidden process, started by a Scheduled Task, serving MCP on
`http://127.0.0.1:8768/mcp`.

## Install

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\install\install.ps1
claude mcp add --transport http --scope user orchestrator http://127.0.0.1:8768/mcp
```

Registers the Scheduled Task and adds two Claude Code hooks. Undo with
`.\install\uninstall.ps1`.

## Installable servers

```
mcp_install("queue")
```

| Server | Repository | What it does |
| --- | --- | --- |
| `session` | [session-control-mcp](https://github.com/mrconter1/session-control-mcp) | List, open, fork, close and restart Claude Code sessions |
| `queue` | [queue-mcp](https://github.com/mrconter1/queue-mcp) | A task queue you fill from another terminal |

`mcp_install` also takes any git URL, or a local path for a private repo. It
expects the ordinary Python layout: a package containing `server.py`, with
`requirements.txt` beside it.

## Tools

| Tool | Purpose |
| --- | --- |
| `mcp_list` / `mcp_status` / `mcp_stats` | what is running, and how it has behaved |
| `mcp_start` / `mcp_stop` / `mcp_restart` | lifecycle |
| `mcp_catalogue` / `mcp_install` / `mcp_uninstall` | install and remove servers |
| `mcp_scaffold` | write a new server from a template |
| `mcp_add` / `mcp_remove` | manage a server already on disk |
| `mcp_autostart` / `mcp_enable` | start at logon, or park a server |
| `mcp_logs` | a hidden server's only voice |
| `sessions_*` | list sessions, and restart them when the server set changes |

## What it does about failure

A hidden server has no window to die in, so the whole design is about not
letting a dead one look like a missing tool.

- **States distinguish causes.** `crashed` (died on its own) is not `stopped`
  (somebody stopped it), and `unhealthy` (alive, not listening) is not either.
  `external` means the port answers but the orchestrator did not start it, and
  will not touch it.
- **A start waits for the port** before reporting success. A pid proves
  nothing.
- **Servers outlive the orchestrator.** On startup it re-adopts anything still
  running its configured command, matched by command line, so a server started
  by a previous orchestrator is supervised rather than written off as
  `external` and left to fend for itself.
- **Anything down is restarted** on a 5, 15, 45, 120, 300 second ladder, then
  abandoned with a notification. Two minutes of health clears the count. Down
  means down: `crashed`, `unhealthy`, or simply not running. The one exception
  is a server stopped through `mcp_stop`, which is recorded as deliberate at the
  moment it is asked for and left alone until it is started again.
- **The tray colour is the summary.** Green up, amber starting or stopped, red
  with an exclamation for trouble.

## Sessions and the restart dance

Claude Code attaches MCP servers at startup and never retries. **Adding or
removing** a server is invisible to running sessions until they restart, so
`mcp_install`, `mcp_scaffold`, `mcp_add` and `mcp_uninstall` ask them to.
**Restarting** a server needs nothing; the client reconnects per call.

The restart is decentralised. A change drops a marker, and each session's Stop
hook acts on it at the end of a turn, when nothing is in flight, resuming the
same transcript so history and queue survive.

## Files

State lives in `~/.mcp-orchestrator`, outside this repo, because the hooks must
find it without knowing where the repo was cloned: `servers.json` (hand
editable), `running.json`, `sessions.json`, the restart markers, and `logs/`.

## Known gaps

- The at-logon trigger is configured but has only ever been fired manually.
- The live session restart path (`wt.exe` plus `--resume`) is unproven.
- `orchestrator.log` does not rotate; per-server logs rotate at 5 MB.
- Windows only, throughout.

[DESIGN.md](DESIGN.md) has the reasoning, including why this is a Scheduled
Task and not a Windows Service, and the Windows traps that cost a debugging
round each.
