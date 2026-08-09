"""HTTP MCP server that manages the user's other local MCP servers.

    .venv\\Scripts\\python.exe -m orchestrator.server

Binds 127.0.0.1 only. It runs hidden from Task Scheduler at logon, starts every
server marked autostart, and stays up as the one thing the OS has to supervise.

The MCP endpoint runs on a background thread so the main thread stays free for
the tray icon, which on Windows has to own it.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
from typing import Any

from mcp.server.mcpserver import MCPServer

from orchestrator import __version__, claude_cli, config, paths, supervise
from orchestrator.config import ServerSpec

HOST = os.environ.get("MCP_ORCHESTRATOR_HOST", "127.0.0.1")
PORT = int(os.environ.get("MCP_ORCHESTRATOR_PORT", "8768"))

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
) -> dict[str, Any]:
    """Put an existing local HTTP MCP server under orchestrator management.

    For a server that already exists on disk. Use mcp_scaffold to create a new
    one from scratch. command is the full command line that starts the server,
    for example "C:/repo/.venv/Scripts/python.exe -m my_server.server".

    With register=true the server is also added to Claude Code's user config,
    which only takes effect in sessions started afterwards.
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
        except claude_cli.ClaudeCliError as exc:
            result["registration"] = {"error": str(exc)}
    result["note"] = (
        "Registered with Claude Code, but running sessions attach MCP servers only at "
        "startup. Call sessions_restart_pending to have every session pick it up at its "
        "next natural stop."
    )
    return _ok(**result)


@server.tool()
def mcp_remove(name: str, stop: bool = True, unregister: bool = True) -> dict[str, Any]:
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
    result["removed_from_config"] = config.remove(name)
    return _ok(**result)


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
    """Adopt whatever survived, then start the servers marked autostart."""
    specs = config.load()
    adopted = supervise.reconcile(specs)
    started = supervise.autostart_all(specs) if autostart else []
    return {"adopted": adopted, "started": started, "managed": len(specs)}


def main() -> None:
    parser = argparse.ArgumentParser(description="MCP orchestrator")
    parser.add_argument("--no-tray", action="store_true", help="run without the tray icon")
    parser.add_argument("--no-autostart", action="store_true", help="do not start servers at boot")
    args = parser.parse_args()

    print(f"mcp-orchestrator {__version__} listening on http://{HOST}:{PORT}/mcp", flush=True)
    print(f"state: {paths.home()}", flush=True)
    result = boot(autostart=not args.no_autostart)
    for entry in result["started"]:
        print(f"  {entry.get('name')}: {entry.get('state')} {entry.get('error') or ''}", flush=True)

    start_mcp_thread()

    if not args.no_tray:
        try:
            from orchestrator import tray

            tray.run(shutdown=lambda: None)
            return
        except ImportError as exc:  # pystray missing: better headless than dead
            print(f"tray unavailable ({exc}); running headless", flush=True)

    threading.Event().wait()


if __name__ == "__main__":
    main()
