-- agno_agent / department schema bootstrap for PostgreSQL (idempotent)
-- Connect to the target database before running (e.g. aip_hub_test).

CREATE TABLE IF NOT EXISTS agno_agent (
  id            BIGSERIAL PRIMARY KEY,
  tenant_id     VARCHAR(64)  NOT NULL,
  role_code     VARCHAR(64)  NOT NULL DEFAULT 'default',
  display_name  VARCHAR(128) DEFAULT '',
  description   TEXT,
  system_prompt TEXT,
  instructions  TEXT,
  knowledge_ids JSONB,
  mcp_enabled   BOOLEAN      NOT NULL DEFAULT FALSE,
  mcp_config    JSONB,
  workflow      JSONB,
  enabled       BOOLEAN      NOT NULL DEFAULT TRUE,
  -- 与 aip-hub AgnoAgentDO(LocalDateTime) / sql/postgresql/agno-agent.sql 对齐：用 timestamp 无时区
  -- 勿用 TIMESTAMPTZ，否则 JDBC 映射 LocalDateTime 会报 Cannot convert TIMESTAMPTZ
  created_at    TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
  updated_at    TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (tenant_id, role_code)
);

CREATE INDEX IF NOT EXISTS idx_agno_agent_tenant_enabled ON agno_agent (tenant_id, enabled);

-- default tenant
INSERT INTO agno_agent (
  tenant_id, role_code, display_name, description,
  system_prompt, instructions, knowledge_ids, mcp_enabled, mcp_config, workflow, enabled
) VALUES (
  'default', 'default', '默认租户', '通用助手',
  '你是默认租户助手。', '提供通用帮助。',
  '["weknora-kb-general"]'::jsonb, FALSE, NULL,
  NULL, TRUE
) ON CONFLICT (tenant_id, role_code) DO UPDATE SET
  display_name = EXCLUDED.display_name,
  system_prompt = EXCLUDED.system_prompt,
  instructions = EXCLUDED.instructions,
  knowledge_ids = EXCLUDED.knowledge_ids,
  workflow = EXCLUDED.workflow,
  enabled = EXCLUDED.enabled,
  updated_at = CURRENT_TIMESTAMP;