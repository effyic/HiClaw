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
    store.list_agents.return_value = [
        {
            "role_code": "expert_cardiology",
            "display_name": "心内科",
            "description": "",
            "workflow": {},
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
                    "route_label": "心内科（业务库）",
                    "prompt_supplements": {
                        "system_prompt_append": "允许 department_code: cardiology\\n- cardiology: 心内科（业务库）",
                    },
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
    assert business["route_label"] == "心内科（业务库）"
    assert "agents" in business

    bundle = service.build_prompt_bundle(ctx, ctx.session_state)
    assert "心内科（业务库）" in bundle["system_prompt"]
    assert "cardiology" in bundle["system_prompt"]
