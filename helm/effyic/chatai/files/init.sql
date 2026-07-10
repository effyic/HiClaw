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
  created_at    TIMESTAMPTZ  DEFAULT CURRENT_TIMESTAMP,
  updated_at    TIMESTAMPTZ  DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (tenant_id, role_code)
);

CREATE INDEX IF NOT EXISTS idx_agno_agent_tenant_enabled ON agno_agent (tenant_id, enabled);

CREATE TABLE IF NOT EXISTS department (
  id               BIGSERIAL PRIMARY KEY,
  tenant_id        VARCHAR(64)  NOT NULL,
  department_code  VARCHAR(64)  NOT NULL,
  department_name  VARCHAR(128) NOT NULL,
  description      TEXT,
  sort_order       INT          NOT NULL DEFAULT 0,
  enabled          BOOLEAN      NOT NULL DEFAULT TRUE,
  UNIQUE (tenant_id, department_code)
);

CREATE INDEX IF NOT EXISTS idx_department_tenant_enabled ON department (tenant_id, enabled);

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

-- tenant-a: triage + expert (medical demo)
INSERT INTO agno_agent (
  tenant_id, role_code, display_name, description,
  system_prompt, instructions, knowledge_ids, mcp_enabled, mcp_config, workflow, enabled
) VALUES (
  'tenant-a', 'triage', '分诊助手', '医疗分诊',
  '你是医疗分诊助手。根据用户描述，从科室列表中选择最合适的 expert 路由。',
  '先了解症状，再给出科室建议；仅允许路由到白名单科室。',
  '["weknora-kb-triage"]'::jsonb, FALSE, NULL,
  NULL, TRUE
) ON CONFLICT (tenant_id, role_code) DO UPDATE SET
  display_name = EXCLUDED.display_name,
  system_prompt = EXCLUDED.system_prompt,
  instructions = EXCLUDED.instructions,
  knowledge_ids = EXCLUDED.knowledge_ids,
  workflow = EXCLUDED.workflow,
  enabled = EXCLUDED.enabled,
  updated_at = CURRENT_TIMESTAMP;

INSERT INTO agno_agent (
  tenant_id, role_code, display_name, description,
  system_prompt, instructions, knowledge_ids, mcp_enabled, mcp_config, workflow, enabled
) VALUES (
  'tenant-a', 'expert_cardiology', '心内科专家', '心血管专科',
  '你是心内科专家助手。', '基于循证医学回答心血管相关问题，必要时建议线下就诊。',
  '["weknora-kb-cardiology"]'::jsonb, FALSE, NULL,
  NULL, TRUE
) ON CONFLICT (tenant_id, role_code) DO UPDATE SET
  display_name = EXCLUDED.display_name,
  system_prompt = EXCLUDED.system_prompt,
  instructions = EXCLUDED.instructions,
  knowledge_ids = EXCLUDED.knowledge_ids,
  workflow = EXCLUDED.workflow,
  enabled = EXCLUDED.enabled,
  updated_at = CURRENT_TIMESTAMP;

INSERT INTO department (
  tenant_id, department_code, department_name, description, sort_order, enabled
) VALUES
  ('tenant-a', 'cardiology', '心内科', '心血管疾病诊疗', 10, TRUE)
ON CONFLICT (tenant_id, department_code) DO UPDATE SET
  department_name = EXCLUDED.department_name,
  description = EXCLUDED.description,
  sort_order = EXCLUDED.sort_order,
  enabled = EXCLUDED.enabled;
