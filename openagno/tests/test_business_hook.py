"""Tests for enrich_business_context_hook."""
from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import MagicMock

from agno_worker.hooks.registry import HookRegistry
from agno_worker.tenant.service import TenantAgentService


class _FakeRunContext:
    def __init__(self, **kwargs: object) -> None:
        self.metadata = kwargs.get("metadata") or {}
        self.session_state = kwargs.get("session_state") or {}
        self.dependencies = kwargs.get("dependencies") or {}


def _mock_store() -> MagicMock:
    store = MagicMock()
    store.load_agent_resolved.return_value = {
        "tenant_id": "tenant-a",
        "role_code": "triage",
        "workflow": {"kind": "triage"},
        "system_prompt": "分诊助手",
        "instructions": "请分诊",
        "knowledge_ids": [],
        "mcp_enabled": False,
        "mcp_config": None,
    }
    store.list_expert_agents.return_value = [
        {
            "role_code": "expert_cardiology",
            "route_key": "cardiology",
            "display_name": "心内科",
            "description": "",
        }
    ]
    return store


def test_enrich_business_context_hook_merges(tmp_path: Path) -> None:
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir()
    (hooks_dir / "business.py").write_text(
        textwrap.dedent(
            """
            def enrich_business_context_hook(run_context, base_context):
                return {
                    "department_name": "心内科（业务库）",
                    "triage_departments": [
                        {
                            "department_code": "cardiology",
                            "department_name": "心内科（业务库）",
                            "role_code": "expert_cardiology",
                            "description": "来自 department 表",
                        }
                    ],
                }
            """
        ),
        encoding="utf-8",
    )
    registry = HookRegistry(hooks_dir)
    service = TenantAgentService(registry=registry, store=_mock_store())
    ctx = _FakeRunContext(
        metadata={"tenant_id": "tenant-a"},
        session_state={"active_role": "triage", "phase": "triage"},
    )
    business = service.resolve_business_context(ctx)
    assert business["department_name"] == "心内科（业务库）"
    assert business["triage_departments"][0]["department_name"] == "心内科（业务库）"
    assert "expert_agents" in business

    bundle = service.build_prompt_bundle(ctx, ctx.session_state)
    assert "心内科（业务库）" in bundle["system_prompt"]
