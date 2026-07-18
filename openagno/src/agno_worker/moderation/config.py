"""敏感内容检测模块配置（全部来自 SENSITIVE_CONTENT_* 环境变量）。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

# 落盘缓存默认路径（不放 workspace，见实施计划）
DEFAULT_CACHE_PATH = "/var/lib/agno/moderation/policy-snapshot.json"

# fail 模式取值
FAIL_OPEN = "open"
FAIL_CLOSED = "closed"


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class ModerationConfig:
    """检测端运行配置。``service_url`` 为空表示功能未启用。"""

    service_url: str = ""
    runtime_token: str = ""
    cache_path: str = DEFAULT_CACHE_PATH
    refresh_interval: float = 30.0
    max_stale: float = 600.0
    fail_mode: str = FAIL_OPEN
    fingerprint_key: str = ""
    # 正则单条超时（秒）；实施计划默认 50ms
    regex_timeout: float = 0.05
    # 连续超时熔断阈值与冷却时长（秒）
    breaker_threshold: int = 3
    breaker_cooldown: float = 60.0
    # 上报队列容量与批量大小
    reporter_queue_size: int = 1000
    reporter_batch_size: int = 100
    reporter_max_retries: int = 5

    extra: dict = field(default_factory=dict)

    @property
    def enabled(self) -> bool:
        return bool(self.service_url)

    @property
    def fail_open(self) -> bool:
        return self.fail_mode != FAIL_CLOSED

    @classmethod
    def from_env(cls) -> "ModerationConfig":
        fail_mode = os.environ.get("SENSITIVE_CONTENT_FAIL_MODE", FAIL_OPEN).strip().lower()
        if fail_mode not in (FAIL_OPEN, FAIL_CLOSED):
            fail_mode = FAIL_OPEN
        return cls(
            service_url=os.environ.get("SENSITIVE_CONTENT_SERVICE_URL", "").strip().rstrip("/"),
            runtime_token=os.environ.get("SENSITIVE_CONTENT_RUNTIME_TOKEN", "").strip(),
            cache_path=os.environ.get("SENSITIVE_CONTENT_CACHE_PATH", "").strip()
            or DEFAULT_CACHE_PATH,
            refresh_interval=_env_float("SENSITIVE_CONTENT_REFRESH_INTERVAL", 30.0),
            max_stale=_env_float("SENSITIVE_CONTENT_MAX_STALE", 600.0),
            fail_mode=fail_mode,
            fingerprint_key=os.environ.get("SENSITIVE_CONTENT_FINGERPRINT_KEY", "").strip(),
            regex_timeout=_env_float("SENSITIVE_CONTENT_REGEX_TIMEOUT_MS", 50.0) / 1000.0,
            breaker_threshold=_env_int("SENSITIVE_CONTENT_BREAKER_THRESHOLD", 3),
            breaker_cooldown=_env_float("SENSITIVE_CONTENT_BREAKER_COOLDOWN", 60.0),
        )
