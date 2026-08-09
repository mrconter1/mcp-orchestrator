"""SessionStart hook: register this session with the orchestrator.

Wired into Claude Code's settings as::

    "SessionStart": [{"hooks": [{"type": "command", "command":
      "\\"<repo>\\.venv\\Scripts\\python.exe\\" \\"<repo>\\orchestrator\\hooks\\session_start.py\\""}]}]

Reads the hook payload on stdin and falls back to the environment, because the
two carry the same facts and neither is guaranteed across Claude Code versions.
Exits 0 no matter what: a session must never fail to start because a bookkeeping
file could not be written.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Run as a plain script from Claude Code's settings, so the package this lives
# in is not importable until the repo root is on the path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))


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
    pid = os.environ.get("CLAUDE_PID")
    if not session_id or not pid:
        return 0  # nothing useful to record; stay silent rather than complain

    try:
        from orchestrator import registry

        registry.record(
            session_id=session_id,
            pid=int(pid),
            cwd=payload.get("cwd") or os.getcwd(),
            transcript=payload.get("transcript_path"),
            source=payload.get("source"),
        )
        registry.prune()
    except Exception:  # noqa: BLE001 -- a hook that raises is worse than one that does nothing
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
