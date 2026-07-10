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


def test_prompt_builder_includes_triage_catalog() -> None:
    from agno_worker.tenant.service import TenantAgentService

    service = TenantAgentService()
    ctx = _FakeRunContext(
        metadata={"tenant_id": "tenant-a"},
        session_state={"active_role": "triage", "phase": "triage"},
    )
    bundle = service.build_prompt_bundle(ctx, ctx.session_state)
    assert "cardiology" in bundle["system_prompt"]
    assert bundle["context_filters"]["tenant_id"] == "tenant-a"


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
