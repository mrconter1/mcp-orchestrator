"""Restarting Claude Code sessions so they pick up a changed MCP server set.

Claude Code attaches MCP servers at process startup and never retries. A server
can therefore be started, healthy and registered, and still be invisible to
every session that was already running. Restarting them is not a nicety; it is
the last step of any install, and without it the install is not finished.

The choreography is decentralised on purpose. Nothing kills a session from
outside at an arbitrary moment. A change drops a marker; each session notices it
at its own Stop hook, which fires when a turn has just ended and nothing is in
flight, and restarts itself from there.

The replacement resumes the *same* session id, so the transcript and the
per-session queue both survive. ``--resume <id>`` without ``--fork-session``
reuses the id rather than minting a new one.
"""

from __future__ import annotations

import datetime
import os
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any

from orchestrator import paths, registry

DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
NEW_CONSOLE = 0x00000010 | 0x00000200  # CREATE_NEW_CONSOLE | CREATE_NEW_PROCESS_GROUP

# Claude Code exports these inside a live session. The Stop hook runs *in* a
# session, so without scrubbing they reach the replacement, which then treats
# itself as a child session and turns transcript saving off -- leaving the very
# session we were trying to preserve unresumable.
SESSION_ENV_MARKERS = (
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_BRIDGE_SESSION_ID",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_PID",
    "CLAUDECODE",
)

DEFAULT_DELAY_MS = 3000


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _clean_env() -> dict[str, str]:
    env = os.environ.copy()
    for key in SESSION_ENV_MARKERS:
        env.pop(key, None)
    return env


# --- the marker ----------------------------------------------------------


def request(reason: str = "the MCP server set changed") -> dict[str, Any]:
    """Ask every session to restart at its next natural stop.

    The marker carries a unique id rather than being identified by its
    timestamp. Two changes inside the same second -- an add immediately
    followed by a remove, say -- would otherwise be indistinguishable, and a
    session that had consumed the first would skip the second and quietly keep
    a stale server list.
    """
    marker = {"id": uuid.uuid4().hex, "at": _now(), "reason": reason}
    paths.write_json(paths.pending_restart_file(), marker)
    return marker


def _marker_key(marker: dict[str, Any]) -> str:
    """Identity of a marker, tolerating one written without an id."""
    return str(marker.get("id") or marker.get("at") or "")


def pending() -> dict[str, Any] | None:
    """The current marker, if any. Kept to a single file read: the Stop hook
    runs at the end of every turn and must stay cheap."""
    if not paths.pending_restart_file().exists():
        return None
    marker = paths.read_json(paths.pending_restart_file(), None)
    return marker if isinstance(marker, dict) else None


def clear() -> bool:
    """Drop the marker and everything that consumed it."""
    existed = paths.pending_restart_file().exists()
    paths.pending_restart_file().unlink(missing_ok=True)
    paths.restart_state_file().unlink(missing_ok=True)
    return existed


def _consumed() -> dict[str, str]:
    data = paths.read_json(paths.restart_state_file(), {})
    return data if isinstance(data, dict) else {}


def has_consumed(session_id: str, marker: dict[str, Any]) -> bool:
    return _consumed().get(session_id) == _marker_key(marker)


def mark_consumed(session_id: str, marker: dict[str, Any]) -> None:
    """Record that this session has handled this marker.

    Per session, keyed on the marker's timestamp. A single global "handled" flag
    would let the first session to restart cancel it for everybody else; no key
    at all and every session would restart forever, each restart re-reading a
    marker it has already acted on.
    """
    data = _consumed()
    data[session_id] = _marker_key(marker)
    paths.write_json(paths.restart_state_file(), data)


def status() -> dict[str, Any]:
    marker = pending()
    sessions = registry.list_sessions()
    consumed = _consumed()
    return {
        "pending": marker,
        "sessions": [
            {
                "session_id": s["session_id"],
                "pid": s["pid"],
                "cwd": s.get("cwd"),
                "restarted": bool(marker) and consumed.get(s["session_id"]) == _marker_key(marker),
            }
            for s in sessions
        ],
        "waiting": [
            s["session_id"]
            for s in sessions
            if marker and consumed.get(s["session_id"]) != _marker_key(marker)
        ],
    }


# --- doing the restart ---------------------------------------------------


def _find_wt() -> str | None:
    """Locate wt.exe.

    ``shutil.which`` alone is not enough: Windows Terminal ships as an App
    Execution Alias under %LOCALAPPDATA%\\Microsoft\\WindowsApps, which is
    missing from PATH in some environments -- including a hook's.
    """
    found = shutil.which("wt.exe") or shutil.which("wt")
    if found:
        return found
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidate = Path(local) / "Microsoft" / "WindowsApps" / "wt.exe"
        if candidate.exists():
            return str(candidate)
    return None


def spawn_replacement(session_id: str, cwd: str | None = None) -> dict[str, Any]:
    """Open a replacement session resuming ``session_id``."""
    working_dir = Path(cwd).expanduser() if cwd else Path.home()
    if not working_dir.is_dir():
        working_dir = Path.home()

    inner = f"claude --resume {session_id}"
    wt = _find_wt()
    if wt:
        # -w 0 targets the current Windows Terminal window, so the replacement
        # arrives as a tab. The directory comes from -d rather than from a
        # "Set-Location ...;" prefix, because wt reads ';' as its own command
        # separator and would split the command in half.
        argv = [wt, "-w", "0", "new-tab", "--title", "claude", "-d", str(working_dir),
                "powershell", "-NoExit", "-Command", inner]
        flags = DETACHED
    else:
        argv = ["powershell.exe", "-NoExit", "-Command", inner]
        flags = NEW_CONSOLE

    proc = subprocess.Popen(  # noqa: S603 -- argv is built from a validated session id
        argv,
        cwd=str(working_dir),
        env=_clean_env(),
        creationflags=flags,
        close_fds=True,
    )
    return {"launcher_pid": proc.pid, "cwd": str(working_dir), "command": inner}


def schedule_kill(pid: int, delay_ms: int = DEFAULT_DELAY_MS) -> int:
    """Kill ``pid`` after a delay, from a process that outlives this one.

    A session cannot terminate itself and still finish the turn that asked for
    it, so the kill is handed off and delayed.

    The hosting shell is left alone on purpose. Terminating it gives it a
    non-zero exit code, and Windows Terminal's default ``closeOnExit=graceful``
    keeps a tab open on "[process exited with code ...]", which is worse than
    the prompt it would otherwise show. Tabs close by exiting 0, which is what
    :func:`_exit_zero` arranges for the tabs this code opens.
    """
    script = f"Start-Sleep -Milliseconds {int(delay_ms)}; Stop-Process -Id {int(pid)} -Force"
    helper = subprocess.Popen(  # noqa: S603
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        creationflags=DETACHED,
        close_fds=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return helper.pid


def _ps_quote(value: str) -> str:
    """Single-quote a value for PowerShell, doubling any quotes inside it."""
    return "'" + str(value).replace("'", "''") + "'"


def _exit_zero(inner: str) -> str:
    """Wrap a command so its shell exits 0 however the command ends.

    This is what closes the tab. Windows Terminal's default ``closeOnExit`` is
    ``graceful``, meaning it closes a tab only on exit code 0, and a session
    that was killed exits non-zero. Without this the replacement tab would
    outlive its own session and sit on "[process exited with code ...]".

    ``try{...}finally{...}`` rather than ``...; exit 0`` because a command
    handed to wt must not contain a semicolon; wt reads it as its own separator.
    """
    return f"try{{{inner}}}finally{{exit 0}}"


def restart_session(
    session_id: str,
    pid: int | None = None,
    cwd: str | None = None,
    delay_ms: int = DEFAULT_DELAY_MS,
) -> dict[str, Any]:
    """Replace one session with a fresh one resuming the same transcript.

    The whole sequence runs in one detached helper, in this order: kill the old
    session, pause, then open the replacement.

    Killing first is the fix for a real failure. Spawning first left both
    sessions alive at once, and because a resumed session keeps its id, two
    workers claimed one session id: Remote Control evicted one and ``/rc``
    failed in the new tab with code 4090.

    The cost is a window where neither session exists. If the spawn then fails,
    the transcript is still on disk and ``claude --resume <id>`` brings it back,
    which is why this is the better trade.

    Whether the old tab disappears depends on how it was started. A tab opened
    by this code exits 0 and closes itself; one started by hand returns to its
    shell prompt and stays, and there is no way to close it from outside that
    does not leave a worse artefact behind.
    """
    entry = registry.get(session_id) or {}
    pid = pid or entry.get("pid")
    cwd = cwd or entry.get("cwd")
    if not pid:
        raise LookupError(
            f"no pid known for session {session_id}; it is not in the registry, so the "
            f"old process cannot be replaced. Start it once with the SessionStart hook "
            f"installed, or restart it by hand"
        )

    working_dir = Path(cwd).expanduser() if cwd else Path.home()
    if not working_dir.is_dir():
        working_dir = Path.home()

    steps = [
        f"Start-Sleep -Milliseconds {int(delay_ms)}",
        f"Stop-Process -Id {int(pid)} -Force -ErrorAction SilentlyContinue",
        # Let the old worker's socket actually close before the replacement
        # claims the same session id, or the eviction happens the other way.
        "Start-Sleep -Milliseconds 1200",
    ]

    inner = _exit_zero(f"claude --resume {session_id}")
    wt = _find_wt()
    if wt:
        steps.append(
            f"& {_ps_quote(wt)} -w 0 new-tab --title claude -d {_ps_quote(str(working_dir))} "
            f"powershell -Command {_ps_quote(inner)}"
        )
    else:
        steps.append(
            f"Start-Process powershell.exe -WorkingDirectory {_ps_quote(str(working_dir))} "
            f"-ArgumentList '-Command',{_ps_quote(inner)}"
        )

    helper = subprocess.Popen(  # noqa: S603 -- every part is quoted above
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", "; ".join(steps)],
        cwd=str(working_dir),
        env=_clean_env(),
        creationflags=DETACHED,
        close_fds=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return {
        "session_id": session_id,
        "helper_pid": helper.pid,
        "killing_pid": pid,
        "cwd": str(working_dir),
        "delay_ms": delay_ms,
        "order": "kill the old session, pause, then open the replacement",
    }
