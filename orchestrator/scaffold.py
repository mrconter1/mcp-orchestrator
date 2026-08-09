"""Generating a new MCP server from the templates.

The point is that one call produces something that already works: files on
disk, a venv with the SDK in it, a free port, an entry in ``servers.json``, a
registration with Claude Code and a running process answering on its port. A
scaffold that stops short of that leaves the user to find out which of the six
steps was the one that failed.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from orchestrator import claude_cli, config, paths, supervise

DEFAULT_PARENT = Path.home() / "Repos"
PIP_TIMEOUT = 600
NAME_PATTERN = re.compile(r"^[a-z][a-z0-9-]{1,31}$")


class ScaffoldError(Exception):
    """Raised when a new server cannot be generated as asked."""


def validate_name(name: str) -> str:
    """Names end up in a package name, a URL and a CLI argument, so keep them dull."""
    name = name.strip().lower()
    if not NAME_PATTERN.match(name):
        raise ScaffoldError(
            f"invalid name {name!r}: use lowercase letters, digits and hyphens, "
            f"starting with a letter (2-32 characters)"
        )
    return name


def package_name(name: str) -> str:
    """``weather`` -> ``weather_mcp``: importable, and unlikely to shadow anything."""
    base = name.replace("-", "_")
    return base if base.endswith("_mcp") else f"{base}_mcp"


def _base_python() -> str:
    """The interpreter to build the new venv with.

    The orchestrator runs from its own venv, and creating a venv from a venv
    works but inherits its parent's oddities. Prefer the base installation.
    """
    candidate = Path(sys.base_prefix) / "python.exe"
    return str(candidate) if candidate.exists() else sys.executable


def _render(template: str, substitutions: dict[str, str]) -> str:
    """Token substitution rather than str.format: the templates are full of braces."""
    text = (paths.templates_dir() / template).read_text(encoding="utf-8")
    for token, value in substitutions.items():
        text = text.replace(token, value)
    return text


def _write_files(target: Path, name: str, package: str, port: int) -> list[str]:
    substitutions = {
        "__NAME__": name,
        "__PACKAGE__": package,
        "__PORT__": str(port),
        "__ENV_PREFIX__": package.upper(),
    }
    files = {
        ".gitignore": "gitignore.tmpl",
        "requirements.txt": "requirements.txt.tmpl",
        "run.ps1": "run.ps1.tmpl",
        "README.md": "README.md.tmpl",
        f"{package}/__init__.py": "__init__.py.tmpl",
        f"{package}/server.py": "server.py.tmpl",
    }
    written = []
    for relative, template in files.items():
        path = target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_render(template, substitutions), encoding="utf-8")
        written.append(str(path))
    return written


def _create_venv(target: Path) -> dict[str, Any]:
    """Build the venv and install the SDK, reporting what pip actually said."""
    venv = target / ".venv"
    result = subprocess.run(  # noqa: S603
        [_base_python(), "-m", "venv", str(venv)],
        capture_output=True, text=True, timeout=PIP_TIMEOUT, check=False,
    )
    if result.returncode != 0:
        raise ScaffoldError(f"venv creation failed: {(result.stderr or result.stdout).strip()}")
    python = venv / "Scripts" / "python.exe"
    if not python.exists():
        raise ScaffoldError(f"venv created but {python} is missing")
    install = subprocess.run(  # noqa: S603
        [str(python), "-m", "pip", "install", "--quiet", "-r", str(target / "requirements.txt")],
        capture_output=True, text=True, timeout=PIP_TIMEOUT, check=False,
    )
    if install.returncode != 0:
        raise ScaffoldError(f"pip install failed: {(install.stderr or install.stdout).strip()}")
    return {"python": str(python), "installed": "mcp"}


def create(
    name: str,
    directory: str | None = None,
    port: int | None = None,
    register: bool = True,
    start: bool = True,
    autostart: bool = True,
) -> dict[str, Any]:
    """Generate, install, register and start a new MCP server."""
    name = validate_name(name)
    if config.get(name) is not None:
        raise ScaffoldError(f"a managed server named {name!r} already exists")

    package = package_name(name)
    target = Path(directory).expanduser() if directory else DEFAULT_PARENT / f"{name}-mcp"
    if target.exists() and any(target.iterdir()):
        raise ScaffoldError(f"{target} already exists and is not empty")

    port = supervise.free_port(preferred=port, taken=config.used_ports())
    target.mkdir(parents=True, exist_ok=True)

    steps: dict[str, Any] = {}
    steps["files"] = _write_files(target, name, package, port)
    steps["venv"] = _create_venv(target)

    spec = config.ServerSpec(
        name=name,
        command=[steps["venv"]["python"], "-m", f"{package}.server"],
        cwd=str(target),
        port=port,
        path="/mcp",
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
        "package": package,
        "directory": str(target),
        "port": port,
        "url": spec.url,
        "edit": str(target / package / "server.py"),
        **steps,
    }
