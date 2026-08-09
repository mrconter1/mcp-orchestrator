# mcp-orchestrator - design handoff

Written 2026-08-09 by the session that designed this, for the session that builds it.
Everything here was either decided by the user or verified on this machine. Where
something is unverified it says so.

## First real task: a restart that is already pending

`queue-mcp` has two commits (`51ce07c`, `a7391bf`) that are **not live**. The
running server holds the old code in memory, so `~/.queue-mcp/history.jsonl`
does not exist yet and nothing is being recorded. The log starts at the first
completion after the server restarts.

The user deliberately chose not to restart it by hand. Restarting the queue
server detaches it from every running session, because Claude Code only
attaches MCP servers at startup, so a bare restart silently costs those
sessions their queue tools. That is exactly the problem requirement 3 exists to
solve, which makes this the natural first exercise of the whole flow rather
than a special case:

1. restart the `queue` server
2. write the pending-restart marker
3. let each running session pick it up at its next natural stop and resume
   itself, keeping its transcript and its queue

Do not paper over it with a manual restart. If it cannot be done through the
orchestrator yet, say so rather than doing it by hand, because the manual path
is what hides whether requirement 3 actually works.

Two known defects in this document, flagged but not corrected, because the user
did not ask for the edit:

- The session-registry section below overstates the problem. Resumed sessions
  carry `--resume <session-id>` on their command line and `--name <name>` if
  named, so most sessions can be identified with no hook at all. The
  SessionStart hook is only needed for freshly started ones.
- The Windows trap list predates one more trap, worth adding before spawning
  anything: Windows Terminal's `closeOnExit` defaults to `graceful`, closing a
  tab only on exit code **0**. Killing a tab's shell therefore *keeps* the tab
  open on `[process exited with code ...]`. Make the shell exit 0 instead
  (`try{...}finally{exit 0}`), and note `wt` forbids `;` in that string.

## What it is

A standalone HTTP MCP server that manages the user's other local MCP servers:
creates them, starts and stops them, tracks what is running, autostarts them at
logon, and restarts Claude Code sessions when the server set changes.

It is deliberately **not** merged into `session-control-mcp`. The user chose a
standalone orchestrator, and chose that it **also owns session restarting**.
`session-control-mcp` (`Repos\session-control-mcp`, port 8767) stays as the
interactive session tool; read its `procs.py` before writing any spawn code,
because it already solved most of the Windows traps listed at the bottom.

## The constraint everything else follows from

**Claude Code attaches MCP servers only at process startup, and never retries.**
Verified this session: the server was listening and healthy on 8767, and the
running session still could not see it until Claude Code was restarted. There is
no lazy retry and `/mcp reconnect` does not recover it.

So the orchestrator can restart a server, but it cannot make a *running* session
see the restarted server. Any "install a new MCP" flow must end in a session
restart or it is not finished. That is why requirement 3 exists.

## Requirements (user's words, lightly structured)

1. **Create new HTTP MCPs easily.** Hidden by default (no console window),
   autostart on logon.
2. **Stop them, and turn autostart off.**
3. **Install a new MCP and restart the running sessions.** Not immediately: at
   each session's next natural stop, before it continues the queue. The session
   announces something like `Detecting pending mcp restart...` and restarts
   itself, resuming its own transcript.

`/rc` in the user's description means **remote control**, which is already on by
default via `remoteControlAtStartup: true` in `~/.claude/settings.json`. No
special handling needed; the replacement session picks it up from settings.

## Architecture

One orchestrator process, hidden, started by a single Task Scheduler task at
logon. It starts every server marked autostart. Exactly one thing the OS has to
supervise; the orchestrator supervises the rest.

**Not a Windows Service.** Services run in session 0, isolated from the desktop,
so a service-hosted orchestrator physically cannot open an interactive terminal
window or tab. Task Scheduler with an *at log on* trigger running as the user is
the right mechanism: no admin, no window, runs in the desktop session, survives
reboot, and has restart-on-failure built in.

**Config**: `servers.json` - name, command, cwd, port, autostart, enabled.
**Logs**: `logs/<name>.log`, stdout and stderr redirected per server.
**Processes**: launched with `CREATE_NO_WINDOW`.

### Tools

- `mcp_list` - configured servers plus live state (port probe + pid liveness)
- `mcp_start` / `mcp_stop` / `mcp_restart`
- `mcp_autostart(name, on|off)`
- `mcp_logs(name, tail)`
- `mcp_scaffold(name)` - generate repo from template, create venv, allocate a
  free port, write a server stub, register in `servers.json`, run
  `claude mcp add --transport http`
- `sessions_list` - from the session registry below
- `sessions_restart` / pending-restart marker management

## Session registry (needed for requirement 3)

**Pid to session-id mapping is impossible from outside.** Verified: `psutil`
can read another process's environment on Windows, but `claude.exe` itself does
not carry `CLAUDE_CODE_SESSION_ID`. Claude *sets* those vars for the processes
it spawns, so a shell inside a session sees them and the parent does not.

The way in is a **SessionStart hook**, which runs inside the session where both
halves are visible. Verified present in a Claude-spawned shell:

- `CLAUDE_PID` - the owning `claude.exe` pid (matched exactly)
- `CLAUDE_CODE_SESSION_ID` - the transcript/session id

The hook writes `{pid, session_id, cwd, started}` to a registry file the
orchestrator reads. Prune entries whose pid is dead or is no longer a
`claude.exe`.

## Restart choreography

Decentralised. No central kill, so nothing dies mid-turn.

1. An install or change writes `pending-restart.json` with a timestamp.
2. A **Stop hook** fires at each session's natural end of turn. It is a cheap
   file-existence check. If a marker exists that this session has not consumed,
   it announces the pending restart and triggers it.
3. The replacement resumes the **same session id**, so transcript and queue both
   survive. Verified this session: `--resume <id>` without `--fork-session`
   reuses the id rather than minting a new one, and the per-session queue was
   still intact afterwards.

### Traps to design against, not discover

- **Restart storms.** Consume the marker **per session**, keyed
  `{session_id: marker_timestamp}`. One global "handled" flag means the first
  session to restart cancels it for all the others; no key at all means sessions
  restart forever.
- **Hidden means silent.** With no windows, a crashed server is
  indistinguishable from a missing tool - which is exactly the failure that cost
  two debugging rounds today. Port probes and log files are load-bearing.
- **The orchestrator cannot install itself.** It must be listening before Claude
  Code starts or Claude cannot see its tools. Task Scheduler owns it, with
  restart-on-failure.
- **The Stop hook runs every turn.** Keep it to a file check, nothing heavier.

## Windows traps already paid for in session-control-mcp

Read `Repos\session-control-mcp\session_control\procs.py`. Each of these cost a
debugging round today:

- **`shutil.which('wt.exe')` returns None** even though Windows Terminal is
  installed. It is an App Execution Alias in
  `%LOCALAPPDATA%\Microsoft\WindowsApps`, which is not on PATH in the server's
  environment. Fall back to that path directly.
- **`wt -w 0 new-tab ...`** opens a tab in the *current* terminal window. This
  is what the user wants for spawned sessions.
- **Windows Terminal parses `;` as its own command separator**, so a command
  string handed to `wt` must not contain one. Use `-d <dir>` rather than a
  `Set-Location ...;` prefix.
- **`cmd /c start` reads its first unquoted token as the program to run**, not
  as a window title, so `start my-title powershell ...` tries to execute
  `my-title`. Avoid `start` entirely.
- **`DETACHED_PROCESS` gives a console app no console**, so a process spawned
  that way appears to start (you get a pid) and dies invisibly. Use
  `CREATE_NEW_CONSOLE` for a visible window, or `CREATE_NO_WINDOW` for a
  genuinely headless server.
- **Scrub the session env markers before spawning a Claude session.** This
  server is started from inside a session, so `CLAUDE_CODE_CHILD_SESSION`,
  `CLAUDE_CODE_SESSION_ID`, `CLAUDE_CODE_BRIDGE_SESSION_ID`,
  `CLAUDE_CODE_ENTRYPOINT`, `CLAUDE_PID` and `CLAUDECODE` all leak into the
  child. A `claude` that starts with them set treats itself as a child session
  and **turns transcript saving off**, leaving the new session unresumable.
- **Start servers from a terminal the user owns, or detached.** A server started
  as a Claude background task dies when that session tears down.

## What was built from this

All of the above, plus two things this document did not anticipate:

- **The orchestrator needs its own log.** Under Task Scheduler there is no
  console at all, so `sys.stdout` is None and the first log line kills the
  process. It started, held a pid, and never opened its port. Exactly the
  failure this design was written to prevent, arriving through a door the
  design did not think to close.
- **Resources must be measured across the process tree.** The command being
  launched is usually a thin launcher that re-executes the real interpreter,
  and it is the child that holds the socket.

One assumption here also turned out to be too strong. "Claude Code attaches MCP
servers only at process startup" is true for *adding* a server, but restarting
an already-attached one is fine: the HTTP client reconnects per call. Only
adding and removing require a session restart.
