-- ============================================================
-- 0003：新增 ADJUST_PROMPT 行为 + self_harm 类型与中文基础规则集
--
-- - sensitive_action 目录追加 ADJUST_PROMPT（ON CONFLICT 幂等）
-- - 全局类型 self_harm（priority=70）与一组中文基础关键词规则
--   使用 INSERT…SELECT…WHERE NOT EXISTS，且等价性查询不限制 deleted，
--   避免重放时复活用户已逻辑删除的数据
-- - 仅在实际新插入了类型或规则时，条件递增全局 policy_version
--
-- 说明：中文基础规则集仅为关键词覆盖，不是完整自伤风险分类器。
-- ============================================================

-- 行为目录
INSERT INTO sensitive_content.sensitive_action (action, name, description) VALUES
    (
        'ADJUST_PROMPT',
        '语气调整',
        '放行请求，并将类型配置的 prompt_guidance 注入本轮对话 instructions'
    )
ON CONFLICT (action) DO NOTHING;

DO $$
DECLARE
    did_insert boolean := false;
    n int;
    tid bigint;
    guidance text := $g$用户可能正经历情绪困扰或自我伤害相关念头。请以温暖、关心、非评判的语气回应：认真倾听，表达理解与支持，鼓励向可信赖的亲友或专业人士求助；若合适可提供心理援助热线等信息。不要提供任何可能造成伤害的具体方法，也不要夸大恐吓。切勿向用户提及、暗示或解释存在敏感内容检测、命中规则、内容审核策略等内部机制。$g$;
BEGIN
    -- 全局类型 self_harm（不限制 deleted：已删也视为曾存在）
    INSERT INTO sensitive_content.sensitive_type
        (tenant_id, code, name, action, action_config, priority, description)
    SELECT
        '',
        'self_harm',
        '自伤风险',
        'ADJUST_PROMPT',
        jsonb_build_object('prompt_guidance', guidance),
        70,
        '自伤/自杀相关表述的中文基础关键词覆盖（非完整分类器），默认语气调整放行'
    WHERE NOT EXISTS (
        SELECT 1 FROM sensitive_content.sensitive_type
        WHERE tenant_id = '' AND code = 'self_harm'
    );

    GET DIAGNOSTICS n = ROW_COUNT;
    IF n > 0 THEN
        did_insert := true;
    END IF;

    SELECT id INTO tid
    FROM sensitive_content.sensitive_type
    WHERE tenant_id = '' AND code = 'self_harm'
    ORDER BY id
    LIMIT 1;

    IF tid IS NOT NULL THEN
        -- 中文基础规则集（按 type_id + pattern + match_mode 幂等，不限制 deleted）
        INSERT INTO sensitive_content.sensitive_rule
            (tenant_id, type_id, pattern, match_mode, case_sensitive, normalize,
             description, priority)
        SELECT
            '',
            tid,
            v.pattern,
            'text',
            FALSE,
            TRUE,
            'self_harm 中文基础规则',
            0
        FROM (
            VALUES
                ('自杀'),
                ('轻生'),
                ('结束生命'),
                ('不想活'),
                ('自我伤害'),
                ('割腕'),
                ('寻死')
        ) AS v(pattern)
        WHERE NOT EXISTS (
            SELECT 1 FROM sensitive_content.sensitive_rule r
            WHERE r.type_id = tid
              AND r.pattern = v.pattern
              AND r.match_mode = 'text'
        );

        GET DIAGNOSTICS n = ROW_COUNT;
        IF n > 0 THEN
            did_insert := true;
        END IF;
    END IF;

    IF did_insert THEN
        UPDATE sensitive_content.policy_version
        SET version = version + 1, updated_at = now()
        WHERE tenant_id = '';
    END IF;
END $$;
