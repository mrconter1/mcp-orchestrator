"""Installing an MCP server straight from a git repository.

``mcp_scaffold`` makes a new server from a template. This makes a server that
already exists somewhere: clone, build a venv, register, start. The catalogue
below is a shortcut for servers known to work with the orchestrator, but any
repository laid out the same way installs by URL.

The layout assumed is the one the scaffolder produces, which is also the
ordinary Python one: a package directory containing ``server.py``, and a
``requirements.txt`` beside it.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from orchestrator import claude_cli, config, paths, scaffold, supervise

CLONE_TIMEOUT = 300

# Servers known to work with the orchestrator. Ports are preferences, not
# reservations: a taken port is reallocated rather than refused.
CATALOGUE: list[dict[str, Any]] = [
    {
        "name": "session",
        "repo": "https://github.com/mrconter1/session-control-mcp",
        "module": "session_control.server",
        "port": 8767,
        "port_env": "SESSION_CONTROL_PORT",
        "description": (
            "Control Claude Code sessions: list, open, fork off a transcript, "
            "close, restart. Destructive tools, meant to prompt on every call."
        ),
    },
    {
        "name": "queue",
        "repo": "https://github.com/mrconter1/queue-mcp",
        "module": "queue_mcp.server",
        "port": 8766,
        "port_env": "QUEUE_MCP_PORT",
        "description": (
            "A per-session task queue. Add work from another terminal without "
            "steering the running turn; Claude drains it one item at a time."
        ),
    },
]


class InstallError(Exception):
    """Raised when a server cannot be installed as asked."""


def find(name: str) -> dict[str, Any] | None:
    for entry in CATALOGUE:
        if entry["name"] == name:
            return entry
    return None


def _repo_name(url: str) -> str:
    return re.sub(r"\.git$", "", url.rstrip("/").replace("\\", "/").rsplit("/", 1)[-1])


def _is_repo_source(value: str) -> bool:
    """Is this something git can clone?

    Remote URLs plus local paths. Cloning from a directory is how you install a
    private repo you already have, or anything at all with no network.
    """
    if "://" in value or value.startswith("git@"):
        return True
    try:
        return Path(value).expanduser().is_dir()
    except OSError:
        return False


def _detect_module(target: Path) -> str:
    """Find the package holding ``server.py``.

    Only needed for repositories not in the catalogue. A repo with several
    candidates is ambiguous, and guessing there would install something the
    caller did not ask for, so it asks instead.
    """
    candidates = [
        path.parent.name
        for path in target.glob("*/server.py")
        if (path.parent / "__init__.py").exists()
    ]
    if not candidates:
        raise InstallError(
            f"no package with a server.py found in {target}; pass module explicitly, "
            f"for example 'my_server.server'"
        )
    if len(candidates) > 1:
        raise InstallError(
            f"several packages look like servers in {target} ({', '.join(candidates)}); "
            f"pass module explicitly"
        )
    return f"{candidates[0]}.server"


def _clone(url: str, target: Path) -> dict[str, Any]:
    if target.exists() and any(target.iterdir()):
        # Already on disk. Reuse it only if it is the same repository, so a
        # name clash with unrelated code fails loudly instead of half-working.
        origin = subprocess.run(  # noqa: S603
            ["git", "-C", str(target), "remote", "get-url", "origin"],
            capture_output=True, text=True, check=False,
        )
        existing = (origin.stdout or "").strip()
        if origin.returncode == 0 and _repo_name(existing) == _repo_name(url):
            return {"cloned": False, "reused": str(target), "origin": existing}
        raise InstallError(
            f"{target} already exists and is not a clone of {url}; "
            f"move it or pass another directory"
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(  # noqa: S603
        ["git", "clone", "--depth", "1", url, str(target)],
        capture_output=True, text=True, timeout=CLONE_TIMEOUT, check=False,
    )
    if result.returncode != 0:
        raise InstallError(f"git clone failed: {(result.stderr or result.stdout).strip()}")
    return {"cloned": True, "directory": str(target)}


def install(
    name_or_url: str,
    name: str | None = None,
    module: str | None = None,
    directory: str | None = None,
    port: int | None = None,
    port_env: str | None = None,
    register: bool = True,
    start: bool = True,
    autostart: bool = True,
) -> dict[str, Any]:
    """Clone, build and register an MCP server from a git repository."""
    entry = find(name_or_url)
    if entry:
        url = entry["repo"]
        name = name or entry["name"]
        module = module or entry["module"]
        port = port or entry["port"]
        port_env = port_env or entry.get("port_env")
    elif _is_repo_source(name_or_url):
        url = name_or_url
        name = name or _repo_name(url).replace("-mcp", "").replace("_", "-")
    else:
        known = ", ".join(e["name"] for e in CATALOGUE)
        raise InstallError(
            f"{name_or_url!r} is not in the catalogue and is not a repository. "
            f"Known: {known}"
        )

    name = scaffold.validate_name(name)
    if config.get(name) is not None:
        raise InstallError(
            f"a server named {name!r} is already managed; remove it first or pass "
            f"another name"
        )

    target = Path(directory).expanduser() if directory else scaffold.DEFAULT_PARENT / _repo_name(url)
    steps: dict[str, Any] = {"repo": url, "clone": _clone(url, target)}

    module = module or _detect_module(target)
    if not (target / "requirements.txt").exists():
        raise InstallError(f"{target} has no requirements.txt; cannot build a venv for it")
    steps["venv"] = scaffold.create_venv(target)

    # The catalogue's port is a preference. Taking it when free keeps a server
    # on the port its own documentation quotes.
    chosen = supervise.free_port(preferred=port, taken=config.used_ports())

    # Allocating a port is only half of it: the server binds whatever *it*
    # decides, so unless we can tell it, it ignores our choice and either binds
    # something else or fails on a port already in use. Refuse to pretend
    # otherwise when the port had to move and we have no way to say so.
    env: dict[str, str] = {}
    if port_env:
        env[port_env] = str(chosen)
    elif port is not None and chosen != port:
        raise InstallError(
            f"port {port} is taken and this server has no known environment "
            f"variable for its port, so it cannot be moved to {chosen}. Free "
            f"port {port}, or pass port_env with the variable it reads."
        )

    spec = config.ServerSpec(
        name=name,
        command=[steps["venv"]["python"], "-m", module],
        cwd=str(target),
        port=chosen,
        path="/mcp",
        env=env,
        autostart=autostart,
        enabled=True,
    )
    config.upsert(spec)
    steps["config"] = spec.summary()

    if start:
        try:
            steps["start"] = supervise.start(spec)
        except supervise.SuperviseError as exc:
            steps["start"] = {"error": str(exc)}
    if register:
        try:
            steps["registration"] = claude_cli.register(name, spec.url or "")
        except claude_cli.ClaudeCliError as exc:
            steps["registration"] = {"error": str(exc)}

    return {
        "name": name,
        "module": module,
        "directory": str(target),
        "port": chosen,
        "url": spec.url,
        "logs": str(paths.log_file(name)),
        **steps,
    }
