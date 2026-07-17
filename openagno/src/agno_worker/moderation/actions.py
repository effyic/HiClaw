"""行为决策：将排序后的命中映射为 GuardrailDecision（检测与响应解耦）。

- 八种行为各自注册一个处理器；新增行为只需 ``register_action_handler``。
- ``BUSINESS_ACTION`` 只能调用 Worker 内预注册的白名单处理器；找不到对应
  处理器时不静默继续——fail-open 记告警并按 LOG_ONLY 降级，fail-closed
  按 BLOCK_REQUEST 阻断请求。
- ``ADJUST_PROMPT`` 放行并收集语气指引；最终是否注入由 Guardrail 按放行/阻断决定。
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from agno_worker.moderation.config import ModerationConfig
from agno_worker.moderation.detector import merge_spans, redact_text
from agno_worker.moderation.models import (
    ActionType,
    DecisionKind,
    GuardrailDecision,
    Match,
)

logger = logging.getLogger(__name__)

# 默认文案（action_config 未配置时兜底；不含敏感词与原文）
DEFAULT_BLOCK_MESSAGE = "您的请求包含敏感内容，已被拦截。"
DEFAULT_FIXED_REPLY = "抱歉，这个话题我无法回答。"
DEFAULT_TERMINATE_MESSAGE = "本次对话包含敏感内容，会话已结束。"
DEFAULT_REDACT_REPLACEMENT = "*"

# 行为处理器签名：(final_match, all_matches, text, action_config, config) -> GuardrailDecision
ActionHandler = Callable[
    [Match, list[Match], str, dict[str, Any], ModerationConfig], GuardrailDecision
]

# 业务动作白名单处理器签名：(final_match, all_matches, text, action_config) -> None
BusinessActionHandler = Callable[[Match, list[Match], str, dict[str, Any]], Any]

_business_handlers: dict[str, BusinessActionHandler] = {}


def register_business_action(name: str, handler: BusinessActionHandler) -> None:
    """注册业务动作白名单处理器（Worker 启动期调用）。"""
    _business_handlers[str(name)] = handler


def unregister_business_action(name: str) -> None:
    _business_handlers.pop(str(name), None)


def _config_text(action_config: dict[str, Any], keys: tuple[str, ...], default: str) -> str:
    for key in keys:
        value = action_config.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return default


def _base_decision(
    kind: DecisionKind,
    action: ActionType,
    match: Match,
    matches: list[Match],
    action_config: dict[str, Any],
) -> GuardrailDecision:
    return GuardrailDecision(
        kind=kind,
        action=action,
        rule_id=match.rule_id,
        type_id=match.type_id,
        action_config=dict(action_config),
        matches=list(matches),
    )


def _handle_log_only(
    match: Match,
    matches: list[Match],
    text: str,
    action_config: dict[str, Any],
    config: ModerationConfig,
) -> GuardrailDecision:
    return _base_decision(DecisionKind.CONTINUE, ActionType.LOG_ONLY, match, matches, action_config)


def _handle_block_request(
    match: Match,
    matches: list[Match],
    text: str,
    action_config: dict[str, Any],
    config: ModerationConfig,
) -> GuardrailDecision:
    decision = _base_decision(
        DecisionKind.REJECT, ActionType.BLOCK_REQUEST, match, matches, action_config
    )
    decision.message = _config_text(action_config, ("message", "reply"), DEFAULT_BLOCK_MESSAGE)
    return decision


def _handle_fixed_reply(
    match: Match,
    matches: list[Match],
    text: str,
    action_config: dict[str, Any],
    config: ModerationConfig,
) -> GuardrailDecision:
    decision = _base_decision(
        DecisionKind.RESPOND, ActionType.FIXED_REPLY, match, matches, action_config
    )
    decision.message = _config_text(action_config, ("reply", "message"), DEFAULT_FIXED_REPLY)
    return decision


def _handle_custom_response(
    match: Match,
    matches: list[Match],
    text: str,
    action_config: dict[str, Any],
    config: ModerationConfig,
) -> GuardrailDecision:
    decision = _base_decision(
        DecisionKind.RESPOND, ActionType.CUSTOM_RESPONSE, match, matches, action_config
    )
    decision.message = _config_text(action_config, ("reply", "message"), DEFAULT_FIXED_REPLY)
    return decision


def _handle_end_conversation(
    match: Match,
    matches: list[Match],
    text: str,
    action_config: dict[str, Any],
    config: ModerationConfig,
) -> GuardrailDecision:
    decision = _base_decision(
        DecisionKind.TERMINATE, ActionType.END_CONVERSATION, match, matches, action_config
    )
    decision.message = _config_text(
        action_config, ("reply", "message"), DEFAULT_TERMINATE_MESSAGE
    )
    return decision


def _handle_redact_and_continue(
    match: Match,
    matches: list[Match],
    text: str,
    action_config: dict[str, Any],
    config: ModerationConfig,
) -> GuardrailDecision:
    decision = _base_decision(
        DecisionKind.REDACT, ActionType.REDACT_AND_CONTINUE, match, matches, action_config
    )
    replacement = _config_text(
        action_config, ("replacement", "mask"), DEFAULT_REDACT_REPLACEMENT
    )
    # 合并全部命中区间统一替换（含其他低优先级规则的命中，避免部分泄漏）
    all_spans = [span for m in matches for span in m.spans]
    decision.redacted_text = redact_text(
        text, merge_spans(all_spans), replacement=replacement
    )
    return decision


def _handle_business_action(
    match: Match,
    matches: list[Match],
    text: str,
    action_config: dict[str, Any],
    config: ModerationConfig,
) -> GuardrailDecision:
    name = _config_text(action_config, ("handler", "action_name", "name"), "")
    handler = _business_handlers.get(name) if name else None
    if handler is None:
        # 白名单处理器缺失：不静默继续，按 fail 模式处理
        if config.fail_open:
            logger.warning(
                "BUSINESS_ACTION 处理器 %r 未注册（规则 %s），fail-open 按 LOG_ONLY 降级",
                name,
                match.rule_id,
            )
            decision = _base_decision(
                DecisionKind.CONTINUE, ActionType.BUSINESS_ACTION, match, matches, action_config
            )
            return decision
        logger.error(
            "BUSINESS_ACTION 处理器 %r 未注册（规则 %s），fail-closed 阻断请求",
            name,
            match.rule_id,
        )
        decision = _base_decision(
            DecisionKind.REJECT, ActionType.BUSINESS_ACTION, match, matches, action_config
        )
        decision.message = DEFAULT_BLOCK_MESSAGE
        return decision

    try:
        handler(match, matches, text, dict(action_config))
    except Exception:
        logger.exception("BUSINESS_ACTION 处理器 %r 执行失败（规则 %s）", name, match.rule_id)
    decision = _base_decision(
        DecisionKind.BUSINESS_ACTION, ActionType.BUSINESS_ACTION, match, matches, action_config
    )
    return decision


def _handle_adjust_prompt(
    match: Match,
    matches: list[Match],
    text: str,
    action_config: dict[str, Any],
    config: ModerationConfig,
) -> GuardrailDecision:
    # 放行类：不改写用户输入；语气指引由 decide() 末尾统一收集
    return _base_decision(
        DecisionKind.CONTINUE, ActionType.ADJUST_PROMPT, match, matches, action_config
    )


def _collect_prompt_guidances(
    matches: list[Match],
    types_config: dict[int, dict[str, Any]],
) -> list[str]:
    """收集全部 ADJUST_PROMPT 命中的 prompt_guidance。

    排序：type_priority DESC，同优先级 type_id ASC；按 type_id 去重；
    仅保留非空字符串。
    """
    adjust_matches = [m for m in matches if m.action == ActionType.ADJUST_PROMPT]
    adjust_matches.sort(key=lambda m: (-m.type_priority, m.type_id))
    seen_type_ids: set[int] = set()
    guidances: list[str] = []
    for m in adjust_matches:
        if m.type_id in seen_type_ids:
            continue
        seen_type_ids.add(m.type_id)
        cfg = types_config.get(m.type_id) or {}
        guidance = cfg.get("prompt_guidance")
        if isinstance(guidance, str) and guidance.strip():
            guidances.append(guidance)
    return guidances


# 行为处理器注册表：新增响应行为只需在这里注册 handler
_action_handlers: dict[ActionType, ActionHandler] = {
    ActionType.LOG_ONLY: _handle_log_only,
    ActionType.BLOCK_REQUEST: _handle_block_request,
    ActionType.FIXED_REPLY: _handle_fixed_reply,
    ActionType.CUSTOM_RESPONSE: _handle_custom_response,
    ActionType.END_CONVERSATION: _handle_end_conversation,
    ActionType.REDACT_AND_CONTINUE: _handle_redact_and_continue,
    ActionType.BUSINESS_ACTION: _handle_business_action,
    ActionType.ADJUST_PROMPT: _handle_adjust_prompt,
}


def register_action_handler(action: ActionType, handler: ActionHandler) -> None:
    """覆盖 / 新增行为处理器（扩展点）。"""
    _action_handlers[action] = handler


def decide(
    matches: list[Match],
    text: str,
    types_config: dict[int, dict[str, Any]],
    config: ModerationConfig,
) -> GuardrailDecision:
    """由确定性排序后的命中列表计算最终决策（第一条命中决定行为）。

    ``types_config``：type_id -> action_config 映射（来自快照类型表）。
    末尾始终收集全部 ADJUST_PROMPT 指引；是否写入请求上下文由 Guardrail 决定。
    """
    final = matches[0]
    action_config = dict(types_config.get(final.type_id) or {})
    handler = _action_handlers.get(final.action, _handle_log_only)
    decision = handler(final, matches, text, action_config, config)
    decision.prompt_guidances = _collect_prompt_guidances(matches, types_config)
    return decision
