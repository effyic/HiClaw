"""审计写入（与业务写操作同事务）+ 策略版本递增。"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import text


def sanitize_changes(fields: dict[str, Any]) -> dict[str, Any]:
    """将变更字段转成仅含元数据的形式：字段名 + 值哈希/长度，不存明文。

    纯函数，便于单测断言审计内容不含明文。
    """
    out: dict[str, Any] = {}
    for name, value in fields.items():
        if value is None:
            out[name] = {"null": True}
            continue
        if isinstance(value, (dict, list)):
            raw = json.dumps(value, ensure_ascii=False, sort_keys=True)
        else:
            raw = str(value)
        out[name] = {
            "sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
            "length": len(raw),
        }
    return out


def record_audit(
    conn: Any,
    *,
    tenant_id: str,
    action: str,
    target_kind: str,
    target_id: int,
    changed_fields: dict[str, Any],
    operator: str,
) -> None:
    """在当前事务内写入一条审计日志（changes 已脱敏）。"""
    conn.execute(
        text(
            """
            INSERT INTO sensitive_content.audit_log
                (tenant_id, action, target_kind, target_id, changes, operator)
            VALUES (:tenant_id, :action, :target_kind, :target_id,
                    CAST(:changes AS JSONB), :operator)
            """
        ),
        {
            "tenant_id": tenant_id,
            "action": action,
            "target_kind": target_kind,
            "target_id": target_id,
            "changes": json.dumps(sanitize_changes(changed_fields), ensure_ascii=False),
            "operator": operator,
        },
    )


def bump_policy_version(conn: Any, tenant_id: str) -> int:
    """在当前事务内递增所属租户的策略版本（全局变更递增 '' 行），返回新版本号。"""
    row = conn.execute(
        text(
            """
            INSERT INTO sensitive_content.policy_version (tenant_id, version, updated_at)
            VALUES (:tenant_id, 1, now())
            ON CONFLICT (tenant_id) DO UPDATE
                SET version = sensitive_content.policy_version.version + 1,
                    updated_at = now()
            RETURNING version
            """
        ),
        {"tenant_id": tenant_id},
    ).first()
    return int(row[0])
