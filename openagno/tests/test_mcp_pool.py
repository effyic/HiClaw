"""Tests for MCP connection pooling."""
from __future__ import annotations

import asyncio
import sys
import types
from types import SimpleNamespace

import pytest

from agno_worker.hooks.protocols import MCPServerConfig
from agno_worker.mcp.loader import build_mcp_tools
from agno_worker.mcp.pool import (
    MCPToolsPool,
    clear_mcp_tools_pool,
    get_default_pool,
    pool_key,
    prepare_pooled_tool,
    tune_refresh_connection,
)


@pytest.fixture(autouse=True)
def _reset_pool(monkeypatch):
    clear_mcp_tools_pool()
    monkeypatch.setenv("AGNO_MCP_POOL", "true")
    monkeypatch.delenv("AGNO_MCP_REFRESH_CONNECTION", raising=False)
    yield
    clear_mcp_tools_pool()


def _fake_tool(**overrides):
    base = {
        "name": "s1",
        "session": None,
        "_initialized": False,
        "refresh_connection": False,
        "close": lambda: None,
        "is_alive": lambda: False,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_pool_key_is_name_url_only():
    a = MCPServerConfig(
        name="medical",
        url="http://mcp.example/mcp",
        headers={
            "tenant-id": "1",
            "campus-id": "9",
            "X-API-Key": "secret-a",
            "user-id": "u1",
            "session-id": "s1",
            "x-request-id": "req-a",
        },
        include_tools=["a"],
        transport="streamable-http",
    )
    b = MCPServerConfig(
        name="medical",
        url="http://mcp.example/mcp",
        headers={
            "tenant-id": "2",
            "campus-id": "1",
            "X-API-Key": "secret-b",
            "user-id": "u2",
            "session-id": "s2",
            "x-request-id": "req-b",
        },
        include_tools=["b"],
        transport="sse",
    )
    # Same name+url → same pool entry regardless of tenant/session/headers.
    assert pool_key(a) == pool_key(b) == ("medical", "http://mcp.example/mcp")

    other_name = MCPServerConfig(
        name="other",
        url="http://mcp.example/mcp",
        headers={"tenant-id": "1"},
    )
    assert pool_key(a) != pool_key(other_name)

    other_url = MCPServerConfig(
        name="medical",
        url="http://mcp.other/mcp",
        headers={"tenant-id": "1"},
    )
    assert pool_key(a) != pool_key(other_url)


def test_pool_reuses_instance_and_disables_refresh_when_ready():
    pool = MCPToolsPool()
    server = MCPServerConfig(name="s1", url="http://mcp.example/mcp")
    created: list[object] = []

    def factory():
        tool = _fake_tool()
        created.append(tool)
        return tool

    first = pool.get_or_create(server, factory)
    assert len(created) == 1
    assert first.refresh_connection is True

    first.session = object()
    first._initialized = True

    second = pool.get_or_create(server, factory)
    assert second is first
    assert len(created) == 1
    assert second.refresh_connection is False
    assert len(pool) == 1


def test_stable_is_alive_skips_ping_when_initialized():
    ping_calls = {"n": 0}

    async def flaky_ping():
        ping_calls["n"] += 1
        raise RuntimeError("ping not supported")

    tool = _fake_tool(session=object(), _initialized=True, is_alive=flaky_ping)
    prepare_pooled_tool(tool)
    assert asyncio.run(tool.is_alive()) is True
    assert ping_calls["n"] == 0


def test_soft_close_keeps_connection():
    closed = {"n": 0}

    async def real_close():
        closed["n"] += 1

    tool = _fake_tool(
        session=object(),
        _initialized=True,
        close=real_close,
    )
    prepare_pooled_tool(tool)
    asyncio.run(tool.close())
    assert closed["n"] == 0
    assert tool._initialized is True


def test_refresh_override_env(monkeypatch):
    tool = _fake_tool(session=object(), _initialized=True)
    monkeypatch.setenv("AGNO_MCP_REFRESH_CONNECTION", "true")
    tune_refresh_connection(tool)
    assert tool.refresh_connection is True

    monkeypatch.setenv("AGNO_MCP_REFRESH_CONNECTION", "false")
    tune_refresh_connection(tool)
    assert tool.refresh_connection is False


def test_pool_disabled_creates_new_each_time(monkeypatch):
    monkeypatch.setenv("AGNO_MCP_POOL", "false")
    pool = MCPToolsPool()
    server = MCPServerConfig(name="s1", url="http://mcp.example/mcp")
    created: list[object] = []

    def factory():
        tool = _fake_tool()
        created.append(tool)
        return tool

    a = pool.get_or_create(server, factory)
    b = pool.get_or_create(server, factory)
    assert a is not b
    assert len(created) == 2
    assert len(pool) == 0


def test_build_mcp_tools_reuses_via_default_pool(monkeypatch):
    created: list[object] = []

    class FakeParams:
        def __init__(self, url: str, headers: dict | None = None):
            self.url = url
            self.headers = headers

    class FakeMCPTools:
        def __init__(self, **kwargs):
            self.name = kwargs.get("name", "")
            self.session = None
            self._initialized = False
            self.refresh_connection = kwargs.get("refresh_connection", False)
            self.server_params = kwargs.get("server_params")
            self.header_provider = kwargs.get("header_provider")
            created.append(self)

        async def close(self):
            return None

        async def is_alive(self):
            return False

    fake = types.ModuleType("agno.tools.mcp")
    fake.MCPTools = FakeMCPTools
    fake.StreamableHTTPClientParams = FakeParams
    monkeypatch.setitem(sys.modules, "agno.tools.mcp", fake)

    server = MCPServerConfig(
        name="aiphub",
        url="http://172.16.1.23:48080/mcp",
        headers={"tenant-id": "1", "user-id": "u-1", "session-id": "s-1"},
    )
    handler = SimpleNamespace(on_mcp_connection=lambda _s: None)

    first = build_mcp_tools([server], handler)
    server.headers = {"tenant-id": "1", "user-id": "u-2", "session-id": "s-2"}
    second = build_mcp_tools([server], handler)

    assert len(first) == 1 and len(second) == 1
    assert first[0] is second[0]
    assert len(created) == 1
    assert len(get_default_pool()) == 1
    # Static auth stay on server_params; identity (tenant/session/user) via header_provider.
    assert first[0].server_params.headers == {}
    assert callable(first[0].header_provider)
    # Provider reads current run_context so pooled reuse keeps the right session.
    ctx = SimpleNamespace(
        user_id="u-9",
        session_id="s-9",
        tenant_id="1",
        role_code=None,
        metadata={},
    )
    assert first[0].header_provider(run_context=ctx) == {
        "user-id": "u-9",
        "session-id": "s-9",
        "tenant-id": "1",
    }


def test_split_static_and_per_run_headers():
    from agno_worker.mcp.headers import split_static_and_per_run_headers

    static, per_run = split_static_and_per_run_headers(
        {
            "tenant-id": "1",
            "campus-id": "9",
            "X-API-Key": "secret",
            "user-id": "u1",
            "session-id": "s1",
            "role-code": "medical-inquiry",
            "x-request-id": "r1",
        }
    )
    assert static == {
        "campus-id": "9",
        "X-API-Key": "secret",
        "x-request-id": "r1",
    }
    assert per_run == {
        "tenant-id": "1",
        "user-id": "u1",
        "session-id": "s1",
        "role-code": "medical-inquiry",
    }
