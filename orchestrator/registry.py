"""Which Claude Code sessions are running, and what their transcript ids are.

Pid to session-id cannot be worked out from outside. ``claude.exe`` does not
carry ``CLAUDE_CODE_SESSION_ID`` in its own environment -- it *sets* that for
the processes it spawns, so a shell inside a session sees both halves and the
parent does not. The only place both are visible is inside the session, which
is why this file is written by a SessionStart hook rather than discovered here.

Entries are keyed by session id, not pid. A restarted session comes back with
the same transcript id and a different pid, and that has to read as the same
session rather than as a second one.
"""

from __future__ import annotations

import datetime
from typing import Any

import psutil

from orchestrator import paths

CLAUDE_EXE = "claude.exe"


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _load() -> dict[str, dict[str, Any]]:
    data = paths.read_json(paths.sessions_file(), {})
    return data if isinstance(data, dict) else {}


def _is_live_claude(entry: dict[str, Any]) -> bool:
    """Is this entry's process still the same live claude.exe?

    The create-time check is what stops a reused pid from resurrecting a dead
    session, which would then be handed a restart it never asked for.
    """
    pid = entry.get("pid")
    if not pid:
        return False
    try:
        proc = psutil.Process(int(pid))
        if proc.name().lower() != CLAUDE_EXE:
            return False
        recorded = entry.get("create_time")
        if recorded is not None and abs(proc.create_time() - float(recorded)) > 1.0:
            return False
    except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError, TypeError):
        return False
    return True


def record(
    session_id: str,
    pid: int,
    cwd: str | None = None,
    transcript: str | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    """Register a session. Called from the SessionStart hook, inside the session."""
    data = _load()
    try:
        create_time = psutil.Process(int(pid)).create_time()
    except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError, TypeError):
        create_time = None

    # One pid holds one session at a time. After /clear the id changes while the
    # process stays put, and without this the old id would linger as a live
    # session forever.
    for other_id in [k for k, v in data.items() if v.get("pid") == pid and k != session_id]:
        del data[other_id]

    entry = {
        "session_id": session_id,
        "pid": int(pid),
        "create_time": create_time,
        "cwd": cwd,
        "transcript": transcript,
        "source": source,
        "started": _now(),
    }
    data[session_id] = entry
    paths.write_json(paths.sessions_file(), data)
    return entry


def forget(session_id: str) -> bool:
    data = _load()
    if session_id not in data:
        return False
    del data[session_id]
    paths.write_json(paths.sessions_file(), data)
    return True


def prune() -> list[str]:
    """Drop sessions whose process is gone. Returns the ids removed."""
    data = _load()
    dead = [sid for sid, entry in data.items() if not _is_live_claude(entry)]
    if dead:
        for sid in dead:
            del data[sid]
        paths.write_json(paths.sessions_file(), data)
    return dead


def list_sessions(prune_first: bool = True) -> list[dict[str, Any]]:
    """Live sessions, newest first."""
    if prune_first:
        prune()
    out = []
    for entry in _load().values():
        info = dict(entry)
        try:
            proc = psutil.Process(int(entry["pid"]))
            info["uptime_seconds"] = int(
                datetime.datetime.now().timestamp() - proc.create_time()
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError, TypeError, KeyError):
            info["uptime_seconds"] = None
        info.pop("create_time", None)
        out.append(info)
    return sorted(out, key=lambda s: s.get("started") or "", reverse=True)


def get(session_id: str) -> dict[str, Any] | None:
    return _load().get(session_id)


def unregistered_sessions() -> list[dict[str, Any]]:
    """Live claude.exe processes with no registry entry.

    These are sessions that started before the hook was installed. They are
    invisible to the restart choreography, so the honest thing is to report
    them rather than to let the count quietly disagree with reality.
    """
    known = {entry.get("pid") for entry in _load().values()}
    out = []
    for proc in psutil.process_iter(["name", "pid"]):
        try:
            if (proc.info["name"] or "").lower() != CLAUDE_EXE:
                continue
            if proc.info["pid"] in known:
                continue
            out.append(
                {
                    "pid": proc.info["pid"],
                    "cwd": proc.cwd(),
                    "started": datetime.datetime.fromtimestamp(
                        proc.create_time()
                    ).isoformat(timespec="seconds"),
                }
            )
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue
    return out
