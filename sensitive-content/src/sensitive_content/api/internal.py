"""内部 API：策略快照下发 + 命中事件采集（Runtime Token 鉴权）。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, Response

from sensitive_content import store
from sensitive_content.api import require_runtime, resolve_tenant
from sensitive_content.models import HitEventBatch

router = APIRouter(
    prefix="/internal/v1",
    dependencies=[Depends(require_runtime)],
    tags=["internal"],
)


def _etag_matches(if_none_match: str, etag: str) -> bool:
    """If-None-Match 可能携带多个候选值或弱校验前缀。"""
    for candidate in if_none_match.split(","):
        candidate = candidate.strip()
        if candidate.startswith("W/"):
            candidate = candidate[2:]
        if candidate == etag or candidate == "*":
            return True
    return False


def _policy_snapshot_response(
    tenant_id: str,
    agent_id: int,
    response: Response,
    if_none_match: str = Header(""),
) -> Any:
    snapshot = store.load_policy_snapshot(resolve_tenant(tenant_id), agent_id)
    etag = snapshot["etag"]
    if if_none_match and _etag_matches(if_none_match, etag):
        # 内容未变化：304，不重复下发快照
        return Response(status_code=304, headers={"ETag": etag})
    response.headers["ETag"] = etag
    return snapshot


@router.get("/tenants/{tenant_id}/agents/{agent_id}/policy-snapshot")
def agent_policy_snapshot(
    tenant_id: str,
    agent_id: int,
    response: Response,
    if_none_match: str = Header(""),
) -> Any:
    return _policy_snapshot_response(tenant_id, agent_id, response, if_none_match)


@router.get("/tenants/{tenant_id}/policy-snapshot", deprecated=True)
def legacy_policy_snapshot(
    tenant_id: str,
    response: Response,
    if_none_match: str = Header(""),
) -> Any:
    """旧客户端安全兼容：无 Agent 身份时只返回合法空策略。"""
    return _policy_snapshot_response(tenant_id, 0, response, if_none_match)


@router.post("/hit-events:batch")
def hit_events_batch(body: HitEventBatch) -> dict[str, int]:
    events = [
        {
            "event_id": str(e.event_id),
            "rule_id": e.rule_id,
            "type_id": e.type_id,
            "rule_action": e.rule_action.value,
            "final_action": e.final_action.value,
            "selected": e.selected,
            "final_rule_id": e.final_rule_id,
            "tenant_id": e.tenant_id,
            "agent_id": e.agent_id,
            "request_fingerprint": e.request_fingerprint,
            "session_fingerprint": e.session_fingerprint,
            "session_id": e.session_id,
            "policy_version": e.policy_version,
            "hit_count": e.hit_count,
            "hit_at": e.hit_at,
        }
        for e in body.events
    ]
    accepted, duplicates = store.insert_hit_events(events)
    return {"accepted": accepted, "duplicates": duplicates}
