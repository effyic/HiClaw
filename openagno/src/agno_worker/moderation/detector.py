"""敏感内容检测引擎：归一化、文本索引化匹配、正则超时保护与确定性排序。

核心流程：
1. 对输入文本生成四种视图（原文/原文小写/归一化/归一化小写），每种视图带
   位置映射表，可将命中区间回写到原文坐标（供脱敏改写）。
2. 文本规则按首字符建立索引后单遍扫描；正则规则在快照编译期预编译，
   运行时用 ``regex`` 库的 ``timeout`` 保护（默认 50ms）。
3. 正则超时不静默视为未命中：fail-open 跳过该规则并告警；fail-closed 抛
   :class:`SensitivePolicyUnavailableError`（503 语义）。同一规则连续超时
   N 次（默认 3）触发进程内熔断，冷却期（默认 60s）后自动重试恢复。
4. 命中排序（确定性）：类型优先级降序 → 规则优先级降序 → 首个匹配位置
   升序 → 规则 ID 升序；仅排序后第一条决定响应行为。
"""
from __future__ import annotations

import logging
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any

import regex

from agno_worker.moderation.config import ModerationConfig
from agno_worker.moderation.errors import SensitivePolicyUnavailableError
from agno_worker.moderation.models import (
    Match,
    PolicySnapshot,
    SensitiveRule,
    SensitiveType,
)

logger = logging.getLogger(__name__)

# 零宽字符集合（归一化时剔除）
_ZERO_WIDTH_CHARS = frozenset("\u200b\u200c\u200d\u2060\ufeff")


@dataclass(frozen=True)
class TextView:
    """一种文本视图及其到原文的位置映射。

    ``index_map[i]`` 为视图第 i 个字符对应的原文字符下标；
    一个原文字符可能展开为多个视图字符（NFKC/lower），它们映射到同一原文下标。
    """

    text: str
    index_map: tuple[int, ...]

    def to_source_span(self, start: int, end: int) -> tuple[int, int]:
        """将视图区间 [start, end) 回写为原文区间 [src_start, src_end)。"""
        if not self.index_map or start >= end:
            return (0, 0)
        start = max(0, min(start, len(self.index_map) - 1))
        end_idx = max(0, min(end - 1, len(self.index_map) - 1))
        return (self.index_map[start], self.index_map[end_idx] + 1)


def _build_view(source: str, *, normalize: bool, lower: bool) -> TextView:
    """构建文本视图。

    - ``normalize=True``：逐字符 NFKC + 去零宽字符 + 空白折叠（连续空白折叠
      为单个空格，映射到首个空白字符）。逐字符 NFKC 与整串 NFKC 在跨字符组合
      场景略有差异，但可保证位置映射精确，对检测足够。
    - ``lower=True``：逐字符小写化（同样保持映射）。
    """
    chars: list[str] = []
    index_map: list[int] = []
    prev_is_space = False
    for idx, ch in enumerate(source):
        if normalize:
            if ch in _ZERO_WIDTH_CHARS:
                continue
            expanded = unicodedata.normalize("NFKC", ch)
        else:
            expanded = ch
        if lower:
            expanded = expanded.lower()
        if normalize and expanded.isspace():
            # 空白折叠：连续空白只保留一个空格
            if prev_is_space:
                continue
            chars.append(" ")
            index_map.append(idx)
            prev_is_space = True
            continue
        prev_is_space = False
        for out_ch in expanded:
            chars.append(out_ch)
            index_map.append(idx)
    return TextView(text="".join(chars), index_map=tuple(index_map))


def _normalize_pattern(pattern: str, *, normalize: bool, lower: bool) -> str:
    """按与文本视图一致的方式转换文本规则的 pattern。"""
    return _build_view(pattern, normalize=normalize, lower=lower).text


def merge_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """合并重叠/相邻的命中区间（用于脱敏统一替换）。"""
    if not spans:
        return []
    ordered = sorted(spans)
    merged = [list(ordered[0])]
    for start, end in ordered[1:]:
        if start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(s, e) for s, e in merged]


def _view_key(rule: SensitiveRule) -> tuple[bool, bool]:
    """规则使用的文本视图键：(normalize, lower)。

    正则规则大小写交给 ``regex.IGNORECASE`` 处理，视图恒为保留大小写版本。
    """
    if rule.match_mode == "regex":
        return (rule.normalize, False)
    return (rule.normalize, not rule.case_sensitive)


@dataclass
class _CompiledTextRule:
    rule: SensitiveRule
    type_: SensitiveType
    pattern: str  # 已按视图转换后的 pattern
    view_key: tuple[bool, bool]


@dataclass
class _CompiledRegexRule:
    rule: SensitiveRule
    type_: SensitiveType
    compiled: Any  # regex.Pattern
    view_key: tuple[bool, bool]


@dataclass
class _BreakerState:
    """单条正则规则的熔断状态（进程内）。"""

    consecutive_timeouts: int = 0
    tripped_until: float = 0.0


@dataclass
class CompiledPolicy:
    """快照编译产物：只包含 enabled 规则，正则已预编译。"""

    snapshot: PolicySnapshot
    text_rules: list[_CompiledTextRule] = field(default_factory=list)
    regex_rules: list[_CompiledRegexRule] = field(default_factory=list)
    # 文本索引：view_key -> 首字符 -> 该视图下以此字符开头的规则列表
    text_index: dict[tuple[bool, bool], dict[str, list[_CompiledTextRule]]] = field(
        default_factory=dict
    )

    @property
    def version(self) -> str:
        return self.snapshot.version


def compile_policy(snapshot: PolicySnapshot) -> CompiledPolicy:
    """将快照编译为可执行策略：过滤 enabled、预编译正则、建立文本索引。"""
    policy = CompiledPolicy(snapshot=snapshot)
    for rule in snapshot.rules:
        # 只加载 enabled=TRUE 的规则；类型缺失或禁用的规则同样跳过
        if not rule.enabled or not rule.pattern:
            continue
        type_ = snapshot.types.get(rule.type_id)
        if type_ is None or not type_.enabled:
            continue
        key = _view_key(rule)
        if rule.match_mode == "regex":
            flags = 0 if rule.case_sensitive else regex.IGNORECASE
            try:
                compiled = regex.compile(rule.pattern, flags)
            except regex.error:
                # 管理端已做语法校验，这里兜底跳过并告警（不带规则明文）
                logger.warning(
                    "敏感内容规则 %s 正则编译失败，已跳过（pattern_len=%d）",
                    rule.id,
                    len(rule.pattern),
                )
                continue
            policy.regex_rules.append(
                _CompiledRegexRule(rule=rule, type_=type_, compiled=compiled, view_key=key)
            )
        else:
            pattern = _normalize_pattern(
                rule.pattern, normalize=key[0], lower=key[1]
            )
            if not pattern:
                continue
            compiled_rule = _CompiledTextRule(
                rule=rule, type_=type_, pattern=pattern, view_key=key
            )
            policy.text_rules.append(compiled_rule)
            bucket = policy.text_index.setdefault(key, {})
            bucket.setdefault(pattern[0], []).append(compiled_rule)
    return policy


class SensitiveContentDetector:
    """检测引擎（无状态匹配 + 进程内正则熔断状态）。"""

    def __init__(self, config: ModerationConfig | None = None) -> None:
        self._config = config or ModerationConfig()
        self._breakers: dict[int, _BreakerState] = {}

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------

    def detect(self, text: str, policy: CompiledPolicy) -> list[Match]:
        """对输入文本执行全部规则匹配，返回确定性排序后的命中列表。"""
        if not text:
            return []
        views = self._build_views(text, policy)
        matches: list[Match] = []
        matches.extend(self._match_text_rules(views, policy))
        matches.extend(self._match_regex_rules(views, policy))
        # 确定性排序：类型优先级降序 → 规则优先级降序 → 首个匹配位置 → 规则 ID
        matches.sort(
            key=lambda m: (-m.type_priority, -m.rule_priority, m.first_pos, m.rule_id)
        )
        return matches

    # ------------------------------------------------------------------
    # 视图构建
    # ------------------------------------------------------------------

    def _build_views(
        self, text: str, policy: CompiledPolicy
    ) -> dict[tuple[bool, bool], TextView]:
        """只为策略实际用到的视图键构建文本视图（惰性节省开销）。"""
        needed: set[tuple[bool, bool]] = set(policy.text_index.keys())
        needed.update(r.view_key for r in policy.regex_rules)
        views: dict[tuple[bool, bool], TextView] = {}
        for normalize, lower in needed:
            views[(normalize, lower)] = _build_view(
                text, normalize=normalize, lower=lower
            )
        return views

    # ------------------------------------------------------------------
    # 文本规则：首字符索引 + 单遍扫描
    # ------------------------------------------------------------------

    def _match_text_rules(
        self,
        views: dict[tuple[bool, bool], TextView],
        policy: CompiledPolicy,
    ) -> list[Match]:
        matches: list[Match] = []
        for key, index in policy.text_index.items():
            view = views[key]
            hits: dict[int, list[tuple[int, int]]] = {}
            text = view.text
            for pos, ch in enumerate(text):
                candidates = index.get(ch)
                if not candidates:
                    continue
                for item in candidates:
                    end = pos + len(item.pattern)
                    if text.startswith(item.pattern, pos):
                        hits.setdefault(item.rule.id, []).append(
                            view.to_source_span(pos, end)
                        )
            for item in index_rules(index):
                spans = hits.get(item.rule.id)
                if spans:
                    matches.append(self._build_match(item.rule, item.type_, spans))
        return matches

    # ------------------------------------------------------------------
    # 正则规则：预编译 + 超时保护 + 熔断
    # ------------------------------------------------------------------

    def _match_regex_rules(
        self,
        views: dict[tuple[bool, bool], TextView],
        policy: CompiledPolicy,
    ) -> list[Match]:
        matches: list[Match] = []
        for item in policy.regex_rules:
            state = self._breakers.setdefault(item.rule.id, _BreakerState())
            now = time.monotonic()
            if (
                state.consecutive_timeouts >= self._config.breaker_threshold
                and now < state.tripped_until
            ):
                # 熔断中：跳过该规则，冷却期结束后自动重试
                continue
            view = views[item.view_key]
            try:
                spans = self._run_regex(item, view)
            except TimeoutError:
                self._on_regex_timeout(item.rule, state)
                continue
            if state.consecutive_timeouts:
                logger.info("敏感内容规则 %s 正则恢复正常，熔断计数清零", item.rule.id)
            state.consecutive_timeouts = 0
            state.tripped_until = 0.0
            if spans:
                matches.append(self._build_match(item.rule, item.type_, spans))
        return matches

    def _run_regex(self, item: _CompiledRegexRule, view: TextView) -> list[tuple[int, int]]:
        spans: list[tuple[int, int]] = []
        for m in item.compiled.finditer(view.text, timeout=self._config.regex_timeout):
            start, end = m.span()
            if start == end:
                continue  # 空匹配不计命中
            spans.append(view.to_source_span(start, end))
        return spans

    def _on_regex_timeout(self, rule: SensitiveRule, state: _BreakerState) -> None:
        state.consecutive_timeouts += 1
        logger.warning(
            "敏感内容规则 %s 正则匹配超时（连续 %d 次，阈值 %d）",
            rule.id,
            state.consecutive_timeouts,
            self._config.breaker_threshold,
        )
        if state.consecutive_timeouts >= self._config.breaker_threshold:
            state.tripped_until = time.monotonic() + self._config.breaker_cooldown
            logger.error(
                "敏感内容规则 %s 连续超时达到阈值，熔断 %.0f 秒后自动重试",
                rule.id,
                self._config.breaker_cooldown,
            )
        if not self._config.fail_open:
            # fail-closed：本次请求按策略不可用处理（503，不调用 LLM）
            raise SensitivePolicyUnavailableError(
                f"regex rule {rule.id} timed out in fail-closed mode"
            )

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------

    @staticmethod
    def _build_match(
        rule: SensitiveRule, type_: SensitiveType, spans: list[tuple[int, int]]
    ) -> Match:
        ordered = tuple(sorted(spans))
        return Match(
            rule_id=rule.id,
            type_id=type_.id,
            action=type_.action,
            spans=ordered,
            rule_priority=rule.priority,
            type_priority=type_.priority,
        )


def index_rules(
    index: dict[str, list[_CompiledTextRule]]
) -> list[_CompiledTextRule]:
    """展开首字符索引中的规则并按规则 ID 去重（保持稳定顺序）。"""
    seen: set[int] = set()
    result: list[_CompiledTextRule] = []
    for bucket in index.values():
        for item in bucket:
            if item.rule.id not in seen:
                seen.add(item.rule.id)
                result.append(item)
    return result


def redact_text(
    text: str,
    spans: list[tuple[int, int]],
    *,
    replacement: str = "*",
) -> str:
    """按合并后的原文区间统一替换（每个字符替换为 replacement）。"""
    merged = merge_spans(spans)
    if not merged:
        return text
    parts: list[str] = []
    cursor = 0
    for start, end in merged:
        start = max(0, min(start, len(text)))
        end = max(start, min(end, len(text)))
        parts.append(text[cursor:start])
        parts.append(replacement * (end - start))
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts)
