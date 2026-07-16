"""敏感内容检测模块的异常定义（不依赖 agno，供 server 层直接捕获）。"""
from __future__ import annotations


class SensitivePolicyUnavailableError(Exception):
    """无有效策略快照（或 fail-closed 场景下检测无法完成）。

    server 层映射为 ``503 sensitive_policy_unavailable``，不调用 LLM。
    """

    code = "sensitive_policy_unavailable"

    def __init__(self, reason: str = "") -> None:
        super().__init__(reason or self.code)
        self.reason = reason or self.code


class SensitiveRegexTimeoutError(Exception):
    """单条正则规则匹配超时（内部异常，由 detector 按 fail 模式转换）。"""

    def __init__(self, rule_id: int, pattern_len: int = 0) -> None:
        # 注意：异常信息不携带规则明文，只带规则 ID 与长度元数据
        super().__init__(f"regex rule {rule_id} timed out (pattern_len={pattern_len})")
        self.rule_id = rule_id
        self.pattern_len = pattern_len
