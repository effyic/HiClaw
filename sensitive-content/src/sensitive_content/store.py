"""SQL 访问层（SQLAlchemy core + text() 裸 SQL）。

本模块同时包含两类内容：

1. 纯函数（不依赖 DB，便于单测）：五步覆盖合并算法 `merge_rules`、
   规范化序列化 `canonical_snapshot_json`、`compute_etag`、组合版本 `combined_version`。
2. DB 访问函数：类型/规则 CRUD、快照装载、命中事件写入、统计聚合。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Any, Optional
from uuid import uuid4

from sqlalchemy import text

from sensitive_content.audit import bump_policy_version, record_audit
from sensitive_content.db import db_connection
from sensitive_content.models import (
    ACTION_CONFIG_ALLOWED_KEYS,
    RuleCreate,
    RuleUpdate,
    TypeCreate,
    TypeUpdate,
    ValidationFailure,
    dedup_key,
    validate_action_config,
    validate_pattern,
)

# 全局租户在库内的标识
GLOBAL_TENANT = ""


class StoreError(Exception):
    """业务错误：携带机器可读 code 与建议的 HTTP 状态码。"""

    def __init__(self, status: int, code: str, message: str, extra: dict[str, Any] | None = None):
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra or {}
        super().__init__(message)


def _fetch_one(conn: Any, sql: str, params: dict[str, Any]) -> dict[str, Any] | None:
    row = conn.execute(text(sql), params).mappings().first()
    return dict(row) if row else None


def _fetch_all(conn: Any, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(text(sql), params or {}).mappings()]


def _parse_json(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


# ---------------------------------------------------------------------------
# 纯函数：合并算法 / 规范化 ETag / 组合版本
# ---------------------------------------------------------------------------

def merge_rules(
    global_rules: list[dict[str, Any]],
    tenant_rules: list[dict[str, Any]],
    deleted_global_rule_ids: set[int] | None = None,
) -> list[dict[str, Any]]:
    """五步覆盖合并算法（纯函数）。

    入参约定：
    - global_rules：全局启用且未删除的规则（tenant_id=''）
    - tenant_rules：租户所有未删除的规则，覆盖记录（overrides_global_rule_id 非空）
      无论 enabled 与否都在其中；普通规则也全部传入，由本函数按 enabled 过滤
    - deleted_global_rule_ids：已逻辑删除的全局规则 ID 集合，指向它们的覆盖
      记录为 orphaned，直接跳过（不进快照）

    步骤：
    1. 全局启用规则作为基础集
    2. 加载租户覆盖记录（含 enabled=FALSE），orphaned 覆盖跳过
    3. enabled=FALSE 的覆盖：从基础集剔除对应全局规则（禁用生效）
    4. enabled=TRUE 的覆盖：替换对应全局规则
    5. 其余租户启用规则（无覆盖关系）追加
    最后按规范化 pattern 去重，租户规则优先保留。
    """
    deleted_ids = deleted_global_rule_ids or set()

    # 步骤 1：基础集（保持 id 索引便于剔除/替换）
    base: dict[int, dict[str, Any]] = {int(r["id"]): r for r in global_rules}

    replacements: list[dict[str, Any]] = []
    for rule in tenant_rules:
        override_target = rule.get("overrides_global_rule_id")
        if not override_target:
            continue
        # 步骤 2：orphaned 覆盖（目标全局规则已删除）不参与合并
        if int(override_target) in deleted_ids:
            continue
        # 步骤 3 / 4：无论启用与否都先剔除目标全局规则
        base.pop(int(override_target), None)
        if rule.get("enabled"):
            replacements.append(rule)

    # 步骤 5：无覆盖关系的租户启用规则
    plain = [
        r
        for r in tenant_rules
        if not r.get("overrides_global_rule_id") and r.get("enabled")
    ]

    # 去重：租户规则优先注册去重键，全局规则撞键则丢弃
    seen: set[tuple[str, bool, bool, str]] = set()
    merged: list[dict[str, Any]] = []
    for rule in replacements + plain:
        key = dedup_key(rule)
        if key in seen:
            continue
        seen.add(key)
        merged.append(rule)
    for rule in base.values():
        key = dedup_key(rule)
        if key in seen:
            continue
        seen.add(key)
        merged.append(rule)

    merged.sort(key=lambda r: (str(r.get("tenant_id", "")), int(r["id"])))
    return merged


def canonical_snapshot_json(content: dict[str, Any]) -> str:
    """规范化序列化：rules/types 按 (tenant_id, id) 排序，键排序、紧凑分隔。"""
    normalized = dict(content)
    for key in ("rules", "types"):
        if key in normalized and isinstance(normalized[key], list):
            normalized[key] = sorted(
                normalized[key],
                key=lambda item: (str(item.get("tenant_id", "")), int(item["id"])),
            )
    return json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def compute_etag(content: dict[str, Any]) -> str:
    """对规范化快照内容取 SHA-256 作为强 ETag（与版本号解耦）。"""
    digest = sha256(canonical_snapshot_json(content).encode("utf-8")).hexdigest()
    return f'"{digest}"'


def combined_version(global_version: int, tenant_version: int) -> str:
    """组合版本字符串：global-{全局版本}:tenant-{租户版本}。"""
    return f"global-{global_version}:tenant-{tenant_version}"


# ---------------------------------------------------------------------------
# 类型 CRUD
# ---------------------------------------------------------------------------

_TYPE_COLUMNS = (
    "id, tenant_id, code, name, action, action_config, priority, "
    "description, enabled, deleted, created_at, updated_at"
)


def _type_out(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out["action_config"] = _parse_json(out.get("action_config"))
    return out


def _get_type_row(conn: Any, type_id: int) -> dict[str, Any] | None:
    return _fetch_one(
        conn,
        f"SELECT {_TYPE_COLUMNS} FROM sensitive_content.sensitive_type "
        "WHERE id = :id AND deleted = FALSE",
        {"id": type_id},
    )


def _assert_type_visible(row: dict[str, Any] | None, tenant_id: str) -> dict[str, Any]:
    """类型必须存在且属于全局或本租户，否则 404。"""
    if row is None or row["tenant_id"] not in (GLOBAL_TENANT, tenant_id):
        raise StoreError(404, "type_not_found", "sensitive type not found")
    return row


def _assert_type_mutable(row: dict[str, Any], tenant_id: str) -> None:
    """租户上下文不能修改全局类型（403）。"""
    if row["tenant_id"] == GLOBAL_TENANT and tenant_id != GLOBAL_TENANT:
        raise StoreError(
            403, "forbidden_global_type",
            "tenant context cannot modify a global sensitive type",
        )


def _validate(func: Any, *args: Any) -> None:
    """将入参校验失败映射为 400 业务错误。"""
    try:
        func(*args)
    except ValidationFailure as exc:
        raise StoreError(400, exc.code, exc.message) from exc


def _generate_type_code() -> str:
    """生成类型编码：t_ + 12 位十六进制，满足 1~64 长度约束。"""
    return f"t_{uuid4().hex[:12]}"


def create_type(tenant_id: str, data: TypeCreate, operator: str) -> dict[str, Any]:
    _validate(validate_action_config, data.action, data.action_config)
    explicit_code = (data.code or "").strip() or None
    with db_connection() as conn:
        # 显式 code：冲突直接 409；缺省 code：生成并少量重试
        max_attempts = 1 if explicit_code else 5
        last_code = explicit_code or ""
        row: dict[str, Any] | None = None
        for _ in range(max_attempts):
            code = explicit_code or _generate_type_code()
            last_code = code
            dup = _fetch_one(
                conn,
                "SELECT id FROM sensitive_content.sensitive_type "
                "WHERE tenant_id = :tenant_id AND code = :code AND deleted = FALSE",
                {"tenant_id": tenant_id, "code": code},
            )
            if dup:
                if explicit_code:
                    raise StoreError(409, "duplicate_code", f"type code already exists: {code}")
                continue
            row = _fetch_one(
                conn,
                f"""
                INSERT INTO sensitive_content.sensitive_type
                    (tenant_id, code, name, action, action_config, priority, description, enabled)
                VALUES (:tenant_id, :code, :name, :action, CAST(:action_config AS JSONB),
                        :priority, :description, :enabled)
                RETURNING {_TYPE_COLUMNS}
                """,
                {
                    "tenant_id": tenant_id,
                    "code": code,
                    "name": data.name,
                    "action": data.action.value,
                    "action_config": json.dumps(data.action_config, ensure_ascii=False),
                    "priority": data.priority,
                    "description": data.description,
                    "enabled": data.enabled,
                },
            )
            break
        if row is None:
            raise StoreError(
                409,
                "duplicate_code",
                f"failed to allocate unique type code after retries: {last_code}",
            )
        record_audit(
            conn,
            tenant_id=tenant_id,
            action="create",
            target_kind="type",
            target_id=int(row["id"]),
            changed_fields={
                "code": row["code"],
                "name": data.name,
                "action": data.action.value,
                "action_config": data.action_config,
                "priority": data.priority,
                "description": data.description,
            },
            operator=operator,
        )
        bump_policy_version(conn, tenant_id)
        return _type_out(row)


def list_types(
    tenant_id: str,
    *,
    enabled: bool | None = None,
    include_global: bool = True,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[dict[str, Any]], int]:
    conds = ["deleted = FALSE"]
    params: dict[str, Any] = {"tenant_id": tenant_id}
    if include_global and tenant_id != GLOBAL_TENANT:
        conds.append("tenant_id IN ('', :tenant_id)")
    else:
        conds.append("tenant_id = :tenant_id")
    if enabled is not None:
        conds.append("enabled = :enabled")
        params["enabled"] = enabled
    where = " AND ".join(conds)
    with db_connection() as conn:
        total_row = _fetch_one(
            conn,
            f"SELECT COUNT(*) AS n FROM sensitive_content.sensitive_type WHERE {where}",
            params,
        )
        rows = _fetch_all(
            conn,
            f"""
            SELECT {_TYPE_COLUMNS} FROM sensitive_content.sensitive_type
            WHERE {where}
            ORDER BY tenant_id, priority DESC, id
            LIMIT :limit OFFSET :offset
            """,
            {**params, "limit": page_size, "offset": (page - 1) * page_size},
        )
    return [_type_out(r) for r in rows], int(total_row["n"] if total_row else 0)


def get_type(tenant_id: str, type_id: int) -> dict[str, Any]:
    with db_connection() as conn:
        row = _assert_type_visible(_get_type_row(conn, type_id), tenant_id)
    return _type_out(row)


def update_type(tenant_id: str, type_id: int, data: TypeUpdate, operator: str) -> dict[str, Any]:
    fields = data.model_dump(exclude_unset=True, exclude_none=True)
    if "action" in fields:
        fields["action"] = data.action.value  # type: ignore[union-attr]
    with db_connection() as conn:
        row = _assert_type_visible(_get_type_row(conn, type_id), tenant_id)
        _assert_type_mutable(row, tenant_id)
        if not fields:
            return _type_out(row)
        # 合并当前行与本次补丁后再校验最终 action + action_config
        final_action = fields.get("action", row["action"])
        final_config = (
            fields["action_config"]
            if "action_config" in fields
            else _parse_json(row.get("action_config"))
        )
        _validate(validate_action_config, final_action, final_config)
        sets = ", ".join(
            f"action_config = CAST(:{k} AS JSONB)" if k == "action_config" else f"{k} = :{k}"
            for k in fields
        )
        params: dict[str, Any] = {
            k: (json.dumps(v, ensure_ascii=False) if k == "action_config" else v)
            for k, v in fields.items()
        }
        params["id"] = type_id
        updated = _fetch_one(
            conn,
            f"""
            UPDATE sensitive_content.sensitive_type
            SET {sets}, updated_at = now()
            WHERE id = :id
            RETURNING {_TYPE_COLUMNS}
            """,
            params,
        )
        assert updated is not None
        record_audit(
            conn,
            tenant_id=row["tenant_id"],
            action="update",
            target_kind="type",
            target_id=type_id,
            changed_fields=fields,
            operator=operator,
        )
        bump_policy_version(conn, row["tenant_id"])
        return _type_out(updated)


def set_type_enabled(tenant_id: str, type_id: int, enabled: bool, operator: str) -> dict[str, Any]:
    with db_connection() as conn:
        row = _assert_type_visible(_get_type_row(conn, type_id), tenant_id)
        _assert_type_mutable(row, tenant_id)
        updated = _fetch_one(
            conn,
            f"""
            UPDATE sensitive_content.sensitive_type
            SET enabled = :enabled, updated_at = now()
            WHERE id = :id
            RETURNING {_TYPE_COLUMNS}
            """,
            {"id": type_id, "enabled": enabled},
        )
        assert updated is not None
        record_audit(
            conn,
            tenant_id=row["tenant_id"],
            action="enable" if enabled else "disable",
            target_kind="type",
            target_id=type_id,
            changed_fields={"enabled": enabled},
            operator=operator,
        )
        bump_policy_version(conn, row["tenant_id"])
        return _type_out(updated)


def delete_type(tenant_id: str, type_id: int, operator: str) -> None:
    with db_connection() as conn:
        row = _assert_type_visible(_get_type_row(conn, type_id), tenant_id)
        _assert_type_mutable(row, tenant_id)
        # 跨租户删除约束：仍被任意租户的未删除规则引用时不可删除
        ref = _fetch_one(
            conn,
            "SELECT COUNT(*) AS n FROM sensitive_content.sensitive_rule "
            "WHERE type_id = :type_id AND deleted = FALSE",
            {"type_id": type_id},
        )
        ref_count = int(ref["n"] if ref else 0)
        if ref_count > 0:
            raise StoreError(
                409, "type_in_use",
                f"type is referenced by {ref_count} active rule(s)",
                extra={"references": ref_count},
            )
        conn.execute(
            text(
                "UPDATE sensitive_content.sensitive_type "
                "SET deleted = TRUE, updated_at = now() WHERE id = :id"
            ),
            {"id": type_id},
        )
        record_audit(
            conn,
            tenant_id=row["tenant_id"],
            action="delete",
            target_kind="type",
            target_id=type_id,
            changed_fields={"deleted": True},
            operator=operator,
        )
        bump_policy_version(conn, row["tenant_id"])


# ---------------------------------------------------------------------------
# 规则 CRUD
# ---------------------------------------------------------------------------

_RULE_COLUMNS = (
    "r.id, r.tenant_id, r.type_id, r.pattern, r.match_mode, r.case_sensitive, "
    "r.normalize, r.overrides_global_rule_id, r.description, r.priority, r.remark, "
    "r.enabled, r.deleted, r.created_at, r.updated_at"
)

# effective_status 标注：覆盖目标已删除则为 orphaned
_RULE_SELECT = f"""
    SELECT {_RULE_COLUMNS},
           CASE
               WHEN r.overrides_global_rule_id IS NOT NULL AND g.id IS NULL
                   THEN 'orphaned'
               ELSE 'active'
           END AS effective_status
    FROM sensitive_content.sensitive_rule r
    LEFT JOIN sensitive_content.sensitive_rule g
        ON g.id = r.overrides_global_rule_id AND g.deleted = FALSE
"""


def _get_rule_row(conn: Any, rule_id: int) -> dict[str, Any] | None:
    return _fetch_one(
        conn,
        f"{_RULE_SELECT} WHERE r.id = :id AND r.deleted = FALSE",
        {"id": rule_id},
    )


def _assert_rule_scoped(row: dict[str, Any] | None, tenant_id: str) -> dict[str, Any]:
    """规则必须属于路径租户；租户上下文碰到全局规则时返回 403。"""
    if row is None:
        raise StoreError(404, "rule_not_found", "sensitive rule not found")
    if row["tenant_id"] != tenant_id:
        if row["tenant_id"] == GLOBAL_TENANT:
            raise StoreError(
                403, "forbidden_global_rule",
                "tenant context cannot modify a global sensitive rule",
            )
        raise StoreError(404, "rule_not_found", "sensitive rule not found")
    return row


def _validate_rule_refs(
    conn: Any,
    tenant_id: str,
    type_id: int,
    overrides_global_rule_id: Optional[int],
) -> None:
    """type_id 归属校验 + 覆盖目标校验。"""
    type_row = _get_type_row(conn, type_id)
    # 类型须为全局类型或本租户类型；其他租户的类型视为不可见（404）
    if type_row is None or type_row["tenant_id"] not in (GLOBAL_TENANT, tenant_id):
        raise StoreError(404, "type_not_found", f"type not found: {type_id}")
    if overrides_global_rule_id is not None:
        if tenant_id == GLOBAL_TENANT:
            raise StoreError(
                400, "invalid_override",
                "global rules cannot set overrides_global_rule_id",
            )
        target = _fetch_one(
            conn,
            "SELECT id, tenant_id, deleted FROM sensitive_content.sensitive_rule "
            "WHERE id = :id",
            {"id": overrides_global_rule_id},
        )
        # 覆盖目标必须指向存在且未删除的全局规则
        if target is None or target["deleted"] or target["tenant_id"] != GLOBAL_TENANT:
            raise StoreError(
                400, "invalid_override_target",
                "overrides_global_rule_id must reference an existing, non-deleted global rule",
            )


def _check_duplicate_rule(
    conn: Any,
    tenant_id: str,
    *,
    pattern: str,
    match_mode: str,
    case_sensitive: bool,
    normalize: bool,
    exclude_id: int | None = None,
) -> None:
    """同租户内完全重复规则（规范化后相等）直接拒绝。"""
    candidate = {
        "pattern": pattern,
        "match_mode": match_mode,
        "case_sensitive": case_sensitive,
        "normalize": normalize,
    }
    key = dedup_key(candidate)
    rows = _fetch_all(
        conn,
        """
        SELECT id, pattern, match_mode, case_sensitive, normalize
        FROM sensitive_content.sensitive_rule
        WHERE tenant_id = :tenant_id AND deleted = FALSE
          AND match_mode = :match_mode
          AND case_sensitive = :case_sensitive
          AND normalize = :normalize
        """,
        {
            "tenant_id": tenant_id,
            "match_mode": match_mode,
            "case_sensitive": case_sensitive,
            "normalize": normalize,
        },
    )
    for row in rows:
        if exclude_id is not None and int(row["id"]) == exclude_id:
            continue
        if dedup_key(row) == key:
            raise StoreError(
                409, "duplicate_rule",
                f"an equivalent rule already exists in this tenant (id={row['id']})",
            )


def create_rule(tenant_id: str, data: RuleCreate, operator: str) -> dict[str, Any]:
    try:
        validate_pattern(data.pattern, data.match_mode.value)
    except ValidationFailure as exc:
        raise StoreError(400, exc.code, exc.message) from exc
    with db_connection() as conn:
        _validate_rule_refs(conn, tenant_id, data.type_id, data.overrides_global_rule_id)
        _check_duplicate_rule(
            conn,
            tenant_id,
            pattern=data.pattern,
            match_mode=data.match_mode.value,
            case_sensitive=data.case_sensitive,
            normalize=data.normalize,
        )
        row = _fetch_one(
            conn,
            f"""
            INSERT INTO sensitive_content.sensitive_rule
                (tenant_id, type_id, pattern, match_mode, case_sensitive, normalize,
                 overrides_global_rule_id, description, priority, remark, enabled)
            VALUES (:tenant_id, :type_id, :pattern, :match_mode, :case_sensitive,
                    :normalize, :overrides, :description, :priority, :remark, :enabled)
            RETURNING {_RULE_COLUMNS.replace('r.', '')}, 'active' AS effective_status
            """,
            {
                "tenant_id": tenant_id,
                "type_id": data.type_id,
                "pattern": data.pattern,
                "match_mode": data.match_mode.value,
                "case_sensitive": data.case_sensitive,
                "normalize": data.normalize,
                "overrides": data.overrides_global_rule_id,
                "description": data.description,
                "priority": data.priority,
                "remark": data.remark,
                "enabled": data.enabled,
            },
        )
        assert row is not None
        record_audit(
            conn,
            tenant_id=tenant_id,
            action="create",
            target_kind="rule",
            target_id=int(row["id"]),
            changed_fields={
                "pattern": data.pattern,
                "match_mode": data.match_mode.value,
                "type_id": data.type_id,
                "case_sensitive": data.case_sensitive,
                "normalize": data.normalize,
                "overrides_global_rule_id": data.overrides_global_rule_id,
                "priority": data.priority,
            },
            operator=operator,
        )
        bump_policy_version(conn, tenant_id)
        return dict(row)


def list_rules(
    tenant_id: str,
    *,
    keyword: str | None = None,
    type_id: int | None = None,
    enabled: bool | None = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[dict[str, Any]], int]:
    conds = ["r.tenant_id = :tenant_id", "r.deleted = FALSE"]
    params: dict[str, Any] = {"tenant_id": tenant_id}
    if keyword:
        conds.append("(r.pattern ILIKE :kw OR r.description ILIKE :kw OR r.remark ILIKE :kw)")
        params["kw"] = f"%{keyword}%"
    if type_id is not None:
        conds.append("r.type_id = :type_id")
        params["type_id"] = type_id
    if enabled is not None:
        conds.append("r.enabled = :enabled")
        params["enabled"] = enabled
    where = " AND ".join(conds)
    with db_connection() as conn:
        total_row = _fetch_one(
            conn,
            f"SELECT COUNT(*) AS n FROM sensitive_content.sensitive_rule r WHERE {where}",
            params,
        )
        rows = _fetch_all(
            conn,
            f"""
            {_RULE_SELECT}
            WHERE {where}
            ORDER BY r.priority DESC, r.id
            LIMIT :limit OFFSET :offset
            """,
            {**params, "limit": page_size, "offset": (page - 1) * page_size},
        )
    return rows, int(total_row["n"] if total_row else 0)


def get_rule(tenant_id: str, rule_id: int) -> dict[str, Any]:
    with db_connection() as conn:
        row = _get_rule_row(conn, rule_id)
        if row is None or row["tenant_id"] != tenant_id:
            raise StoreError(404, "rule_not_found", "sensitive rule not found")
    return row


def update_rule(tenant_id: str, rule_id: int, data: RuleUpdate, operator: str) -> dict[str, Any]:
    fields = data.model_dump(exclude_unset=True)
    with db_connection() as conn:
        row = _assert_rule_scoped(_get_rule_row(conn, rule_id), tenant_id)
        merged = {**row, **fields}
        if "match_mode" in fields and fields["match_mode"] is not None:
            merged["match_mode"] = data.match_mode.value  # type: ignore[union-attr]
            fields["match_mode"] = merged["match_mode"]
        try:
            validate_pattern(str(merged["pattern"]), str(merged["match_mode"]))
        except ValidationFailure as exc:
            raise StoreError(400, exc.code, exc.message) from exc
        _validate_rule_refs(
            conn,
            tenant_id,
            int(merged["type_id"]),
            merged.get("overrides_global_rule_id"),
        )
        _check_duplicate_rule(
            conn,
            tenant_id,
            pattern=str(merged["pattern"]),
            match_mode=str(merged["match_mode"]),
            case_sensitive=bool(merged["case_sensitive"]),
            normalize=bool(merged["normalize"]),
            exclude_id=rule_id,
        )
        if not fields:
            return row
        col_map = {"overrides_global_rule_id": "overrides_global_rule_id"}
        sets = ", ".join(f"{col_map.get(k, k)} = :{k}" for k in fields)
        params: dict[str, Any] = dict(fields)
        params["id"] = rule_id
        conn.execute(
            text(
                f"UPDATE sensitive_content.sensitive_rule "
                f"SET {sets}, updated_at = now() WHERE id = :id"
            ),
            params,
        )
        record_audit(
            conn,
            tenant_id=tenant_id,
            action="update",
            target_kind="rule",
            target_id=rule_id,
            changed_fields=fields,
            operator=operator,
        )
        bump_policy_version(conn, tenant_id)
        updated = _get_rule_row(conn, rule_id)
        assert updated is not None
        return updated


def set_rule_enabled(tenant_id: str, rule_id: int, enabled: bool, operator: str) -> dict[str, Any]:
    with db_connection() as conn:
        _assert_rule_scoped(_get_rule_row(conn, rule_id), tenant_id)
        conn.execute(
            text(
                "UPDATE sensitive_content.sensitive_rule "
                "SET enabled = :enabled, updated_at = now() WHERE id = :id"
            ),
            {"id": rule_id, "enabled": enabled},
        )
        record_audit(
            conn,
            tenant_id=tenant_id,
            action="enable" if enabled else "disable",
            target_kind="rule",
            target_id=rule_id,
            changed_fields={"enabled": enabled},
            operator=operator,
        )
        bump_policy_version(conn, tenant_id)
        updated = _get_rule_row(conn, rule_id)
        assert updated is not None
        return updated


def delete_rule(tenant_id: str, rule_id: int, operator: str) -> None:
    with db_connection() as conn:
        _assert_rule_scoped(_get_rule_row(conn, rule_id), tenant_id)
        conn.execute(
            text(
                "UPDATE sensitive_content.sensitive_rule "
                "SET deleted = TRUE, updated_at = now() WHERE id = :id"
            ),
            {"id": rule_id},
        )
        record_audit(
            conn,
            tenant_id=tenant_id,
            action="delete",
            target_kind="rule",
            target_id=rule_id,
            changed_fields={"deleted": True},
            operator=operator,
        )
        bump_policy_version(conn, tenant_id)


# ---------------------------------------------------------------------------
# 审计查询
# ---------------------------------------------------------------------------

def list_audit_logs(
    tenant_id: str,
    *,
    target_kind: str | None = None,
    target_id: int | None = None,
    action: str | None = None,
    time_from: datetime | None = None,
    time_to: datetime | None = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[dict[str, Any]], int]:
    conds = ["tenant_id = :tenant_id"]
    params: dict[str, Any] = {"tenant_id": tenant_id}
    if target_kind:
        conds.append("target_kind = :target_kind")
        params["target_kind"] = target_kind
    if target_id is not None:
        conds.append("target_id = :target_id")
        params["target_id"] = target_id
    if action:
        conds.append("action = :action")
        params["action"] = action
    if time_from is not None:
        conds.append("created_at >= :time_from")
        params["time_from"] = time_from
    if time_to is not None:
        conds.append("created_at <= :time_to")
        params["time_to"] = time_to
    where = " AND ".join(conds)
    with db_connection() as conn:
        total_row = _fetch_one(
            conn,
            f"SELECT COUNT(*) AS n FROM sensitive_content.audit_log WHERE {where}",
            params,
        )
        rows = _fetch_all(
            conn,
            f"""
            SELECT id, tenant_id, action, target_kind, target_id, changes, operator, created_at
            FROM sensitive_content.audit_log
            WHERE {where}
            ORDER BY id DESC
            LIMIT :limit OFFSET :offset
            """,
            {**params, "limit": page_size, "offset": (page - 1) * page_size},
        )
    for row in rows:
        row["changes"] = _parse_json(row.get("changes"))
    return rows, int(total_row["n"] if total_row else 0)


# ---------------------------------------------------------------------------
# 策略快照
# ---------------------------------------------------------------------------

_SNAPSHOT_RULE_FIELDS = (
    "id", "tenant_id", "type_id", "pattern", "match_mode",
    "case_sensitive", "normalize", "priority",
)


def _snapshot_rule(row: dict[str, Any]) -> dict[str, Any]:
    return {k: row[k] for k in _SNAPSHOT_RULE_FIELDS}


def load_policy_snapshot(tenant_id: str) -> dict[str, Any]:
    """装载合并后的策略快照：{"version", "etag", "types", "rules"}。"""
    with db_connection() as conn:
        # 全局启用规则（且类型有效）作为基础集
        global_rules = _fetch_all(
            conn,
            """
            SELECT r.id, r.tenant_id, r.type_id, r.pattern, r.match_mode,
                   r.case_sensitive, r.normalize, r.priority, r.enabled,
                   r.overrides_global_rule_id
            FROM sensitive_content.sensitive_rule r
            JOIN sensitive_content.sensitive_type t
                ON t.id = r.type_id AND t.deleted = FALSE AND t.enabled = TRUE
            WHERE r.tenant_id = '' AND r.enabled = TRUE AND r.deleted = FALSE
            """,
        )
        tenant_rules: list[dict[str, Any]] = []
        deleted_global_ids: set[int] = set()
        if tenant_id != GLOBAL_TENANT:
            # 租户所有未删除规则；type_live 标记类型是否有效
            tenant_rules = _fetch_all(
                conn,
                """
                SELECT r.id, r.tenant_id, r.type_id, r.pattern, r.match_mode,
                       r.case_sensitive, r.normalize, r.priority, r.enabled,
                       r.overrides_global_rule_id,
                       (t.id IS NOT NULL) AS type_live
                FROM sensitive_content.sensitive_rule r
                LEFT JOIN sensitive_content.sensitive_type t
                    ON t.id = r.type_id AND t.deleted = FALSE AND t.enabled = TRUE
                WHERE r.tenant_id = :tenant_id AND r.deleted = FALSE
                """,
                {"tenant_id": tenant_id},
            )
            # 覆盖目标中已删除的全局规则 ID（orphaned 判定）
            rows = _fetch_all(
                conn,
                """
                SELECT DISTINCT g.id
                FROM sensitive_content.sensitive_rule r
                JOIN sensitive_content.sensitive_rule g
                    ON g.id = r.overrides_global_rule_id
                WHERE r.tenant_id = :tenant_id AND r.deleted = FALSE
                  AND g.deleted = TRUE
                """,
                {"tenant_id": tenant_id},
            )
            deleted_global_ids = {int(row["id"]) for row in rows}
        types = _fetch_all(
            conn,
            """
            SELECT id, tenant_id, code, name, action, action_config, priority
            FROM sensitive_content.sensitive_type
            WHERE deleted = FALSE AND enabled = TRUE
              AND tenant_id IN ('', :tenant_id)
            """,
            {"tenant_id": tenant_id},
        )
        versions = _fetch_all(
            conn,
            "SELECT tenant_id, version FROM sensitive_content.policy_version "
            "WHERE tenant_id IN ('', :tenant_id)",
            {"tenant_id": tenant_id},
        )

    # 类型无效（禁用/删除）的租户规则：启用状态视为 FALSE（覆盖仍然剔除目标）
    for rule in tenant_rules:
        if not rule.pop("type_live", True):
            rule["enabled"] = False

    merged = merge_rules(global_rules, tenant_rules, deleted_global_ids)
    version_map = {str(v["tenant_id"]): int(v["version"]) for v in versions}
    content = {
        "tenant_id": tenant_id,
        "types": [{**t, "action_config": _parse_json(t.get("action_config"))} for t in types],
        "rules": [_snapshot_rule(r) for r in merged],
    }
    return {
        "version": combined_version(
            version_map.get(GLOBAL_TENANT, 0), version_map.get(tenant_id, 0)
        ),
        "etag": compute_etag(content),
        **content,
    }


# ---------------------------------------------------------------------------
# 命中事件
# ---------------------------------------------------------------------------

def insert_hit_events(events: list[dict[str, Any]]) -> tuple[int, int]:
    """批量写入命中事件；event_id ON CONFLICT DO NOTHING 幂等。

    返回 (accepted, duplicates)。
    """
    accepted = 0
    with db_connection() as conn:
        for event in events:
            result = conn.execute(
                text(
                    """
                    INSERT INTO sensitive_content.hit_event
                        (event_id, rule_id, type_id, rule_action, final_action,
                         selected, final_rule_id, tenant_id, request_fingerprint,
                         session_fingerprint, session_id, policy_version, hit_count, hit_at)
                    VALUES (:event_id, :rule_id, :type_id, :rule_action, :final_action,
                            :selected, :final_rule_id, :tenant_id, :request_fingerprint,
                            :session_fingerprint, :session_id, :policy_version, :hit_count,
                            COALESCE(:hit_at, now()))
                    ON CONFLICT (event_id) DO NOTHING
                    """
                ),
                event,
            )
            accepted += int(result.rowcount or 0)
    return accepted, len(events) - accepted


def list_hit_events(
    tenant_id: str | None,
    *,
    rule_id: int | None = None,
    type_id: int | None = None,
    session_id: str | None = None,
    time_from: datetime | None = None,
    time_to: datetime | None = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[dict[str, Any]], int]:
    """命中事件明细分页查询（tenant_id=None 表示跨租户）。

    响应行携带明文 session_id，供管理端跳转查看对应会话记录。
    """
    conds = ["1 = 1"]
    params: dict[str, Any] = {}
    if tenant_id is not None:
        conds.append("tenant_id = :tenant_id")
        params["tenant_id"] = tenant_id
    if rule_id is not None:
        conds.append("rule_id = :rule_id")
        params["rule_id"] = rule_id
    if type_id is not None:
        conds.append("type_id = :type_id")
        params["type_id"] = type_id
    if session_id is not None:
        conds.append("session_id = :session_id")
        params["session_id"] = session_id
    if time_from is not None:
        conds.append("hit_at >= :time_from")
        params["time_from"] = time_from
    if time_to is not None:
        conds.append("hit_at <= :time_to")
        params["time_to"] = time_to
    where = " AND ".join(conds)
    with db_connection() as conn:
        total_row = _fetch_one(
            conn,
            f"SELECT COUNT(*) AS n FROM sensitive_content.hit_event WHERE {where}",
            params,
        )
        rows = _fetch_all(
            conn,
            f"""
            SELECT event_id, rule_id, type_id, rule_action, final_action,
                   selected, final_rule_id, tenant_id, session_id,
                   policy_version, hit_count, hit_at
            FROM sensitive_content.hit_event
            WHERE {where}
            ORDER BY hit_at DESC, event_id
            LIMIT :limit OFFSET :offset
            """,
            {**params, "limit": page_size, "offset": (page - 1) * page_size},
        )
    for row in rows:
        row["event_id"] = str(row["event_id"])
    return rows, int(total_row["n"] if total_row else 0)


def purge_expired_hit_events(retention_days: int) -> int:
    """清理超出保留期的命中事件，返回删除行数。"""
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    with db_connection() as conn:
        result = conn.execute(
            text("DELETE FROM sensitive_content.hit_event WHERE hit_at < :cutoff"),
            {"cutoff": cutoff},
        )
    return int(result.rowcount or 0)


# ---------------------------------------------------------------------------
# 统计查询（tenant_id=None 表示跨租户聚合）
# ---------------------------------------------------------------------------

def _metric_where(
    tenant_id: str | None,
    time_from: datetime | None,
    time_to: datetime | None,
) -> tuple[str, dict[str, Any]]:
    conds = ["1 = 1"]
    params: dict[str, Any] = {}
    if tenant_id is not None:
        conds.append("tenant_id = :tenant_id")
        params["tenant_id"] = tenant_id
    if time_from is not None:
        conds.append("hit_at >= :time_from")
        params["time_from"] = time_from
    if time_to is not None:
        conds.append("hit_at <= :time_to")
        params["time_to"] = time_to
    return " AND ".join(conds), params


def metrics_summary(
    tenant_id: str | None,
    time_from: datetime | None = None,
    time_to: datetime | None = None,
) -> dict[str, Any]:
    where, params = _metric_where(tenant_id, time_from, time_to)
    with db_connection() as conn:
        totals = _fetch_one(
            conn,
            f"""
            SELECT COUNT(*) AS total_events,
                   COALESCE(SUM(hit_count), 0) AS total_hits,
                   COUNT(DISTINCT request_fingerprint) AS hit_requests
            FROM sensitive_content.hit_event WHERE {where}
            """,
            params,
        )
        finals = _fetch_all(
            conn,
            f"""
            SELECT final_action, COUNT(*) AS requests
            FROM sensitive_content.hit_event
            WHERE {where} AND selected = TRUE
            GROUP BY final_action ORDER BY requests DESC
            """,
            params,
        )
    assert totals is not None
    return {
        "total_events": int(totals["total_events"]),
        "total_hits": int(totals["total_hits"]),
        "hit_requests": int(totals["hit_requests"]),
        "final_actions": {str(f["final_action"]): int(f["requests"]) for f in finals},
    }


def metrics_by_rule(
    tenant_id: str | None,
    time_from: datetime | None = None,
    time_to: datetime | None = None,
    top: int = 10,
) -> list[dict[str, Any]]:
    where, params = _metric_where(tenant_id, time_from, time_to)
    with db_connection() as conn:
        rows = _fetch_all(
            conn,
            f"""
            SELECT rule_id, type_id,
                   COUNT(*) AS events,
                   COALESCE(SUM(hit_count), 0) AS hits,
                   MAX(hit_at) AS last_hit_at,
                   -- 最近一次命中的会话标识（示例入口，供跳转会话详情）
                   (array_agg(session_id ORDER BY hit_at DESC)
                        FILTER (WHERE session_id <> ''))[1] AS last_session_id
            FROM sensitive_content.hit_event WHERE {where}
            GROUP BY rule_id, type_id
            ORDER BY hits DESC, rule_id
            LIMIT :top
            """,
            {**params, "top": top},
        )
    return rows


def metrics_by_type(
    tenant_id: str | None,
    time_from: datetime | None = None,
    time_to: datetime | None = None,
    top: int = 10,
) -> list[dict[str, Any]]:
    where, params = _metric_where(tenant_id, time_from, time_to)
    with db_connection() as conn:
        rows = _fetch_all(
            conn,
            f"""
            SELECT type_id,
                   COUNT(*) AS events,
                   COALESCE(SUM(hit_count), 0) AS hits,
                   MAX(hit_at) AS last_hit_at
            FROM sensitive_content.hit_event WHERE {where}
            GROUP BY type_id
            ORDER BY hits DESC, type_id
            LIMIT :top
            """,
            {**params, "top": top},
        )
    return rows


def metrics_by_action(
    tenant_id: str | None,
    time_from: datetime | None = None,
    time_to: datetime | None = None,
) -> dict[str, Any]:
    """双维度统计：rule_action（配置行为的命中次数）与 final_action（实际执行的请求数）。"""
    where, params = _metric_where(tenant_id, time_from, time_to)
    with db_connection() as conn:
        by_rule_action = _fetch_all(
            conn,
            f"""
            SELECT rule_action,
                   COUNT(*) AS events,
                   COALESCE(SUM(hit_count), 0) AS hits
            FROM sensitive_content.hit_event WHERE {where}
            GROUP BY rule_action ORDER BY hits DESC
            """,
            params,
        )
        by_final_action = _fetch_all(
            conn,
            f"""
            SELECT final_action,
                   COUNT(DISTINCT request_fingerprint) AS requests
            FROM sensitive_content.hit_event
            WHERE {where} AND selected = TRUE
            GROUP BY final_action ORDER BY requests DESC
            """,
            params,
        )
    return {
        "by_rule_action": by_rule_action,
        "by_final_action": by_final_action,
    }


def metrics_trend(
    tenant_id: str | None,
    time_from: datetime | None = None,
    time_to: datetime | None = None,
    granularity: str = "day",
) -> list[dict[str, Any]]:
    if granularity not in ("hour", "day", "week", "month"):
        raise StoreError(400, "invalid_granularity", f"unsupported granularity: {granularity}")
    where, params = _metric_where(tenant_id, time_from, time_to)
    with db_connection() as conn:
        rows = _fetch_all(
            conn,
            f"""
            SELECT date_trunc('{granularity}', hit_at) AS bucket,
                   COUNT(*) AS events,
                   COALESCE(SUM(hit_count), 0) AS hits,
                   COUNT(DISTINCT request_fingerprint) AS requests
            FROM sensitive_content.hit_event WHERE {where}
            GROUP BY bucket ORDER BY bucket
            """,
            params,
        )
    return rows
