"""Wiring the hooks into Claude Code's settings.

The orchestrator needs two hooks to do its job: SessionStart to learn which
sessions exist, and Stop to let each one restart itself when the server set
changes. Both are edits to the user's global ``settings.json``, so this is
written to be idempotent, to back the file up first, and to be undoable.

    .venv\\Scripts\\python.exe -m orchestrator.install install
    .venv\\Scripts\\python.exe -m orchestrator.install uninstall
    .venv\\Scripts\\python.exe -m orchestrator.install status
"""

from __future__ import annotations

import argparse
import datetime
import json
import shutil
import sys
from pathlib import Path
from typing import Any

from orchestrator import paths

SETTINGS = Path.home() / ".claude" / "settings.json"
HOOK_TIMEOUT = 15  # seconds; a wedged hook must not stall every turn forever

# (settings key, hook script filename)
HOOKS = (
    ("SessionStart", "session_start.py"),
    ("Stop", "stop.py"),
)


def python_exe() -> Path:
    """The interpreter the hooks run under.

    python.exe rather than pythonw.exe: the Stop hook talks to Claude Code over
    stdout, and it is spawned with pipes from a process that already owns a
    console, so there is no window to flash.
    """
    return paths.repo_root() / ".venv" / "Scripts" / "python.exe"


def hook_command(script: str) -> str:
    return f'"{python_exe()}" "{paths.hooks_dir() / script}"'


def _is_ours(command: str) -> bool:
    """Does this hook entry point at our scripts?

    Matching on the hooks directory rather than the exact string, so a command
    that was hand-edited or moved is still recognised as ours and replaced
    instead of being left behind as a duplicate.
    """
    return str(paths.hooks_dir()).lower() in command.lower()


def _load_settings(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{path} is not valid JSON ({exc}); refusing to touch it")
    return data if isinstance(data, dict) else {}


def _backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backups = paths.home() / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    target = backups / f"settings.json.{stamp}"
    shutil.copy2(path, target)
    return target


def _strip_ours(entries: list[Any]) -> list[Any]:
    """Remove our hook entries from one event's list, leaving anyone else's alone."""
    kept = []
    for entry in entries:
        if not isinstance(entry, dict):
            kept.append(entry)
            continue
        inner = [
            hook
            for hook in entry.get("hooks", [])
            if not (isinstance(hook, dict) and _is_ours(str(hook.get("command", ""))))
        ]
        if inner:
            kept.append({**entry, "hooks": inner})
        elif not entry.get("hooks"):
            kept.append(entry)  # someone else's empty entry; not ours to delete
    return kept


def install(settings_path: Path = SETTINGS) -> dict[str, Any]:
    settings = _load_settings(settings_path)
    backup = _backup(settings_path)
    hooks = settings.setdefault("hooks", {})

    for event, script in HOOKS:
        existing = _strip_ours(hooks.get(event, []))
        existing.append(
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": hook_command(script),
                        "timeout": HOOK_TIMEOUT,
                    }
                ]
            }
        )
        hooks[event] = existing

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        json.dumps(settings, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return {
        "settings": str(settings_path),
        "backup": str(backup) if backup else None,
        "installed": [event for event, _ in HOOKS],
        "note": "Hooks apply to sessions started from now on, not to this one.",
    }


def uninstall(settings_path: Path = SETTINGS) -> dict[str, Any]:
    settings = _load_settings(settings_path)
    backup = _backup(settings_path)
    hooks = settings.get("hooks", {})
    removed = []
    for event, _ in HOOKS:
        if event not in hooks:
            continue
        before = json.dumps(hooks[event])
        hooks[event] = _strip_ours(hooks[event])
        if not hooks[event]:
            del hooks[event]
        if json.dumps(hooks.get(event, [])) != before:
            removed.append(event)
    if not hooks:
        settings.pop("hooks", None)
    settings_path.write_text(
        json.dumps(settings, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return {"settings": str(settings_path), "backup": str(backup) if backup else None, "removed": removed}


def status(settings_path: Path = SETTINGS) -> dict[str, Any]:
    settings = _load_settings(settings_path)
    hooks = settings.get("hooks", {})
    out: dict[str, Any] = {"settings": str(settings_path), "hooks": {}}
    for event, script in HOOKS:
        found = [
            hook.get("command")
            for entry in hooks.get(event, [])
            if isinstance(entry, dict)
            for hook in entry.get("hooks", [])
            if isinstance(hook, dict) and _is_ours(str(hook.get("command", "")))
        ]
        out["hooks"][event] = found or None
    out["installed"] = all(out["hooks"].values())
    out["python"] = str(python_exe())
    out["python_exists"] = python_exe().exists()
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Install the orchestrator's Claude Code hooks")
    parser.add_argument("action", choices=["install", "uninstall", "status"])
    parser.add_argument("--settings", default=str(SETTINGS), help="path to settings.json")
    args = parser.parse_args()
    result = {"install": install, "uninstall": uninstall, "status": status}[args.action](
        Path(args.settings)
    )
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
