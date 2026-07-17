"""管理 API：类型/规则 CRUD、启停、逻辑删除、审计查询（Admin Token 鉴权）。"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query

from sensitive_content import store
from sensitive_content.api import (
    GLOBAL_TENANT_PATH,
    require_admin,
    resolve_operator,
    resolve_tenant,
)
from sensitive_content.models import RuleCreate, RuleUpdate, TypeCreate, TypeUpdate

router = APIRouter(
    prefix="/api/v1/tenants/{tenant_id}",
    dependencies=[Depends(require_admin)],
    tags=["admin"],
)


def _page_meta(page: int, page_size: int, total: int) -> dict[str, int]:
    return {"page": page, "page_size": page_size, "total": total}


# ---------------------------------------------------------------------------
# 敏感内容类型
# ---------------------------------------------------------------------------

@router.post("/sensitive-types", status_code=201)
def create_type(
    tenant_id: str,
    body: TypeCreate,
    operator: str = Depends(resolve_operator),
) -> dict[str, Any]:
    return store.create_type(resolve_tenant(tenant_id), body, operator)


@router.get("/sensitive-types")
def list_types(
    tenant_id: str,
    enabled: Optional[bool] = Query(None),
    include_global: bool = Query(True),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
) -> dict[str, Any]:
    items, total = store.list_types(
        resolve_tenant(tenant_id),
        enabled=enabled,
        include_global=include_global,
        page=page,
        page_size=page_size,
    )
    return {"items": items, **_page_meta(page, page_size, total)}


@router.post("/sensitive-types/{type_id}:enable")
def enable_type(
    tenant_id: str, type_id: int, operator: str = Depends(resolve_operator)
) -> dict[str, Any]:
    return store.set_type_enabled(resolve_tenant(tenant_id), type_id, True, operator)


@router.post("/sensitive-types/{type_id}:disable")
def disable_type(
    tenant_id: str, type_id: int, operator: str = Depends(resolve_operator)
) -> dict[str, Any]:
    return store.set_type_enabled(resolve_tenant(tenant_id), type_id, False, operator)


@router.get("/sensitive-types/{type_id}")
def get_type(tenant_id: str, type_id: int) -> dict[str, Any]:
    return store.get_type(resolve_tenant(tenant_id), type_id)


@router.put("/sensitive-types/{type_id}")
def update_type(
    tenant_id: str,
    type_id: int,
    body: TypeUpdate,
    operator: str = Depends(resolve_operator),
) -> dict[str, Any]:
    return store.update_type(resolve_tenant(tenant_id), type_id, body, operator)


@router.delete("/sensitive-types/{type_id}", status_code=204)
def delete_type(
    tenant_id: str, type_id: int, operator: str = Depends(resolve_operator)
) -> None:
    store.delete_type(resolve_tenant(tenant_id), type_id, operator)


# ---------------------------------------------------------------------------
# 敏感内容规则
# ---------------------------------------------------------------------------

@router.post("/sensitive-rules", status_code=201)
def create_rule(
    tenant_id: str,
    body: RuleCreate,
    operator: str = Depends(resolve_operator),
) -> dict[str, Any]:
    return store.create_rule(resolve_tenant(tenant_id), body, operator)


@router.get("/sensitive-rules")
def list_rules(
    tenant_id: str,
    keyword: Optional[str] = Query(None),
    type_id: Optional[int] = Query(None),
    enabled: Optional[bool] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
) -> dict[str, Any]:
    items, total = store.list_rules(
        resolve_tenant(tenant_id),
        keyword=keyword,
        type_id=type_id,
        enabled=enabled,
        page=page,
        page_size=page_size,
    )
    return {"items": items, **_page_meta(page, page_size, total)}


@router.post("/sensitive-rules/{rule_id}:enable")
def enable_rule(
    tenant_id: str, rule_id: int, operator: str = Depends(resolve_operator)
) -> dict[str, Any]:
    return store.set_rule_enabled(resolve_tenant(tenant_id), rule_id, True, operator)


@router.post("/sensitive-rules/{rule_id}:disable")
def disable_rule(
    tenant_id: str, rule_id: int, operator: str = Depends(resolve_operator)
) -> dict[str, Any]:
    return store.set_rule_enabled(resolve_tenant(tenant_id), rule_id, False, operator)


@router.get("/sensitive-rules/{rule_id}")
def get_rule(tenant_id: str, rule_id: int) -> dict[str, Any]:
    return store.get_rule(resolve_tenant(tenant_id), rule_id)


@router.put("/sensitive-rules/{rule_id}")
def update_rule(
    tenant_id: str,
    rule_id: int,
    body: RuleUpdate,
    operator: str = Depends(resolve_operator),
) -> dict[str, Any]:
    return store.update_rule(resolve_tenant(tenant_id), rule_id, body, operator)


@router.delete("/sensitive-rules/{rule_id}", status_code=204)
def delete_rule(
    tenant_id: str, rule_id: int, operator: str = Depends(resolve_operator)
) -> None:
    store.delete_rule(resolve_tenant(tenant_id), rule_id, operator)


# ---------------------------------------------------------------------------
# 命中事件明细
# ---------------------------------------------------------------------------

@router.get("/hit-events")
def list_hit_events(
    tenant_id: str,
    rule_id: Optional[int] = Query(None),
    type_id: Optional[int] = Query(None),
    session_id: Optional[str] = Query(None),
    time_from: Optional[datetime] = Query(None, alias="from"),
    time_to: Optional[datetime] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
) -> dict[str, Any]:
    """命中事件明细分页查询：响应携带明文 session_id，前端可据此
    调用网关会话接口（/effyic/v1/sessions/{session_id}）查看完整会话。
    tenant_id=global 表示跨全部租户（命中事件的 tenant_id 均为真实租户）。
    """
    items, total = store.list_hit_events(
        None if tenant_id == GLOBAL_TENANT_PATH else tenant_id,
        rule_id=rule_id,
        type_id=type_id,
        session_id=session_id,
        time_from=time_from,
        time_to=time_to,
        page=page,
        page_size=page_size,
    )
    return {"items": items, **_page_meta(page, page_size, total)}


# ---------------------------------------------------------------------------
# 审计日志
# ---------------------------------------------------------------------------

@router.get("/audit-logs")
def list_audit_logs(
    tenant_id: str,
    target_kind: Optional[str] = Query(None),
    target_id: Optional[int] = Query(None),
    action: Optional[str] = Query(None),
    time_from: Optional[datetime] = Query(None, alias="from"),
    time_to: Optional[datetime] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
) -> dict[str, Any]:
    items, total = store.list_audit_logs(
        resolve_tenant(tenant_id),
        target_kind=target_kind,
        target_id=target_id,
        action=action,
        time_from=time_from,
        time_to=time_to,
        page=page,
        page_size=page_size,
    )
    return {"items": items, **_page_meta(page, page_size, total)}
