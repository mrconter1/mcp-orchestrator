"""The optional bearer lock and read-only mode of the orchestrator endpoint."""

from __future__ import annotations

import asyncio
import importlib
import sys

import pytest

from orchestrator import guard


def _call(app, path="/mcp", token=None):
    headers = [(b"host", b"127.0.0.1")]
    if token is not None:
        headers.append((b"authorization", f"Bearer {token}".encode()))
    scope = {"type": "http", "path": path, "headers": headers, "method": "POST"}
    sent = []

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    return sent


async def _inner(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def _status(sent):
    return sent[0]["status"]


def test_no_token_file_leaves_the_app_untouched(monkeypatch):
    monkeypatch.delenv("MCP_ORCHESTRATOR_TOKEN_FILE", raising=False)
    assert guard.wrap(_inner) is _inner


def test_token_is_required_when_configured(monkeypatch, tmp_path):
    path = tmp_path / "orch.token"
    monkeypatch.setenv("MCP_ORCHESTRATOR_TOKEN_FILE", str(path))
    app = guard.wrap(_inner)
    token = guard.read_token(path)
    assert token and len(token) >= guard.MIN_LENGTH
    assert b"Bearer" not in path.read_bytes() and token.encode() not in path.read_bytes()
    assert _status(_call(app)) == 401
    assert _status(_call(app, token="x" * 40)) == 401
    assert _status(_call(app, token=token)) == 200


def test_paths_outside_mcp_pass(monkeypatch, tmp_path):
    monkeypatch.setenv("MCP_ORCHESTRATOR_TOKEN_FILE", str(tmp_path / "orch.token"))
    app = guard.wrap(_inner)
    assert _status(_call(app, path="/health")) == 200


def test_an_unreadable_token_file_locks_everything(monkeypatch, tmp_path):
    path = tmp_path / "orch.token"
    path.write_bytes(b"not a dpapi blob")
    monkeypatch.setenv("MCP_ORCHESTRATOR_TOKEN_FILE", str(path))
    app = guard.wrap(_inner)
    assert _status(_call(app)) == 503
    assert _status(_call(app, token="x" * 40)) == 503
    # The broken file is never replaced here.
    assert path.read_bytes() == b"not a dpapi blob"


def test_an_existing_token_is_kept(tmp_path):
    path = tmp_path / "orch.token"
    guard.write_token(path, "a" * 40)
    assert guard.load_token(path) == "a" * 40


@pytest.mark.parametrize("value,expected", [("1", True), ("true", True), ("", False), ("0", False)])
def test_readonly_flag(monkeypatch, value, expected):
    monkeypatch.setenv("MCP_ORCHESTRATOR_READONLY", value)
    assert guard.readonly() is expected


def _tool_names(module):
    return {tool.name for tool in module.server._tool_manager.list_tools()}


def test_readonly_drops_the_tools_that_change_command_lines(monkeypatch):
    monkeypatch.setenv("MCP_ORCHESTRATOR_READONLY", "1")
    sys.modules.pop("orchestrator.server", None)
    module = importlib.import_module("orchestrator.server")
    names = _tool_names(module)
    for name in guard.MUTATING_TOOLS:
        assert name not in names
    assert {"mcp_list", "mcp_start", "mcp_stop", "mcp_logs"} <= names
    sys.modules.pop("orchestrator.server", None)


def test_default_keeps_every_tool(monkeypatch):
    monkeypatch.delenv("MCP_ORCHESTRATOR_READONLY", raising=False)
    sys.modules.pop("orchestrator.server", None)
    module = importlib.import_module("orchestrator.server")
    names = _tool_names(module)
    for name in guard.MUTATING_TOOLS:
        assert name in names
    sys.modules.pop("orchestrator.server", None)
