"""环境变量配置（DB URL、Admin/Runtime Token、保留期等）。"""
from __future__ import annotations

import os
from pathlib import Path


def db_url() -> str:
    """PostgreSQL 连接 URL（sensitive_content schema 所在库）。"""
    url = os.environ.get("SENSITIVE_CONTENT_DB_URL", "").strip()
    if not url:
        raise RuntimeError("SENSITIVE_CONTENT_DB_URL is required")
    return url


def admin_token() -> str:
    """管理 API 的 Bearer Token（平台管理员凭据）。"""
    return os.environ.get("SENSITIVE_CONTENT_ADMIN_TOKEN", "").strip()


def runtime_token() -> str:
    """内部 API（快照下发/事件采集）的 Bearer Token。"""
    return os.environ.get("SENSITIVE_CONTENT_RUNTIME_TOKEN", "").strip()


def fingerprint_key() -> str:
    """HMAC 指纹密钥（供检测端生成指纹；服务端仅透传配置，不参与计算）。"""
    return os.environ.get("SENSITIVE_CONTENT_FINGERPRINT_KEY", "").strip()


def retention_days() -> int:
    """命中事件保留天数，默认 90 天。"""
    return int(os.environ.get("SENSITIVE_CONTENT_RETENTION_DAYS", "90"))


def cleanup_interval_seconds() -> int:
    """定期清理任务的执行间隔（秒），默认每天一次。"""
    return int(os.environ.get("SENSITIVE_CONTENT_CLEANUP_INTERVAL", "86400"))


def migrate_wait_seconds() -> int:
    """migrate 等待数据库可用的重试时长上限（秒），默认 120。"""
    return int(os.environ.get("SENSITIVE_CONTENT_MIGRATE_WAIT_SECONDS", "120"))


def migrations_dir() -> Path:
    """迁移 SQL 目录：优先环境变量，否则取源码树内的 migrations/。"""
    env = os.environ.get("SENSITIVE_CONTENT_MIGRATIONS_DIR", "").strip()
    if env:
        return Path(env)
    # 源码布局：src/sensitive_content/config.py → 项目根/migrations
    return Path(__file__).resolve().parents[2] / "migrations"


def serve_host() -> str:
    return os.environ.get("SENSITIVE_CONTENT_HOST", "0.0.0.0")


def serve_port() -> int:
    return int(os.environ.get("SENSITIVE_CONTENT_PORT", "8091"))
