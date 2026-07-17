"""SensitiveContentGuardrail：Agno pre_hooks 首位的敏感内容检测入口。

行为映射（由排序第一条命中决定）：
- ``REDACT_AND_CONTINUE``：合并重叠命中区间统一替换，直接改写
  ``run_input.input_content`` 放行——后续业务 pre_hook、Prompt 与会话持久化
  看到的都是脱敏后文本。
- ``LOG_ONLY``：上报命中事件后放行。
- 其余行为：抛 :class:`SensitiveContentDecisionError`（``InputCheckError``
  子类），携带 ``GuardrailDecision`` 与 ``action_config``。

存储保护说明：agno 捕获 ``InputCheckError`` 后仍会把 run（含 ``run_input``）
持久化到会话，因此抛异常前必须先把 ``run_input.input_content`` 改写为占位
文本，保证被阻断/终止的原始输入不落库；同时把决策写入请求上下文
（contextvars），engine 在 ``arun`` 返回后读取并完成响应映射。

接口按 ``RunInput | TeamRunInput`` 编写；本期只在 Agent 路径运行（v1 交付
范围），Team runtime 建成后可直接复用。
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Union

from agno.exceptions import CheckTrigger, InputCheckError
from agno.guardrails import BaseGuardrail

from agno_worker.moderation import actions as action_registry
from agno_worker.moderation.config import ModerationConfig
from agno_worker.moderation.context import get_request_context
from agno_worker.moderation.detector import SensitiveContentDetector
from agno_worker.moderation.errors import SensitivePolicyUnavailableError
from agno_worker.moderation.models import DecisionKind, GuardrailDecision
from agno_worker.moderation.reporter import HitReporter, build_hit_events
from agno_worker.moderation.snapshot import SnapshotClient

if TYPE_CHECKING:
    from agno.run.agent import RunInput
    from agno.run.team import TeamRunInput

logger = logging.getLogger(__name__)

# 阻断/终止后写回 run_input 的占位文本（替代原文落库）
BLOCKED_INPUT_PLACEHOLDER = "[input removed by sensitive content policy]"


class SensitiveContentDecisionError(InputCheckError):
    """敏感内容命中且最终行为需要中断本次运行（非 redact/log_only）。"""

    def __init__(self, decision: GuardrailDecision) -> None:
        # message 不携带敏感词与用户原文
        super().__init__(
            f"sensitive content decision: {decision.action.value}",
            check_trigger=CheckTrigger.INPUT_NOT_ALLOWED,
            additional_data={
                "final_action": decision.action.value,
                "rule_id": decision.rule_id,
                "type_id": decision.type_id,
            },
        )
        self.decision = decision
        self.action_config = dict(decision.action_config)


class SensitiveContentGuardrail(BaseGuardrail):
    """对 Agent 输入执行敏感内容检测的 Guardrail。"""

    def __init__(
        self,
        config: ModerationConfig,
        *,
        snapshot_client: SnapshotClient | None = None,
        detector: SensitiveContentDetector | None = None,
        reporter: HitReporter | None = None,
    ) -> None:
        self.config = config
        self.snapshot_client = snapshot_client or SnapshotClient(config)
        self.detector = detector or SensitiveContentDetector(config)
        self.reporter = reporter or HitReporter(config)

    # ------------------------------------------------------------------
    # BaseGuardrail 接口
    # ------------------------------------------------------------------

    def check(self, run_input: Union["RunInput", "TeamRunInput"]) -> None:
        """同步检测入口（agno 同步 run 路径）。"""
        self._check_impl(run_input)

    async def async_check(self, run_input: Union["RunInput", "TeamRunInput"]) -> None:
        """异步检测入口：顺带启动后台快照刷新与上报任务（需事件循环）。"""
        try:
            self.snapshot_client.ensure_background_refresh()
            self.reporter.ensure_started()
        except RuntimeError:
            # 无事件循环（理论上不会发生在 async 路径），忽略
            pass
        self._check_impl(run_input)

    # ------------------------------------------------------------------
    # 核心流程
    # ------------------------------------------------------------------

    def _check_impl(self, run_input: Any) -> None:
        ctx = get_request_context()
        # 每轮检测开头清空，避免复用上下文时污染上一轮指引
        ctx.prompt_guidances = []
        text = getattr(run_input, "input_content", None)
        if not isinstance(text, str) or not text:
            # 本期只检测纯文本输入；多模态/结构化输入直接放行
            return

        policy = self.snapshot_client.get_policy(ctx.tenant_id)
        if policy is None:
            self._handle_no_policy(ctx, run_input)
            return

        try:
            matches = self.detector.detect(text, policy)
        except SensitivePolicyUnavailableError:
            # fail-closed 下正则超时：等价于策略不可用（503，不调用 LLM）；
            # 输入未完成检测，同样清除原文防止落库
            ctx.policy_unavailable = True
            run_input.input_content = BLOCKED_INPUT_PLACEHOLDER
            raise InputCheckError(
                "sensitive_policy_unavailable",
                check_trigger=CheckTrigger.INPUT_NOT_ALLOWED,
            ) from None
        if not matches:
            return

        types_config = {t.id: dict(t.action_config) for t in policy.snapshot.types.values()}
        decision = action_registry.decide(matches, text, types_config, self.config)
        decision.policy_version = policy.version

        # 命中即上报（含 log_only 与 redact）：每条命中一条事件
        self._report(ctx, decision)

        if decision.kind == DecisionKind.REDACT:
            # 脱敏后放行：改写 run_input，后续流程与持久化只见脱敏文本
            run_input.input_content = decision.redacted_text
            ctx.prompt_guidances = list(decision.prompt_guidances)
            return
        if decision.kind in (DecisionKind.CONTINUE, DecisionKind.BUSINESS_ACTION):
            # LOG_ONLY / ADJUST_PROMPT（或业务动作已执行 / fail-open 降级）：放行
            ctx.prompt_guidances = list(decision.prompt_guidances)
            return

        # respond / reject / terminate：中断本次运行（指引保持空列表，不注入）。
        # agno 捕获 InputCheckError 后仍会持久化 run，先清除原文防止落库。
        ctx.pending_decision = decision
        run_input.input_content = BLOCKED_INPUT_PLACEHOLDER
        raise SensitiveContentDecisionError(decision)

    def _handle_no_policy(self, ctx: Any, run_input: Any) -> None:
        """无有效快照（或快照超过过期上限）：按 fail 模式处理。"""
        if self.config.fail_open:
            logger.warning(
                "租户 %s 无有效敏感内容策略快照，fail-open 跳过检测",
                ctx.tenant_id,
            )
            return
        logger.error(
            "租户 %s 无有效敏感内容策略快照，fail-closed 拒绝请求",
            ctx.tenant_id,
        )
        ctx.policy_unavailable = True
        # 输入未经过检测，清除原文防止落库
        run_input.input_content = BLOCKED_INPUT_PLACEHOLDER
        raise InputCheckError(
            "sensitive_policy_unavailable",
            check_trigger=CheckTrigger.INPUT_NOT_ALLOWED,
        )

    def _report(self, ctx: Any, decision: GuardrailDecision) -> None:
        events = build_hit_events(
            decision,
            tenant_id=ctx.tenant_id,
            request_id=ctx.request_id,
            session_id=ctx.session_id,
            fingerprint_key=self.config.fingerprint_key,
        )
        try:
            self.reporter.enqueue(events)
        except RuntimeError:
            # 无事件循环时（纯同步单测场景）跳过上报
            logger.debug("命中事件入队失败：当前无事件循环")


# ----------------------------------------------------------------------
# 模块级单例：builder 热重载时复用同一 SnapshotClient / HitReporter
# ----------------------------------------------------------------------

_singleton: SensitiveContentGuardrail | None = None


def maybe_build_guardrail() -> SensitiveContentGuardrail | None:
    """配置 ``SENSITIVE_CONTENT_SERVICE_URL`` 时返回 Guardrail 单例，否则 None。"""
    global _singleton
    config = ModerationConfig.from_env()
    if not config.enabled:
        return None
    if _singleton is None or _singleton.config != config:
        _singleton = SensitiveContentGuardrail(config)
    return _singleton


def reset_guardrail_singleton() -> None:
    """测试辅助：重置单例。"""
    global _singleton
    _singleton = None
