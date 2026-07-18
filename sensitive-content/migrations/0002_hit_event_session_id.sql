-- ============================================================
-- 0002：hit_event 新增明文 session_id 列
--
-- 产品决策：后台需要从命中事件跳转查看完整会话记录（复用网关
-- /effyic/v1/sessions/{session_id} 接口），因此在事件中保存明文
-- session_id。用户原文仍不落库，request/session 指纹字段保持不变。
-- 旧数据无会话信息，默认空字符串兼容。
-- ============================================================

ALTER TABLE sensitive_content.hit_event
    ADD COLUMN IF NOT EXISTS session_id TEXT NOT NULL DEFAULT '';

-- 明细查询路径：按租户 + 会话 + 时间过滤（空会话不进索引）
CREATE INDEX IF NOT EXISTS idx_hit_event_tenant_session_hit_at
    ON sensitive_content.hit_event (tenant_id, session_id, hit_at)
    WHERE session_id <> '';
