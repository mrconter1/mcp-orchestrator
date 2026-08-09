"""Stop hook: restart this session if the MCP server set changed under it.

Wired into Claude Code's settings as::

    "Stop": [{"hooks": [{"type": "command", "command":
      "\\"<repo>\\.venv\\Scripts\\python.exe\\" \\"<repo>\\orchestrator\\hooks\\stop.py\\""}]}]

This runs at the end of *every* turn, so the common path has to be nearly free:
one ``exists()`` call on a file that is usually absent, then exit. Everything
heavier happens only when a marker is actually there.

When it does fire, the marker is consumed *before* the replacement is spawned.
Consuming afterwards leaves a window where a failed spawn means the session
tries again on every subsequent turn, forever.
"""

from __future__ import annotations

import datetime
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))


def _log_failure(detail: str) -> None:
    """Leave a trace when the hook gives up.

    Swallowing the error is right -- a turn must not fail over this -- but
    swallowing it *silently* means a restart that never happens looks like a
    restart that was never requested.
    """
    try:
        from orchestrator import paths

        with open(paths.logs_dir() / "hooks.log", "a", encoding="utf-8") as handle:
            handle.write(f"{datetime.datetime.now().isoformat(timespec='seconds')} stop: {detail}\n")
    except Exception:  # noqa: BLE001 -- the logger of last resort cannot itself throw
        pass


def _emit(message: str) -> None:
    """Tell the user what is about to happen.

    A window closing and another opening, with no explanation, is indisting-
    uishable from a crash.
    """
    print(json.dumps({"systemMessage": message}))


def main() -> int:
    try:
        raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    except (OSError, ValueError):
        raw = ""
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        payload = {}

    session_id = payload.get("session_id") or os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not session_id:
        return 0

    try:
        from orchestrator import paths

        if not paths.pending_restart_file().exists():
            return 0  # the overwhelmingly common case, and it costs one stat

        from orchestrator import restart

        marker = restart.pending()
        if not marker or restart.has_consumed(session_id, marker):
            return 0

        pid = os.environ.get("CLAUDE_PID")
        cwd = payload.get("cwd") or os.getcwd()

        # Claim it first: a spawn that fails must not leave this session
        # retrying on every turn from here on.
        restart.mark_consumed(session_id, marker)
        restart.restart_session(session_id, pid=int(pid) if pid else None, cwd=cwd)
        _emit(
            f"Detecting pending mcp restart ({marker.get('reason')}) - restarting this "
            f"session in a new tab, resuming the same transcript."
        )
    except Exception as exc:  # noqa: BLE001 -- never fail a turn over bookkeeping
        _log_failure(f"{type(exc).__name__}: {exc}")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
