"""敏感内容绑定使用实际 agno_agent.id 的解析测试。"""
from __future__ import annotations

from agno_worker.tenant.service import TenantAgentService
from agno_worker.tenant.store import _row_to_config


class FakeStore:
    def __init__(self, config):
        self.config = config

    def load_agent_resolved(self, tenant_id: str, *, role_code: str = "default"):
        return dict(self.config)

    def clear_cache(self):
        pass


def test_row_config_preserves_agent_primary_key():
    config = _row_to_config(
        {"id": 42, "tenant_id": "t1", "role_code": "doctor", "workflow": {}}
    )
    assert config["id"] == 42


def test_same_tenant_resolution_returns_agent_id():
    service = TenantAgentService(store=FakeStore({"id": 42, "tenant_id": "t1"}))
    assert service.resolve_agent_id("t1", "doctor") == 42


def test_cross_tenant_template_fallback_uses_empty_policy_identity():
    service = TenantAgentService(store=FakeStore({"id": 1, "tenant_id": "default"}))
    assert service.resolve_agent_id("t1", "missing") == 0
