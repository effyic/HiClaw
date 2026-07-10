"""Tests for run-scoped tenant pipeline cache."""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from agno_worker.tenant.service import TenantAgentService


class _FakeRunContext:
    def __init__(self, **kwargs: object) -> None:
        self.metadata = kwargs.get("metadata") or {}
        self.session_state = kwargs.get("session_state") or {}
        self.dependencies = kwargs.get("dependencies") or {}


def test_prepare_run_context_caches_prompt_bundle() -> None:
    store = MagicMock()
    store.load_agent_resolved.return_value = {
        "tenant_id": "tenant-a",
        "role_code": "default",
        "workflow": {"kind": "default"},
        "system_prompt": "hello",
        "instructions": "help",
        "knowledge_ids": [],
        "mcp_enabled": False,
        "mcp_config": None,
    }
    store.list_expert_agents.return_value = []

    service = TenantAgentService(store=store)
    ctx = _FakeRunContext(metadata={"tenant_id": "tenant-a"})

    service.prepare_run_context(ctx, ctx.session_state)
    calls_after_prepare = store.load_agent_resolved.call_count
    bundle1 = service.get_prompt_bundle(ctx, ctx.session_state)
    bundle2 = service.get_prompt_bundle(ctx, ctx.session_state)

    assert "hello" in bundle1["system_prompt"]
    assert bundle1 is bundle2
    assert store.load_agent_resolved.call_count == calls_after_prepare


def test_resolve_business_context_uses_run_cache() -> None:
    store = MagicMock()
    store.load_agent_resolved.return_value = {
        "tenant_id": "tenant-a",
        "role_code": "default",
        "workflow": {"kind": "default"},
        "system_prompt": "",
        "instructions": "",
        "knowledge_ids": [],
        "mcp_enabled": False,
        "mcp_config": None,
    }
    store.list_expert_agents.return_value = [{"route_key": "cardiology"}]

    service = TenantAgentService(store=store)
    ctx = _FakeRunContext(metadata={"tenant_id": "tenant-a"})

    service.prepare_run_context(ctx, ctx.session_state)
    business1 = service.resolve_business_context(ctx)
    business2 = service.resolve_business_context(ctx)

    assert business1["tenant_id"] == "tenant-a"
    assert business1 is business2
    assert store.list_expert_agents.call_count == 1
