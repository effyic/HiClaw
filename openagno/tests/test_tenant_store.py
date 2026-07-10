"""Tests for tenant store against MySQL agno_agent table."""
from __future__ import annotations

import os

import pytest

from agno_worker.tenant.context import TenantContextResolver
from agno_worker.tenant.store import AgentStore


AGENT_DB_URL = os.environ.get(
    "AGNO_AGENT_DB_URL",
    "mysql+pymysql://root:mysql@192.168.0.111:3306/agno_worker",
)


@pytest.fixture(scope="module", autouse=True)
def _agent_db_env() -> None:
    os.environ["AGNO_AGENT_DB_URL"] = AGENT_DB_URL


class _FakeRunContext:
    def __init__(self, **kwargs: object) -> None:
        self.metadata = kwargs.get("metadata") or {}
        self.session_state = kwargs.get("session_state") or {}
        self.dependencies = kwargs.get("dependencies") or {}


def test_store_loads_default_tenant() -> None:
    store = AgentStore()
    cfg = store.load_agent("default")
    assert cfg["tenant_id"] == "default"
    assert cfg["role_code"] == "default"
    assert cfg["system_prompt"]


def test_store_loads_tenant_a_triage() -> None:
    store = AgentStore()
    cfg = store.load_agent_resolved("tenant-a", role_code="triage", kind="triage")
    assert cfg["role_code"] == "triage"
    assert cfg["workflow"]["kind"] == "triage"


def test_store_lists_expert_agents() -> None:
    store = AgentStore()
    experts = store.list_expert_agents("tenant-a")
    codes = {item["route_key"] for item in experts}
    assert "cardiology" in codes


def test_context_resolver_loads_expert_by_route() -> None:
    resolver = TenantContextResolver()
    ctx = _FakeRunContext(
        metadata={"tenant_id": "tenant-a"},
        session_state={
            "workflow": {"kind": "expert", "route_key": "cardiology", "phase": "consultation"},
            "role_code": "expert_cardiology",
        },
    )
    resolved = resolver.resolve(ctx)
    assert resolved.tenant_id == "tenant-a"
    assert resolved.agent_config["workflow"]["kind"] == "expert"
