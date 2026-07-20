-- Agent 绑定从「规则」改为「敏感词类型」。
-- 未上线环境：破坏性替换，迁移后删除 agent_rule_binding。

-- 1) 历史跨类型 override 预检（fail-fast，禁止静默改策略）
DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM sensitive_content.sensitive_rule o
    JOIN sensitive_content.sensitive_rule g
      ON g.id = o.overrides_global_rule_id
    WHERE o.deleted = FALSE
      AND o.overrides_global_rule_id IS NOT NULL
      AND o.type_id <> g.type_id
  ) THEN
    RAISE EXCEPTION
      '0005 blocked: cross-type override rows exist; clean them before migrating';
  END IF;
END $$;

-- 2) Agent ↔ 类型绑定表
CREATE TABLE IF NOT EXISTS sensitive_content.agent_type_binding (
    tenant_id  TEXT NOT NULL,
    agent_id   BIGINT NOT NULL,
    type_id    BIGINT NOT NULL REFERENCES sensitive_content.sensitive_type(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, agent_id, type_id)
);

CREATE INDEX IF NOT EXISTS idx_agent_type_binding_type
    ON sensitive_content.agent_type_binding (type_id);

-- 3) 从旧规则绑定迁移（DISTINCT；仅未删除的规则与类型；禁用未删仍迁）
INSERT INTO sensitive_content.agent_type_binding (tenant_id, agent_id, type_id)
SELECT DISTINCT arb.tenant_id, arb.agent_id, r.type_id
FROM sensitive_content.agent_rule_binding arb
JOIN sensitive_content.sensitive_rule r ON r.id = arb.rule_id
JOIN sensitive_content.sensitive_type t ON t.id = r.type_id
WHERE r.deleted = FALSE AND t.deleted = FALSE
ON CONFLICT DO NOTHING;

-- 4) 受影响 Agent 策略版本 +1（无行则插入 version=1）
INSERT INTO sensitive_content.agent_policy_version (tenant_id, agent_id, version, updated_at)
SELECT b.tenant_id, b.agent_id, 1, now()
FROM (
    SELECT DISTINCT tenant_id, agent_id
    FROM sensitive_content.agent_type_binding
) b
ON CONFLICT (tenant_id, agent_id) DO UPDATE
    SET version = sensitive_content.agent_policy_version.version + 1,
        updated_at = now();

-- 5) 删除旧规则绑定表
DROP INDEX IF EXISTS sensitive_content.idx_agent_rule_binding_rule;
DROP TABLE IF EXISTS sensitive_content.agent_rule_binding;
