-- ============================================================
-- sensitive-content 初始迁移：7 张表（6 业务表 + schema_migration）
-- 全部位于独立 sensitive_content schema 内。
-- 本文件是数据库结构的唯一来源，由 `sensitive-content migrate` 执行。
-- ============================================================

CREATE SCHEMA IF NOT EXISTS sensitive_content;

-- 迁移版本记录表（migrate 子命令 bootstrap 时也会确保其存在）
CREATE TABLE IF NOT EXISTS sensitive_content.schema_migration (
    version    INT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 行为目录表：7 种响应行为的种子数据（只读参考，供管理端展示与校验）
CREATE TABLE IF NOT EXISTS sensitive_content.sensitive_action (
    action      TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT ''
);

INSERT INTO sensitive_content.sensitive_action (action, name, description) VALUES
    ('END_CONVERSATION',    '结束对话',   '返回终止话术并结束当前请求'),
    ('FIXED_REPLY',         '固定回复',   '返回预设固定文案，不调用 LLM'),
    ('BLOCK_REQUEST',       '阻断请求',   '返回 422 sensitive_content_blocked'),
    ('REDACT_AND_CONTINUE', '脱敏放行',   '替换命中区间后继续对话'),
    ('LOG_ONLY',            '仅记录',     '记录命中事件后放行'),
    ('BUSINESS_ACTION',     '业务动作',   '调用 Worker 内预注册的白名单处理器'),
    ('CUSTOM_RESPONSE',     '自定义响应', '返回租户配置的自定义响应文案')
ON CONFLICT (action) DO NOTHING;

-- 敏感内容类型表（tenant_id = '' 表示全局）
CREATE TABLE IF NOT EXISTS sensitive_content.sensitive_type (
    id            BIGSERIAL PRIMARY KEY,
    tenant_id     TEXT NOT NULL DEFAULT '',
    code          TEXT NOT NULL,
    name          TEXT NOT NULL,
    action        TEXT NOT NULL REFERENCES sensitive_content.sensitive_action(action),
    action_config JSONB NOT NULL DEFAULT '{}'::jsonb,
    priority      INT NOT NULL DEFAULT 0,
    description   TEXT NOT NULL DEFAULT '',
    enabled       BOOLEAN NOT NULL DEFAULT TRUE,
    deleted       BOOLEAN NOT NULL DEFAULT FALSE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 唯一约束只作用于未删除行，允许逻辑删除后复用 code
CREATE UNIQUE INDEX IF NOT EXISTS uq_sensitive_type_tenant_code
    ON sensitive_content.sensitive_type (tenant_id, code)
    WHERE deleted = FALSE;

-- 敏感内容规则表
CREATE TABLE IF NOT EXISTS sensitive_content.sensitive_rule (
    id                       BIGSERIAL PRIMARY KEY,
    tenant_id                TEXT NOT NULL DEFAULT '',
    type_id                  BIGINT NOT NULL REFERENCES sensitive_content.sensitive_type(id),
    pattern                  TEXT NOT NULL,
    match_mode               TEXT NOT NULL DEFAULT 'text' CHECK (match_mode IN ('text', 'regex')),
    case_sensitive           BOOLEAN NOT NULL DEFAULT FALSE,
    normalize                BOOLEAN NOT NULL DEFAULT TRUE,
    overrides_global_rule_id BIGINT NULL REFERENCES sensitive_content.sensitive_rule(id),
    description              TEXT NOT NULL DEFAULT '',
    priority                 INT NOT NULL DEFAULT 0,
    remark                   TEXT NOT NULL DEFAULT '',
    enabled                  BOOLEAN NOT NULL DEFAULT TRUE,
    deleted                  BOOLEAN NOT NULL DEFAULT FALSE,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_sensitive_rule_tenant
    ON sensitive_content.sensitive_rule (tenant_id) WHERE deleted = FALSE;
CREATE INDEX IF NOT EXISTS idx_sensitive_rule_type
    ON sensitive_content.sensitive_rule (type_id) WHERE deleted = FALSE;
CREATE INDEX IF NOT EXISTS idx_sensitive_rule_overrides
    ON sensitive_content.sensitive_rule (overrides_global_rule_id)
    WHERE overrides_global_rule_id IS NOT NULL;

-- 策略版本表：规则/类型任何变更在同一事务内递增所属 tenant_id 行
CREATE TABLE IF NOT EXISTS sensitive_content.policy_version (
    tenant_id  TEXT PRIMARY KEY,
    version    BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 全局版本行（tenant_id = ''）
INSERT INTO sensitive_content.policy_version (tenant_id, version)
    VALUES ('', 0)
    ON CONFLICT (tenant_id) DO NOTHING;

-- 命中事件表：不含用户原文与规则明文，event_id 幂等
CREATE TABLE IF NOT EXISTS sensitive_content.hit_event (
    event_id            UUID PRIMARY KEY,
    rule_id             BIGINT NOT NULL,
    type_id             BIGINT NOT NULL,
    rule_action         TEXT NOT NULL,
    final_action        TEXT NOT NULL,
    selected            BOOLEAN NOT NULL DEFAULT FALSE,
    final_rule_id       BIGINT NOT NULL,
    tenant_id           TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    session_fingerprint TEXT NOT NULL DEFAULT '',
    policy_version      TEXT NOT NULL DEFAULT '',
    hit_count           INT NOT NULL DEFAULT 1,
    hit_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 统计查询路径的 5 个索引
CREATE INDEX IF NOT EXISTS idx_hit_event_tenant_hit_at
    ON sensitive_content.hit_event (tenant_id, hit_at);
CREATE INDEX IF NOT EXISTS idx_hit_event_rule_hit_at
    ON sensitive_content.hit_event (rule_id, hit_at);
CREATE INDEX IF NOT EXISTS idx_hit_event_type_hit_at
    ON sensitive_content.hit_event (type_id, hit_at);
CREATE INDEX IF NOT EXISTS idx_hit_event_rule_action_hit_at
    ON sensitive_content.hit_event (rule_action, hit_at);
CREATE INDEX IF NOT EXISTS idx_hit_event_final_action_hit_at
    ON sensitive_content.hit_event (final_action, hit_at);

-- 审计日志表：changes 只存字段名 + 值哈希/长度元数据，不存明文
CREATE TABLE IF NOT EXISTS sensitive_content.audit_log (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   TEXT NOT NULL DEFAULT '',
    action      TEXT NOT NULL CHECK (action IN ('create', 'update', 'delete', 'enable', 'disable')),
    target_kind TEXT NOT NULL CHECK (target_kind IN ('rule', 'type')),
    target_id   BIGINT NOT NULL,
    changes     JSONB NOT NULL DEFAULT '{}'::jsonb,
    operator    TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_audit_log_tenant_created
    ON sensitive_content.audit_log (tenant_id, created_at);
CREATE INDEX IF NOT EXISTS idx_audit_log_target
    ON sensitive_content.audit_log (target_kind, target_id);

-- 种子类型数据：默认全局类型（可被管理 API 修改）
INSERT INTO sensitive_content.sensitive_type
    (tenant_id, code, name, action, action_config, priority, description) VALUES
    ('', 'politics', '政治敏感', 'BLOCK_REQUEST',
     '{}'::jsonb, 100, '涉政敏感内容，默认阻断请求'),
    ('', 'porn', '色情低俗', 'BLOCK_REQUEST',
     '{}'::jsonb, 90, '色情低俗内容，默认阻断请求'),
    ('', 'violence', '暴力恐怖', 'BLOCK_REQUEST',
     '{}'::jsonb, 80, '暴恐内容，默认阻断请求'),
    ('', 'privacy', '隐私信息', 'REDACT_AND_CONTINUE',
     '{"replacement": "***"}'::jsonb, 60, '身份证号/手机号等隐私信息，默认脱敏放行'),
    ('', 'abuse', '辱骂攻击', 'REDACT_AND_CONTINUE',
     '{"replacement": "***"}'::jsonb, 50, '辱骂攻击性内容，默认脱敏放行'),
    ('', 'general', '一般敏感词', 'LOG_ONLY',
     '{}'::jsonb, 10, '一般敏感词，默认仅记录')
ON CONFLICT DO NOTHING;
