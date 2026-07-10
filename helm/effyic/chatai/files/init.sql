-- agno_worker schema bootstrap (idempotent)
CREATE DATABASE IF NOT EXISTS agno_worker
  DEFAULT CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

USE agno_worker;

CREATE TABLE IF NOT EXISTS agno_agent (
  id            BIGINT AUTO_INCREMENT PRIMARY KEY,
  tenant_id     VARCHAR(64)  NOT NULL,
  role_code     VARCHAR(64)  NOT NULL DEFAULT 'default',
  display_name  VARCHAR(128) DEFAULT '',
  description   TEXT,
  system_prompt TEXT,
  instructions  TEXT,
  knowledge_ids JSON,
  mcp_enabled   TINYINT(1)   NOT NULL DEFAULT 0,
  mcp_config    JSON,
  workflow      JSON,
  enabled       TINYINT(1)   NOT NULL DEFAULT 1,
  created_at    TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
  updated_at    TIMESTAMP    DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_tenant_role (tenant_id, role_code),
  KEY idx_tenant_enabled (tenant_id, enabled)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS department (
  id               BIGINT AUTO_INCREMENT PRIMARY KEY,
  tenant_id        VARCHAR(64)  NOT NULL,
  department_code  VARCHAR(64)  NOT NULL,
  department_name  VARCHAR(128) NOT NULL,
  description      TEXT,
  sort_order       INT          NOT NULL DEFAULT 0,
  enabled          TINYINT(1)   NOT NULL DEFAULT 1,
  UNIQUE KEY uk_tenant_dept (tenant_id, department_code),
  KEY idx_tenant_enabled (tenant_id, enabled)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- default tenant
INSERT INTO agno_agent (
  tenant_id, role_code, display_name, description,
  system_prompt, instructions, knowledge_ids, mcp_enabled, mcp_config, workflow, enabled
) VALUES (
  'default', 'default', '默认租户', '通用助手',
  '你是默认租户助手。', '提供通用帮助。',
  JSON_ARRAY('weknora-kb-general'), 0, NULL,
  NULL, 1
) ON DUPLICATE KEY UPDATE
  display_name = VALUES(display_name),
  system_prompt = VALUES(system_prompt),
  instructions = VALUES(instructions),
  knowledge_ids = VALUES(knowledge_ids),
  workflow = VALUES(workflow),
  enabled = VALUES(enabled);

-- tenant-a: triage + expert (medical demo)
INSERT INTO agno_agent (
  tenant_id, role_code, display_name, description,
  system_prompt, instructions, knowledge_ids, mcp_enabled, mcp_config, workflow, enabled
) VALUES (
  'tenant-a', 'triage', '分诊助手', '医疗分诊',
  '你是医疗分诊助手。根据用户描述，从科室列表中选择最合适的 expert 路由。',
  '先了解症状，再给出科室建议；仅允许路由到白名单科室。',
  JSON_ARRAY('weknora-kb-triage'), 0, NULL,
  NULL, 1
) ON DUPLICATE KEY UPDATE
  display_name = VALUES(display_name),
  system_prompt = VALUES(system_prompt),
  instructions = VALUES(instructions),
  knowledge_ids = VALUES(knowledge_ids),
  workflow = VALUES(workflow),
  enabled = VALUES(enabled);

INSERT INTO agno_agent (
  tenant_id, role_code, display_name, description,
  system_prompt, instructions, knowledge_ids, mcp_enabled, mcp_config, workflow, enabled
) VALUES (
  'tenant-a', 'expert_cardiology', '心内科专家', '心血管专科',
  '你是心内科专家助手。', '基于循证医学回答心血管相关问题，必要时建议线下就诊。',
  JSON_ARRAY('weknora-kb-cardiology'), 0, NULL,
  NULL, 1
) ON DUPLICATE KEY UPDATE
  display_name = VALUES(display_name),
  system_prompt = VALUES(system_prompt),
  instructions = VALUES(instructions),
  knowledge_ids = VALUES(knowledge_ids),
  workflow = VALUES(workflow),
  enabled = VALUES(enabled);

INSERT INTO department (
  tenant_id, department_code, department_name, description, sort_order, enabled
) VALUES
  ('tenant-a', 'cardiology', '心内科', '心血管疾病诊疗', 10, 1)
ON DUPLICATE KEY UPDATE
  department_name = VALUES(department_name),
  description = VALUES(description),
  sort_order = VALUES(sort_order),
  enabled = VALUES(enabled);
