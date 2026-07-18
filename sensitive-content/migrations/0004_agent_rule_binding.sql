-- Agent-scoped rule bindings and hit-event attribution.
-- Existing agents intentionally receive no bindings.

CREATE TABLE IF NOT EXISTS sensitive_content.agent_rule_binding (
    tenant_id  TEXT NOT NULL,
    agent_id   BIGINT NOT NULL,
    rule_id    BIGINT NOT NULL REFERENCES sensitive_content.sensitive_rule(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, agent_id, rule_id)
);

CREATE INDEX IF NOT EXISTS idx_agent_rule_binding_rule
    ON sensitive_content.agent_rule_binding (rule_id);

CREATE TABLE IF NOT EXISTS sensitive_content.agent_policy_version (
    tenant_id  TEXT NOT NULL,
    agent_id   BIGINT NOT NULL,
    version    BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, agent_id)
);

ALTER TABLE sensitive_content.hit_event
    ADD COLUMN IF NOT EXISTS agent_id BIGINT NULL;

CREATE INDEX IF NOT EXISTS idx_hit_event_tenant_agent_hit_at
    ON sensitive_content.hit_event (tenant_id, agent_id, hit_at);

ALTER TABLE sensitive_content.audit_log
    DROP CONSTRAINT IF EXISTS audit_log_target_kind_check;

ALTER TABLE sensitive_content.audit_log
    ADD CONSTRAINT audit_log_target_kind_check
    CHECK (target_kind IN ('rule', 'type', 'agent_binding'));
