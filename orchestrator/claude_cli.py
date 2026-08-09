"""Talking to the ``claude`` CLI to register and unregister MCP servers.

Registration is what makes a managed server visible to Claude Code at all;
starting it is only half the job. Note the ordering constraint from the design:
Claude Code attaches MCP servers at process startup and never retries, so a
registration only takes effect in sessions started afterwards. Everything here
therefore pairs with the restart choreography.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

DEFAULT_SCOPE = "user"  # machine-wide, not tied to whichever directory ran the tool
TIMEOUT = 60


class ClaudeCliError(Exception):
    """Raised when the claude CLI is missing or returns a failure."""


def find_claude() -> str:
    """Locate the claude CLI.

    ``shutil.which`` is not sufficient on its own: the orchestrator is started
    by Task Scheduler, whose PATH is the logon-time one and can be missing the
    nvm shim directory that a normal terminal has. The known install locations
    are checked as a fallback before giving up.
    """
    found = shutil.which("claude")
    if found:
        return found
    candidates = [
        Path.home() / ".local" / "bin" / "claude.exe",
        Path(os.environ.get("APPDATA", "")) / "npm" / "claude.cmd",
        Path("C:/nvm4w/nodejs/claude.cmd"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    raise ClaudeCliError("claude CLI not found on PATH or in any known install location")


def _run(args: list[str]) -> subprocess.CompletedProcess:
    claude = find_claude()
    try:
        return subprocess.run(  # noqa: S603 -- argv is built here, never from a shell string
            [claude, *args],
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ClaudeCliError(f"claude {' '.join(args)} timed out after {TIMEOUT}s") from exc
    except OSError as exc:
        raise ClaudeCliError(f"could not run claude: {exc}") from exc


def register(name: str, url: str, scope: str = DEFAULT_SCOPE) -> dict[str, Any]:
    """Add an HTTP MCP server to Claude Code's config.

    Re-registering an existing name fails in the CLI, so an existing entry is
    removed first: this has to be idempotent, or re-scaffolding after a mistake
    leaves the user editing config by hand.
    """
    unregister(name, scope)
    result = _run(["mcp", "add", "--transport", "http", "--scope", scope, name, url])
    if result.returncode != 0:
        raise ClaudeCliError(
            f"claude mcp add failed ({result.returncode}): "
            f"{(result.stderr or result.stdout or '').strip()}"
        )
    return {"registered": name, "url": url, "scope": scope, "output": (result.stdout or "").strip()}


def unregister(name: str, scope: str = DEFAULT_SCOPE) -> bool:
    """Remove a server from Claude Code's config. False if it was not there."""
    result = _run(["mcp", "remove", "--scope", scope, name])
    return result.returncode == 0


def is_registered(name: str) -> bool:
    result = _run(["mcp", "get", name])
    return result.returncode == 0
