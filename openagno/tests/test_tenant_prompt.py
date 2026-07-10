"""Tests for tenant prompt builder."""
from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest

from agno_worker.tenant.prompt import TenantPromptBuilder

AGENT_DB_URL = os.environ.get(
    "AGNO_AGENT_DB_URL",
    "mysql+pymysql://root:mysql@192.168.0.111:3306/agno_worker",
)


class _FakeRunContext:
    def __init__(self, **kwargs: object) -> None:
        self.metadata = kwargs.get("metadata") or {}
        self.session_state = kwargs.get("session_state") or {}
        self.dependencies = kwargs.get("dependencies") or {}


@pytest.fixture(autouse=True)
def _agent_db_env() -> None:
    os.environ["AGNO_AGENT_DB_URL"] = AGENT_DB_URL


def test_prompt_builder_without_hook_uses_agent_config_only() -> None:
    """Standard flow must not inject scenario-specific catalog text."""
    from agno_worker.tenant.service import TenantAgentService

    service = TenantAgentService()
    ctx = _FakeRunContext(
        metadata={"tenant_id": "tenant-a"},
        session_state={"active_role": "triage", "phase": "triage"},
    )
    bundle = service.build_prompt_bundle(ctx, ctx.session_state)
    assert "cardiology" not in bundle["system_prompt"]
    assert "分诊" in bundle["system_prompt"] or "triage" in bundle["system_prompt"].lower()
    assert bundle["context_filters"]["tenant_id"] == "tenant-a"


def test_prompt_supplements_via_business_context() -> None:
    from agno_worker.tenant.context import TenantContext, TenantContextResolver
    from unittest.mock import patch

    builder = TenantPromptBuilder()
    ctx = _FakeRunContext(
        metadata={"tenant_id": "tenant-a"},
        session_state={"active_role": "triage"},
    )
    fake_cfg = {
        "system_prompt": "分诊助手",
        "instructions": "请分诊",
        "workflow": {"kind": "triage"},
        "knowledge_ids": [],
        "role_code": "triage",
    }
    fake_tenant_ctx = TenantContext(
        tenant_id="tenant-a",
        role_code="triage",
        agent_config=fake_cfg,
    )
    business = {"prompt_supplements": {"system_prompt_append": "允许 department_code: cardiology"}}

    with patch.object(TenantContextResolver, "resolve", return_value=fake_tenant_ctx):
        bundle = builder.build_prompt_bundle(ctx, ctx.session_state, business_context=business)

    assert "cardiology" in bundle["system_prompt"]
    assert "分诊助手" in bundle["system_prompt"]


def test_transform_prompt_hook_applied() -> None:
    from agno_worker.hooks.registry import HookRegistry
    from agno_worker.tenant.service import TenantAgentService

    registry = MagicMock(spec=HookRegistry)
    registry.has.side_effect = lambda name: name == "transform_prompt_hook"

    def _transform(name: str, *args: object) -> dict:
        bundle = dict(args[-1])  # type: ignore[arg-type]
        bundle["system_prompt"] = str(bundle["system_prompt"]) + " [custom]"
        return bundle

    registry.call.side_effect = _transform
    service = TenantAgentService(registry=registry)
    ctx = _FakeRunContext(metadata={"tenant_id": "default"})
    bundle = service.build_prompt_bundle(ctx)
    assert bundle["system_prompt"].endswith("[custom]")
