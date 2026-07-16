"""集成测试：版本化迁移（顺序执行、记录、幂等、advisory lock 互斥）。"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from sqlalchemy import text

from conftest import MIGRATIONS_DIR


def _applied_versions() -> list[int]:
    from sensitive_content.db import db_connection

    with db_connection() as conn:
        rows = conn.execute(
            text("SELECT version FROM sensitive_content.schema_migration ORDER BY version")
        ).all()
    return [int(r[0]) for r in rows]


def test_initial_migration_recorded(migrated_db: str):
    assert 1 in _applied_versions()


def test_rerun_is_idempotent(migrated_db: str):
    from sensitive_content.migrate import run_migrations

    assert run_migrations(MIGRATIONS_DIR, max_wait_seconds=10) == []


def test_versions_applied_in_order(migrated_db: str, tmp_path: Path):
    """0003 依赖 0002 创建的表，能成功执行即证明按版本顺序应用。"""
    from sensitive_content.db import db_connection
    from sensitive_content.migrate import run_migrations

    (tmp_path / "0003_insert.sql").write_text(
        "INSERT INTO sensitive_content._mig_order_probe (mark) VALUES ('from-0003');",
        encoding="utf-8",
    )
    (tmp_path / "0002_create.sql").write_text(
        "CREATE TABLE sensitive_content._mig_order_probe "
        "(id SERIAL PRIMARY KEY, mark TEXT NOT NULL);",
        encoding="utf-8",
    )
    try:
        applied = run_migrations(tmp_path, max_wait_seconds=10)
        assert applied == [2, 3]
        assert {2, 3}.issubset(set(_applied_versions()))
        with db_connection() as conn:
            row = conn.execute(
                text("SELECT mark FROM sensitive_content._mig_order_probe")
            ).first()
        assert row is not None and row[0] == "from-0003"
        # 再次执行：不重复应用
        assert run_migrations(tmp_path, max_wait_seconds=10) == []
    finally:
        with db_connection() as conn:
            conn.execute(text("DROP TABLE IF EXISTS sensitive_content._mig_order_probe"))
            conn.execute(
                text("DELETE FROM sensitive_content.schema_migration WHERE version IN (2, 3)")
            )


def test_advisory_lock_mutual_exclusion(migrated_db: str, tmp_path: Path):
    """持有 advisory lock 时并发 migrate 阻塞，释放后才继续。"""
    from sensitive_content.db import get_engine
    from sensitive_content.migrate import ADVISORY_LOCK_KEY, run_migrations

    holder = get_engine().connect()
    holder.execute(text("SELECT pg_advisory_lock(:k)"), {"k": ADVISORY_LOCK_KEY})
    holder.commit()

    done = threading.Event()

    def _migrate() -> None:
        run_migrations(tmp_path, max_wait_seconds=10)
        done.set()

    worker = threading.Thread(target=_migrate, daemon=True)
    try:
        worker.start()
        time.sleep(1.0)
        # 锁被持有期间 migrate 无法完成
        assert not done.is_set()
        holder.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": ADVISORY_LOCK_KEY})
        holder.commit()
        assert done.wait(timeout=10), "migrate did not finish after lock release"
    finally:
        holder.close()
        worker.join(timeout=10)


def test_wait_for_db_times_out_when_unreachable(monkeypatch):
    """DB 不可达时按上限重试后报错（内置可用性等待）。"""
    import pytest

    from sensitive_content.db import reset_engine
    from sensitive_content.migrate import wait_for_db

    monkeypatch.setenv(
        "SENSITIVE_CONTENT_DB_URL",
        "postgresql+psycopg://nobody:nope@127.0.0.1:1/none",
    )
    reset_engine()
    try:
        start = time.monotonic()
        with pytest.raises(RuntimeError, match="database not ready"):
            wait_for_db(max_wait_seconds=2)
        assert time.monotonic() - start >= 1.0
    finally:
        reset_engine()
