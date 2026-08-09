"""The configured server set: ``servers.json``.

Shape::

    {
      "version": 1,
      "servers": [
        {
          "name": "queue",
          "command": ["C:/.../.venv/Scripts/python.exe", "-m", "queue_mcp.server"],
          "cwd": "C:/Users/.../Repos/queue-mcp",
          "port": 8766,
          "path": "/mcp",
          "env": {},
          "autostart": true,
          "enabled": true
        }
      ]
    }

The file is meant to be hand-editable, so ``command`` is accepted either as a
list of arguments or as a single string, and every other field has a default.
Anything unrecognised is preserved on save rather than silently dropped.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from typing import Any

from orchestrator import paths

DEFAULT_PATH = "/mcp"
CONFIG_VERSION = 1

# 8765-8767 are already taken on this machine (queue-mcp, session-control-mcp and
# one other), and 8768 is the orchestrator itself, so scaffolding allocates above.
PORT_RANGE = (8769, 8899)


class ConfigError(Exception):
    """Raised for a server definition that cannot be used as written."""


@dataclass
class ServerSpec:
    """One managed MCP server."""

    name: str
    command: list[str]
    cwd: str | None = None
    port: int | None = None
    path: str = DEFAULT_PATH
    env: dict[str, str] = field(default_factory=dict)
    autostart: bool = True
    enabled: bool = True
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def url(self) -> str | None:
        if self.port is None:
            return None
        return f"http://127.0.0.1:{self.port}{self.path}"

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = dict(self.extra)
        data.update(
            {
                "name": self.name,
                "command": list(self.command),
                "cwd": self.cwd,
                "port": self.port,
                "path": self.path,
                "env": dict(self.env),
                "autostart": self.autostart,
                "enabled": self.enabled,
            }
        )
        return data

    def summary(self) -> dict[str, Any]:
        """The subset worth showing in tool output."""
        return {
            "name": self.name,
            "port": self.port,
            "url": self.url,
            "cwd": self.cwd,
            "autostart": self.autostart,
            "enabled": self.enabled,
            "command": " ".join(self.command),
        }


_KNOWN = {"name", "command", "cwd", "port", "path", "env", "autostart", "enabled"}


def _as_command(value: Any, name: str) -> list[str]:
    if isinstance(value, list):
        command = [str(part) for part in value]
    elif isinstance(value, str):
        # posix=False keeps Windows backslashes intact; shlex would otherwise
        # read C:\Users\... as escape sequences and hand over a mangled path.
        command = shlex.split(value, posix=False)
    else:
        raise ConfigError(f"server {name!r}: command must be a list or a string")
    if not command:
        raise ConfigError(f"server {name!r}: command is empty")
    return command


def spec_from_dict(data: dict[str, Any]) -> ServerSpec:
    name = str(data.get("name") or "").strip()
    if not name:
        raise ConfigError("server entry has no name")
    port = data.get("port")
    if port is not None:
        try:
            port = int(port)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"server {name!r}: port {port!r} is not a number") from exc
    env = data.get("env") or {}
    if not isinstance(env, dict):
        raise ConfigError(f"server {name!r}: env must be an object")
    return ServerSpec(
        name=name,
        command=_as_command(data.get("command"), name),
        cwd=str(data["cwd"]) if data.get("cwd") else None,
        port=port,
        path=str(data.get("path") or DEFAULT_PATH),
        env={str(k): str(v) for k, v in env.items()},
        autostart=bool(data.get("autostart", True)),
        enabled=bool(data.get("enabled", True)),
        extra={k: v for k, v in data.items() if k not in _KNOWN},
    )


def load() -> list[ServerSpec]:
    """Every configured server, skipping entries too broken to use.

    A single bad entry must not hide the rest: it is dropped with its error kept
    in :func:`load_with_errors`, which is what the list tool reports.
    """
    specs, _ = load_with_errors()
    return specs


def load_with_errors() -> tuple[list[ServerSpec], list[str]]:
    raw = paths.read_json(paths.config_file(), {})
    entries = raw.get("servers") if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        return [], []
    specs: list[ServerSpec] = []
    errors: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            errors.append(f"ignored non-object entry: {entry!r}")
            continue
        try:
            spec = spec_from_dict(entry)
        except ConfigError as exc:
            errors.append(str(exc))
            continue
        if spec.name in seen:
            errors.append(f"duplicate server name {spec.name!r}: keeping the first")
            continue
        seen.add(spec.name)
        specs.append(spec)
    return specs, errors


def save(specs: list[ServerSpec]) -> None:
    paths.write_json(
        paths.config_file(),
        {"version": CONFIG_VERSION, "servers": [spec.to_dict() for spec in specs]},
    )


def get(name: str) -> ServerSpec | None:
    for spec in load():
        if spec.name == name:
            return spec
    return None


def upsert(spec: ServerSpec) -> None:
    specs = load()
    for index, existing in enumerate(specs):
        if existing.name == spec.name:
            specs[index] = spec
            break
    else:
        specs.append(spec)
    save(specs)


def remove(name: str) -> bool:
    specs = load()
    kept = [spec for spec in specs if spec.name != name]
    if len(kept) == len(specs):
        return False
    save(kept)
    return True


def used_ports() -> set[int]:
    return {spec.port for spec in load() if spec.port is not None}
