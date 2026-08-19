"""Where everything lives, and how it is written.

State lives outside the repo, under ``%USERPROFILE%\\.mcp-orchestrator``. Two
reasons: the server list is machine-specific config rather than code, and the
hooks -- which run from Claude Code's settings, not from this checkout -- need a
location they can compute without knowing where the repo was cloned.

Override the root with ``MCP_ORCHESTRATOR_HOME`` (the hooks read the same
variable, so an override has to be set for the whole user, not just the server).
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

ENV_HOME = "MCP_ORCHESTRATOR_HOME"


def home() -> Path:
    """Root of the orchestrator's state, created if missing."""
    override = os.environ.get(ENV_HOME)
    root = Path(override).expanduser() if override else Path.home() / ".mcp-orchestrator"
    root.mkdir(parents=True, exist_ok=True)
    return root


def config_file() -> Path:
    """servers.json -- the configured server set."""
    return home() / "servers.json"


def running_file() -> Path:
    """Pids of servers this orchestrator started, so a restarted orchestrator re-adopts them."""
    return home() / "running.json"


def sessions_file() -> Path:
    """Session registry, written by the SessionStart hook."""
    return home() / "sessions.json"


def pending_restart_file() -> Path:
    """Marker saying every session should restart at its next natural stop."""
    return home() / "pending-restart.json"


def restart_state_file() -> Path:
    """Which sessions have already consumed which marker.

    Keyed per session on purpose. One global "handled" flag would let the first
    session to restart cancel it for everybody else; no key at all would restart
    every session forever.
    """
    return home() / "restart-consumed.json"


def logs_dir() -> Path:
    root = home() / "logs"
    root.mkdir(parents=True, exist_ok=True)
    return root


def log_file(name: str) -> Path:
    return logs_dir() / f"{name}.log"


def templates_dir() -> Path:
    """Scaffold templates, which ship with the code rather than with the state."""
    return Path(__file__).resolve().parent / "templates"


def hooks_dir() -> Path:
    """Hook scripts, referenced by absolute path from Claude Code's settings."""
    return Path(__file__).resolve().parent / "hooks"


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def read_json(path: Path, default: Any) -> Any:
    """Read JSON, tolerating absence and a torn write.

    A corrupt state file must not take the orchestrator down with it: losing the
    running-pid map costs an adoption, losing the config costs an error message,
    but a crash on startup costs every server at once.
    """
    try:
        # utf-8-sig, not utf-8: these files are meant to be hand-editable, and a
        # Windows editor (or Set-Content -Encoding utf8) writes a BOM. Plain
        # utf-8 turns that into a JSONDecodeError, which lands in the except
        # below and silently returns the default -- losing every recorded pid
        # and leaving the whole server set unsupervised.
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return default
    except OSError:
        return default
    if not text.strip():
        return default
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return default


def write_json(path: Path, data: Any) -> None:
    """Write JSON atomically.

    Hooks and the server write the same files from different processes, so a
    reader must never see a half-written file. Write to a temp file in the same
    directory and ``os.replace`` it, which is atomic on Windows too.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        # Windows can still hold a brief lock from an antivirus or indexer scan
        # of the file we just closed; a couple of retries beats failing the call.
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.05)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
