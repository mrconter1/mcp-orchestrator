"""HTTP MCP server that manages the user's other local MCP servers.

    .venv\\Scripts\\python.exe -m orchestrator.server

Binds 127.0.0.1 only. It runs hidden from Task Scheduler at logon, starts every
server marked autostart, and stays up as the one thing the OS has to supervise.

The MCP endpoint runs on a background thread so the main thread stays free for
the tray icon, which on Windows has to own it.
"""

from __future__ import annotations

import argparse
import datetime
import os
import sys
import threading
import time
import traceback
from typing import Any

from mcp.server.mcpserver import MCPServer

from orchestrator import (
    __version__,
    claude_cli,
    config,
    monitor,
    paths,
    registry,
    restart,
    scaffold,
    supervise,
)
from orchestrator.config import ServerSpec

HOST = os.environ.get("MCP_ORCHESTRATOR_HOST", "127.0.0.1")
PORT = int(os.environ.get("MCP_ORCHESTRATOR_PORT", "8768"))
BOOT_TIME = time.time()

server = MCPServer(
    "orchestrator",
    instructions=(
        "Manages the local HTTP MCP servers on this machine: list them with "
        "live health, start/stop/restart them, turn autostart on or off, read "
        "their logs, and scaffold new ones. Claude Code attaches MCP servers "
        "only at process startup, so a newly started or newly registered server "
        "is invisible to the running session until it restarts -- after any "
        "change that adds or removes a server, tell the user a session restart "
        "is needed. Prefer mcp_list before acting: it shows which servers are "
        "running, crashed, or held by a process the orchestrator did not start."
    ),
)


def _ok(**payload: Any) -> dict[str, Any]:
    return {"ok": True, **payload}


def _err(message: str, **payload: Any) -> dict[str, Any]:
    return {"ok": False, "error": message, **payload}


def _finish_install(reason: str, restart_sessions: bool) -> dict[str, Any]:
    """The last step of any change to the server set.

    Registering a server is not the end of installing it. Sessions already
    running attached their MCP servers at startup and never retry, so without
    this they keep the old tool list until the user happens to restart them --
    and the change looks like it silently did nothing.
    """
    if not restart_sessions:
        return {
            "note": (
                "Not requested. Running sessions will not see this change until they "
                "restart; call sessions_restart_pending when ready."
            )
        }
    restart.request(reason)
    waiting = restart.status()["waiting"]
    return {
        "sessions_pending_restart": waiting,
        "note": (
            f"{len(waiting)} running session(s) will restart at the end of their next "
            f"turn and resume the same transcript. Until then they cannot see this change."
        ),
    }


def _require(name: str) -> ServerSpec:
    spec = config.get(name)
    if spec is None:
        known = ", ".join(s.name for s in config.load()) or "none configured"
        raise LookupError(f"no server named {name!r}. Configured: {known}")
    return spec


@server.tool()
def mcp_list() -> dict[str, Any]:
    """List every managed MCP server with its live state.

    States: running (process alive and port answering), starting, unhealthy
    (alive but not listening), crashed (died on its own since we started it),
    external (port answers but the orchestrator did not start it), stopped.
    """
    specs, errors = config.load_with_errors()
    servers = supervise.status_all(specs)
    counts: dict[str, int] = {}
    for entry in servers:
        counts[entry["state"]] = counts.get(entry["state"], 0) + 1
    return _ok(servers=servers, count=len(servers), states=counts, config_errors=errors)


@server.tool()
def mcp_status(name: str) -> dict[str, Any]:
    """Live state of one managed server, including uptime, crashes and last error."""
    try:
        return _ok(**supervise.status(_require(name)))
    except LookupError as exc:
        return _err(str(exc))


@server.tool()
def mcp_start(name: str) -> dict[str, Any]:
    """Start a managed server, hidden, and wait for its port to answer.

    Reports failure with the tail of the server's log rather than a bare pid: a
    hidden server that died still hands back a pid, which is what makes this
    class of failure so easy to miss.
    """
    try:
        return _ok(**supervise.start(_require(name)))
    except (LookupError, supervise.SuperviseError) as exc:
        return _err(str(exc))


@server.tool()
def mcp_stop(name: str, force: bool = False) -> dict[str, Any]:
    """Stop a managed server and its child processes.

    Refuses to kill a process the orchestrator did not start.
    """
    try:
        return _ok(**supervise.stop(_require(name), force=force))
    except (LookupError, supervise.SuperviseError) as exc:
        return _err(str(exc))


@server.tool()
def mcp_restart(name: str) -> dict[str, Any]:
    """Stop then start a managed server.

    This restarts the server process only. Sessions already running keep their
    old connection and will not see the new process; use sessions_restart for
    that.
    """
    try:
        return _ok(**supervise.restart(_require(name)))
    except (LookupError, supervise.SuperviseError) as exc:
        return _err(str(exc))


@server.tool()
def mcp_autostart(name: str, on: bool) -> dict[str, Any]:
    """Turn autostart-at-logon on or off for a managed server.

    Does not start or stop anything now; it only changes what happens at the
    next logon.
    """
    try:
        spec = _require(name)
    except LookupError as exc:
        return _err(str(exc))
    spec.autostart = bool(on)
    config.upsert(spec)
    return _ok(name=name, autostart=spec.autostart)


@server.tool()
def mcp_enable(name: str, on: bool) -> dict[str, Any]:
    """Enable or disable a managed server.

    A disabled server is kept in the config but refuses to start, autostart
    included. Use this to park a server without losing how it was configured.
    """
    try:
        spec = _require(name)
    except LookupError as exc:
        return _err(str(exc))
    spec.enabled = bool(on)
    config.upsert(spec)
    stopped = None
    if not spec.enabled:
        try:
            stopped = supervise.stop(spec)["was_running"]
        except supervise.SuperviseError:
            stopped = False
    return _ok(name=name, enabled=spec.enabled, stopped_now=stopped)


@server.tool()
def mcp_logs(name: str, tail: int = 80) -> dict[str, Any]:
    """Read the tail of a managed server's log.

    This is where a hidden server's stdout and stderr go, so it is the first
    place to look when a server is crashed or unhealthy.
    """
    try:
        _require(name)
    except LookupError as exc:
        return _err(str(exc))
    return _ok(**supervise.read_log(name, tail))


@server.tool()
def mcp_add(
    name: str,
    command: str,
    port: int | None = None,
    cwd: str | None = None,
    path: str = "/mcp",
    autostart: bool = True,
    register: bool = True,
    start: bool = True,
    restart_sessions: bool = True,
) -> dict[str, Any]:
    """Put an existing local HTTP MCP server under orchestrator management.

    For a server that already exists on disk. Use mcp_scaffold to create a new
    one from scratch. command is the full command line that starts the server,
    for example "C:/repo/.venv/Scripts/python.exe -m my_server.server".

    With register=true the server is also added to Claude Code's user config,
    and with restart_sessions=true every running session is asked to restart at
    its next natural stop, which is what actually makes the new tools reachable.
    """
    if config.get(name) is not None:
        return _err(f"{name!r} is already managed; remove it first or pick another name")
    try:
        spec = config.spec_from_dict(
            {
                "name": name,
                "command": command,
                "cwd": cwd,
                "port": port or supervise.free_port(taken=config.used_ports()),
                "path": path,
                "autostart": autostart,
                "enabled": True,
            }
        )
    except (config.ConfigError, supervise.SuperviseError) as exc:
        return _err(str(exc))

    config.upsert(spec)
    result: dict[str, Any] = {"server": spec.summary()}
    if start:
        try:
            result["start"] = supervise.start(spec)
        except supervise.SuperviseError as exc:
            result["start"] = {"error": str(exc)}
    if register:
        try:
            result["registration"] = claude_cli.register(name, spec.url or "")
            result.update(_finish_install(f"added MCP server {name!r}", restart_sessions))
        except claude_cli.ClaudeCliError as exc:
            result["registration"] = {"error": str(exc)}
    return _ok(**result)


@server.tool()
def mcp_remove(
    name: str,
    stop: bool = True,
    unregister: bool = True,
    restart_sessions: bool = True,
) -> dict[str, Any]:
    """Stop managing a server: drop it from servers.json and from Claude Code's config.

    Deletes no files. The server's repository and logs are left alone.
    """
    try:
        spec = _require(name)
    except LookupError as exc:
        return _err(str(exc))
    result: dict[str, Any] = {"name": name}
    if stop:
        try:
            result["stopped"] = supervise.stop(spec)["was_running"]
        except supervise.SuperviseError as exc:
            result["stopped"] = f"error: {exc}"
    if unregister:
        result["unregistered"] = claude_cli.unregister(name)
        result.update(_finish_install(f"removed MCP server {name!r}", restart_sessions))
    result["removed_from_config"] = config.remove(name)
    return _ok(**result)


@server.tool()
def mcp_scaffold(
    name: str,
    directory: str | None = None,
    port: int | None = None,
    register: bool = True,
    start: bool = True,
    autostart: bool = True,
    restart_sessions: bool = True,
) -> dict[str, Any]:
    """Create a brand new local HTTP MCP server and put it under management.

    Generates a repo (default ~/Repos/<name>-mcp), builds a venv with the MCP
    SDK, allocates a free port, registers the server with Claude Code and
    starts it. The generated server already has one working tool, so a
    successful call means the whole chain works and the only thing left is
    writing real tools in the file named by "edit" in the result.

    Takes a minute or so, most of it pip. The new tools are invisible to
    sessions already running until they restart.
    """
    try:
        result = scaffold.create(name, directory, port, register, start, autostart)
    except (scaffold.ScaffoldError, supervise.SuperviseError, OSError) as exc:
        return _err(str(exc))
    if register and "error" not in result.get("registration", {}):
        result.update(_finish_install(f"scaffolded MCP server {name!r}", restart_sessions))
    result["next"] = f"Edit {result['edit']} to add tools, then mcp_restart({name!r})."
    return _ok(**result)


@server.tool()
def mcp_stats(name: str | None = None) -> dict[str, Any]:
    """Statistics for the managed servers: uptime, availability, crashes, resources.

    Pass a name for one server, or nothing for all of them plus totals. Sampled
    every 10 seconds by the orchestrator, so availability is measured over the
    time it has been watching, not guessed from the current instant.

    These are the orchestrator's own observations: whether each server is up,
    how long it has stayed up, how often it died, and what it costs in memory
    and CPU. It does not see traffic, so there are no per-tool call counts here
    unless a server reports them itself.
    """
    data = monitor.stats()
    if name is not None:
        for entry in data["servers"]:
            if entry["name"] == name:
                return _ok(**entry)
        return _err(f"no server named {name!r}")
    return _ok(
        **data,
        orchestrator={
            "uptime_seconds": int(time.time() - BOOT_TIME),
            "started": datetime.datetime.fromtimestamp(BOOT_TIME).isoformat(timespec="seconds"),
        },
    )


@server.tool()
def sessions_list() -> dict[str, Any]:
    """List the running Claude Code sessions, with pid and transcript session id.

    Populated by a SessionStart hook, which is the only place both halves are
    visible. "unregistered" lists live claude.exe processes with no entry: those
    started before the hook was installed and cannot be restarted by session id
    until they have started once with it in place.
    """
    sessions = registry.list_sessions()
    unknown = registry.unregistered_sessions()
    return _ok(
        sessions=sessions,
        count=len(sessions),
        unregistered=unknown,
        hook_installed=paths.sessions_file().exists(),
    )


@server.tool()
def sessions_restart_pending(reason: str = "the MCP server set changed") -> dict[str, Any]:
    """Ask every running session to restart at its next natural stop.

    This is how a newly installed server actually reaches the sessions that are
    already running: they attach MCP servers only at startup, so nothing short
    of a restart makes a new server visible. Nothing is killed now. Each session
    notices the marker at the end of its current turn, announces it, and comes
    back resuming the same transcript, so its history and its queue survive.
    """
    marker = restart.request(reason)
    state = restart.status()
    return _ok(
        marker=marker,
        sessions_waiting=state["waiting"],
        count=len(state["waiting"]),
        note="Sessions restart at the end of their next turn, not immediately.",
    )


@server.tool()
def sessions_restart(session_id: str, delay_ms: int = 3000) -> dict[str, Any]:
    """Restart one session right now, resuming the same transcript.

    DESTRUCTIVE and immediate: the replacement opens in a new tab and the old
    process is killed a few seconds later, losing any turn still in flight.
    Prefer sessions_restart_pending, which waits for a natural stop. Use this
    only when the user asks for a specific session to be restarted now.
    """
    try:
        return _ok(**restart.restart_session(session_id, delay_ms=delay_ms))
    except (LookupError, OSError) as exc:
        return _err(str(exc))


@server.tool()
def sessions_restart_status() -> dict[str, Any]:
    """Whether a restart is pending, and which sessions have yet to act on it."""
    return _ok(**restart.status())


@server.tool()
def sessions_restart_clear() -> dict[str, Any]:
    """Cancel a pending restart. Sessions that already restarted stay restarted."""
    return _ok(cleared=restart.clear())


@server.tool()
def orchestrator_info() -> dict[str, Any]:
    """Orchestrator health: version, endpoint, state directory, server counts."""
    specs = config.load()
    states = supervise.status_all(specs)
    return _ok(
        version=__version__,
        url=f"http://{HOST}:{PORT}/mcp",
        python=sys.version.split()[0],
        state_dir=str(paths.home()),
        config_file=str(paths.config_file()),
        logs_dir=str(paths.logs_dir()),
        managed=len(specs),
        running=sum(1 for s in states if s["state"] == "running"),
        unhealthy=[s["name"] for s in states if s["state"] in ("crashed", "unhealthy")],
    )


def start_mcp_thread() -> threading.Thread:
    """Run the MCP endpoint on a daemon thread.

    The tray icon needs the main thread on Windows, and the process should die
    with it rather than linger with no way to reach it.
    """

    def run() -> None:
        server.run(transport="streamable-http", host=HOST, port=PORT)

    thread = threading.Thread(target=run, name="mcp-endpoint", daemon=True)
    thread.start()
    return thread


def boot(autostart: bool = True) -> dict[str, Any]:
    """Adopt whatever survived, start the autostart servers, begin watching."""
    specs = config.load()
    adopted = supervise.reconcile(specs)
    started = supervise.autostart_all(specs) if autostart else []
    # Independent of the tray: headless, nothing else would ever notice a crash.
    monitor.Monitor().start()
    return {"adopted": adopted, "started": started, "managed": len(specs)}


_stdout_is_log = False


def _setup_output() -> Any:
    """Give the orchestrator somewhere to speak.

    It normally runs under pythonw.exe from Task Scheduler, which has no console
    at all: ``sys.stdout`` and ``sys.stderr`` are None, and the first thing that
    logs -- ours or uvicorn's -- dies on ``NoneType.write``. The process stays
    alive with nothing listening, which is indistinguishable from a missing
    tool. So everything goes to a file, and the standard streams are pointed at
    it when they do not exist.
    """
    global _stdout_is_log
    handle = open(paths.logs_dir() / "orchestrator.log", "a", encoding="utf-8", buffering=1)
    if sys.stdout is None:
        sys.stdout = handle
        _stdout_is_log = True
    if sys.stderr is None:
        sys.stderr = handle
    return handle


def log(message: str) -> None:
    """Write to the orchestrator's own log, and to the console if there is one."""
    line = f"{datetime.datetime.now().isoformat(timespec='seconds')} {message}"
    try:
        with open(paths.logs_dir() / "orchestrator.log", "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass
    # Skip the echo when stdout *is* the log file, or every line lands twice.
    if sys.stdout is not None and not _stdout_is_log:
        try:
            print(line, flush=True)
        except (ValueError, OSError):
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description="MCP orchestrator")
    parser.add_argument("--no-tray", action="store_true", help="run without the tray icon")
    parser.add_argument("--no-autostart", action="store_true", help="do not start servers at boot")
    args = parser.parse_args()

    _setup_output()
    try:
        log(f"mcp-orchestrator {__version__} starting on http://{HOST}:{PORT}/mcp")
        log(f"state: {paths.home()}")
        result = boot(autostart=not args.no_autostart)
        for entry in result["started"]:
            log(f"  {entry.get('name')}: {entry.get('state')} {entry.get('error') or ''}")

        start_mcp_thread()
        for _ in range(100):  # the endpoint is the whole point; say whether it came up
            if supervise.port_open(PORT):
                log(f"listening on http://{HOST}:{PORT}/mcp")
                break
            time.sleep(0.1)
        else:
            log(f"WARNING: nothing listening on {PORT} after 10s")

        if not args.no_tray:
            try:
                from orchestrator import tray

                log("tray icon starting")
                tray.run(port=PORT)
                log("tray closed; shutting down")
                return
            except ImportError as exc:  # pystray missing: better headless than dead
                log(f"tray unavailable ({exc}); running headless")

        threading.Event().wait()
    except Exception:  # noqa: BLE001 -- a crash with no console leaves no other trace
        log("FATAL: " + traceback.format_exc())
        raise


if __name__ == "__main__":
    main()
