"""版本化迁移：advisory lock + 按版本执行 migrations/ + schema_migration 记录。

只有 migrate 子命令（Helm migration Job 或手动执行）执行 DDL；
服务启动不执行任何 DDL。
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Any

from sqlalchemy import text

from sensitive_content.config import migrate_wait_seconds, migrations_dir
from sensitive_content.db import get_engine

logger = logging.getLogger(__name__)

# advisory lock 键（固定常量，防止多个 migration Job 并发执行 DDL）
ADVISORY_LOCK_KEY = 815001

_MIGRATION_FILE = re.compile(r"^(\d{4})_.+\.sql$")


def discover_migrations(directory: Path | None = None) -> list[tuple[int, Path]]:
    """扫描迁移目录，返回按版本号升序排列的 (version, path) 列表。"""
    directory = directory or migrations_dir()
    found: list[tuple[int, Path]] = []
    for path in sorted(directory.glob("*.sql")):
        match = _MIGRATION_FILE.match(path.name)
        if match:
            found.append((int(match.group(1)), path))
    found.sort(key=lambda item: item[0])
    return found


def wait_for_db(max_wait_seconds: int | None = None) -> None:
    """等待数据库可用：连接失败退避重试，超过上限抛出异常。"""
    deadline = time.monotonic() + (
        max_wait_seconds if max_wait_seconds is not None else migrate_wait_seconds()
    )
    delay = 1.0
    while True:
        try:
            with get_engine().connect() as conn:
                conn.execute(text("SELECT 1"))
            return
        except Exception as exc:
            if time.monotonic() >= deadline:
                raise RuntimeError(f"database not ready within wait limit: {exc}") from exc
            logger.warning("database not ready, retrying in %.1fs: %s", delay, exc)
            time.sleep(delay)
            delay = min(delay * 2, 10.0)


def _ensure_bootstrap(conn: Any) -> None:
    """确保 schema 与 schema_migration 表存在（幂等）。"""
    conn.execute(text("CREATE SCHEMA IF NOT EXISTS sensitive_content"))
    conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS sensitive_content.schema_migration (
                version    INT PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
    )
    conn.commit()


def run_migrations(
    directory: Path | None = None,
    *,
    max_wait_seconds: int | None = None,
) -> list[int]:
    """执行所有未应用的迁移，返回本次应用的版本号列表。"""
    wait_for_db(max_wait_seconds)
    applied: list[int] = []
    conn = get_engine().connect()
    try:
        # 会话级 advisory lock：并发 migration Job 在此互斥
        conn.execute(text("SELECT pg_advisory_lock(:key)"), {"key": ADVISORY_LOCK_KEY})
        conn.commit()
        try:
            _ensure_bootstrap(conn)
            done = {
                int(row[0])
                for row in conn.execute(
                    text("SELECT version FROM sensitive_content.schema_migration")
                )
            }
            for version, path in discover_migrations(directory):
                if version in done:
                    continue
                logger.info("applying migration %04d: %s", version, path.name)
                sql = path.read_text(encoding="utf-8")
                # 单个迁移在同一事务内执行并记录版本
                conn.exec_driver_sql(sql)
                conn.execute(
                    text(
                        "INSERT INTO sensitive_content.schema_migration (version) "
                        "VALUES (:version)"
                    ),
                    {"version": version},
                )
                conn.commit()
                applied.append(version)
        finally:
            conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": ADVISORY_LOCK_KEY})
            conn.commit()
    finally:
        conn.close()
    if applied:
        logger.info("applied migrations: %s", applied)
    else:
        logger.info("no pending migrations")
    return applied
