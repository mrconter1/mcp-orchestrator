"""Optional lock-down of the orchestrator's own endpoint.

The orchestrator can start any command line it is told to (``mcp_add``,
``mcp_install``, ``mcp_scaffold``, ``mcp_update``). Listening on 127.0.0.1
keeps other machines out and the MCP SDK's Host check keeps web pages out, but
neither stops another program or another Windows user on the same machine from
reaching the port. Two switches close that, both off unless set, so a plain
developer install behaves exactly as before:

``MCP_ORCHESTRATOR_TOKEN_FILE``
    A DPAPI-protected file holding a bearer token. When set, every request
    under ``/mcp`` must send ``Authorization: Bearer <token>``. The token is
    made the first time if there is no file. A file that exists but cannot be
    read refuses every request (fail closed) instead of opening the door.

``MCP_ORCHESTRATOR_READONLY=1``
    The tools that add, install, scaffold, update, remove or uninstall a server
    are not registered at all. Starting, stopping, restarting and reading the
    logs of the servers already configured still works.

A bundled install (such as an installer that ships its own server set) turns
both on; the end user then never needs the orchestrator's tools.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any

PROTECTED_PREFIX = "/mcp"
MIN_LENGTH = 32
# Tools that change which command lines the orchestrator will run.
MUTATING_TOOLS = (
    "mcp_add",
    "mcp_remove",
    "mcp_scaffold",
    "mcp_install",
    "mcp_update",
    "mcp_uninstall",
)


def readonly() -> bool:
    return os.environ.get("MCP_ORCHESTRATOR_READONLY", "").strip().lower() in ("1", "true", "yes", "on")


def token_file() -> Path | None:
    raw = os.environ.get("MCP_ORCHESTRATOR_TOKEN_FILE", "").strip()
    return Path(raw) if raw else None


def _protect(data: bytes) -> bytes:
    try:
        from msal_extensions.windows import WindowsDataProtectionAgent

        return WindowsDataProtectionAgent().protect(data.decode())
    except ImportError:
        import win32crypt

        return win32crypt.CryptProtectData(data, None, None, None, None, 0)


def _unprotect(blob: bytes) -> str:
    try:
        from msal_extensions.windows import WindowsDataProtectionAgent

        return WindowsDataProtectionAgent().unprotect(blob) or ""
    except ImportError:
        import win32crypt

        return win32crypt.CryptUnprotectData(blob, None, None, None, 0)[1].decode()


def read_token(path: Path, attempts: int = 5, delay: float = 0.1) -> str | None:
    """The token in ``path``; None if there is no file. Raises if the file
    exists but stays unreadable."""
    last: Exception | None = None
    for attempt in range(max(1, attempts)):
        try:
            blob = path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            last = exc
        else:
            try:
                token = _unprotect(blob).strip()
            except Exception as exc:  # noqa: BLE001 -- DPAPI raises several types
                last = exc
            else:
                if len(token) >= MIN_LENGTH:
                    return token
                last = ValueError("token too short")
        if attempt + 1 < attempts:
            time.sleep(delay)
    raise RuntimeError(f"cannot read {path}: {type(last).__name__}")


def write_token(path: Path, token: str) -> None:
    if len(token) < MIN_LENGTH:
        raise ValueError("token too short")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    tmp.write_bytes(_protect(token.encode()))
    try:
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def load_token(path: Path) -> str | None:
    """The token to require, made once if there is no file. None means the
    file is there but unreadable: every request is then refused."""
    try:
        token = read_token(path)
    except RuntimeError:
        return None
    if token is None:
        token = secrets.token_urlsafe(32)
        write_token(path, token)
    return token


def _given(scope: dict[str, Any]) -> str | None:
    for name, value in scope.get("headers") or []:
        if name.lower() == b"authorization":
            scheme, _, given = value.decode("latin-1").partition(" ")
            return given.strip() if scheme.lower() == "bearer" else None
    return None


async def _refuse(send: Any, status: int, detail: str) -> None:
    body = json.dumps({"error": "unauthorized", "detail": detail}).encode()
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
            (b"www-authenticate", b'Bearer realm="local"'),
        ],
    })
    await send({"type": "http.response.body", "body": body})


def protect(app: Any, token: str | None) -> Any:
    """Wrap an ASGI app so every request under /mcp needs ``token``.

    ``token`` None (the file exists but could not be read) refuses everything.
    """

    async def guarded(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "http" and scope.get("path", "").startswith(PROTECTED_PREFIX):
            if token is None:
                await _refuse(send, 503, "token file unreadable; endpoint locked")
                return
            given = _given(scope)
            if given is None or not hmac.compare_digest(
                given.encode("utf-8", "surrogateescape"), token.encode()
            ):
                await _refuse(send, 401, "missing or wrong bearer token")
                return
        await app(scope, receive, send)

    return guarded


def wrap(app: Any) -> Any:
    """``app`` behind the bearer check when a token file is configured, else as is."""
    path = token_file()
    if path is None:
        return app
    return protect(app, load_token(path))
