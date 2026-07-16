"""统计查询 API（Admin Token 鉴权）。

tenant_id=global 表示跨全部租户聚合（命中事件的 tenant_id 均为真实租户）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query

from sensitive_content import store
from sensitive_content.api import GLOBAL_TENANT_PATH, require_admin

router = APIRouter(
    prefix="/api/v1/tenants/{tenant_id}/metrics",
    dependencies=[Depends(require_admin)],
    tags=["metrics"],
)


def _metric_tenant(tenant_id: str) -> str | None:
    """统计口径：global 聚合全部租户（返回 None 表示不过滤）。"""
    return None if tenant_id == GLOBAL_TENANT_PATH else tenant_id


@router.get("/summary")
def summary(
    tenant_id: str,
    time_from: Optional[datetime] = Query(None, alias="from"),
    time_to: Optional[datetime] = Query(None, alias="to"),
) -> dict[str, Any]:
    return store.metrics_summary(_metric_tenant(tenant_id), time_from, time_to)


@router.get("/by-rule")
def by_rule(
    tenant_id: str,
    time_from: Optional[datetime] = Query(None, alias="from"),
    time_to: Optional[datetime] = Query(None, alias="to"),
    top: int = Query(10, ge=1, le=1000),
) -> dict[str, Any]:
    return {"items": store.metrics_by_rule(_metric_tenant(tenant_id), time_from, time_to, top)}


@router.get("/by-type")
def by_type(
    tenant_id: str,
    time_from: Optional[datetime] = Query(None, alias="from"),
    time_to: Optional[datetime] = Query(None, alias="to"),
    top: int = Query(10, ge=1, le=1000),
) -> dict[str, Any]:
    return {"items": store.metrics_by_type(_metric_tenant(tenant_id), time_from, time_to, top)}


@router.get("/by-action")
def by_action(
    tenant_id: str,
    time_from: Optional[datetime] = Query(None, alias="from"),
    time_to: Optional[datetime] = Query(None, alias="to"),
) -> dict[str, Any]:
    return store.metrics_by_action(_metric_tenant(tenant_id), time_from, time_to)


@router.get("/trend")
def trend(
    tenant_id: str,
    time_from: Optional[datetime] = Query(None, alias="from"),
    time_to: Optional[datetime] = Query(None, alias="to"),
    granularity: str = Query("day"),
) -> dict[str, Any]:
    return {
        "items": store.metrics_trend(
            _metric_tenant(tenant_id), time_from, time_to, granularity
        )
    }
