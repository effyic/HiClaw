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
    # 0001 初始 + 0002 session_id + 0003 ADJUST_PROMPT + 0004 Agent 绑定 + 0005 类型绑定
    assert {1, 2, 3, 4, 5}.issubset(set(_applied_versions()))


def test_rerun_is_idempotent(migrated_db: str):
    from sensitive_content.migrate import run_migrations

    assert run_migrations(MIGRATIONS_DIR, max_wait_seconds=10) == []


def test_versions_applied_in_order(migrated_db: str, tmp_path: Path):
    """9003 依赖 9002 创建的表，能成功执行即证明按版本顺序应用。

    临时迁移使用 9xxx 版本号，避开仓库内真实迁移（0001/0002…）已占用的版本。
    """
    from sensitive_content.db import db_connection
    from sensitive_content.migrate import run_migrations

    (tmp_path / "9003_insert.sql").write_text(
        "INSERT INTO sensitive_content._mig_order_probe (mark) VALUES ('from-9003');",
        encoding="utf-8",
    )
    (tmp_path / "9002_create.sql").write_text(
        "CREATE TABLE sensitive_content._mig_order_probe "
        "(id SERIAL PRIMARY KEY, mark TEXT NOT NULL);",
        encoding="utf-8",
    )
    try:
        applied = run_migrations(tmp_path, max_wait_seconds=10)
        assert applied == [9002, 9003]
        assert {9002, 9003}.issubset(set(_applied_versions()))
        with db_connection() as conn:
            row = conn.execute(
                text("SELECT mark FROM sensitive_content._mig_order_probe")
            ).first()
        assert row is not None and row[0] == "from-9003"
        # 再次执行：不重复应用
        assert run_migrations(tmp_path, max_wait_seconds=10) == []
    finally:
        with db_connection() as conn:
            conn.execute(text("DROP TABLE IF EXISTS sensitive_content._mig_order_probe"))
            conn.execute(
                text(
                    "DELETE FROM sensitive_content.schema_migration "
                    "WHERE version IN (9002, 9003)"
                )
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


def _global_policy_version() -> int:
    from sensitive_content.db import db_connection

    with db_connection() as conn:
        row = conn.execute(
            text(
                "SELECT version FROM sensitive_content.policy_version "
                "WHERE tenant_id = ''"
            )
        ).first()
    return int(row[0]) if row else -1


def _replay_0003() -> None:
    from sensitive_content.db import db_connection

    sql = (MIGRATIONS_DIR / "0003_adjust_prompt_action.sql").read_text(encoding="utf-8")
    with db_connection() as conn:
        conn.exec_driver_sql(sql)


class TestAdjustPromptSeedIdempotency:
    """0003：重放幂等、不复活 deleted、仅实际插入时递增版本。"""

    def test_replay_idempotent_no_duplicate_and_no_version_bump(self, db: str):
        from sensitive_content.db import db_connection

        with db_connection() as conn:
            types_before = conn.execute(
                text(
                    "SELECT COUNT(*) FROM sensitive_content.sensitive_type "
                    "WHERE tenant_id = '' AND code = 'self_harm'"
                )
            ).scalar()
            rules_before = conn.execute(
                text(
                    """
                    SELECT COUNT(*) FROM sensitive_content.sensitive_rule r
                    JOIN sensitive_content.sensitive_type t ON t.id = r.type_id
                    WHERE t.code = 'self_harm' AND t.tenant_id = ''
                    """
                )
            ).scalar()
            actions_before = conn.execute(
                text(
                    "SELECT COUNT(*) FROM sensitive_content.sensitive_action "
                    "WHERE action = 'ADJUST_PROMPT'"
                )
            ).scalar()
        version_before = _global_policy_version()

        _replay_0003()

        with db_connection() as conn:
            assert (
                conn.execute(
                    text(
                        "SELECT COUNT(*) FROM sensitive_content.sensitive_type "
                        "WHERE tenant_id = '' AND code = 'self_harm'"
                    )
                ).scalar()
                == types_before
            )
            assert (
                conn.execute(
                    text(
                        """
                        SELECT COUNT(*) FROM sensitive_content.sensitive_rule r
                        JOIN sensitive_content.sensitive_type t ON t.id = r.type_id
                        WHERE t.code = 'self_harm' AND t.tenant_id = ''
                        """
                    )
                ).scalar()
                == rules_before
            )
            assert (
                conn.execute(
                    text(
                        "SELECT COUNT(*) FROM sensitive_content.sensitive_action "
                        "WHERE action = 'ADJUST_PROMPT'"
                    )
                ).scalar()
                == actions_before
            )
        assert _global_policy_version() == version_before

    def test_does_not_resurrect_deleted_type(self, db: str):
        from sensitive_content.db import db_connection

        with db_connection() as conn:
            conn.execute(
                text(
                    """
                    UPDATE sensitive_content.sensitive_type
                    SET deleted = TRUE, updated_at = now()
                    WHERE tenant_id = '' AND code = 'self_harm'
                    """
                )
            )
            tid = conn.execute(
                text(
                    "SELECT id FROM sensitive_content.sensitive_type "
                    "WHERE tenant_id = '' AND code = 'self_harm'"
                )
            ).scalar()
        version_before = _global_policy_version()

        _replay_0003()

        with db_connection() as conn:
            rows = conn.execute(
                text(
                    "SELECT id, deleted FROM sensitive_content.sensitive_type "
                    "WHERE tenant_id = '' AND code = 'self_harm' ORDER BY id"
                )
            ).all()
            # 仍只有一行且保持 deleted，未插入新行
            assert len(rows) == 1
            assert rows[0][0] == tid
            assert rows[0][1] is True
        assert _global_policy_version() == version_before

    def test_does_not_resurrect_deleted_rule(self, db: str):
        from sensitive_content.db import db_connection

        with db_connection() as conn:
            tid = conn.execute(
                text(
                    "SELECT id FROM sensitive_content.sensitive_type "
                    "WHERE tenant_id = '' AND code = 'self_harm' AND deleted = FALSE"
                )
            ).scalar()
            assert tid is not None
            conn.execute(
                text(
                    """
                    UPDATE sensitive_content.sensitive_rule
                    SET deleted = TRUE, updated_at = now()
                    WHERE type_id = :tid AND pattern = '自杀' AND match_mode = 'text'
                    """
                ),
                {"tid": tid},
            )
            count_before = conn.execute(
                text(
                    "SELECT COUNT(*) FROM sensitive_content.sensitive_rule "
                    "WHERE type_id = :tid AND pattern = '自杀'"
                ),
                {"tid": tid},
            ).scalar()
        version_before = _global_policy_version()

        _replay_0003()

        with db_connection() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT deleted FROM sensitive_content.sensitive_rule
                    WHERE type_id = :tid AND pattern = '自杀' AND match_mode = 'text'
                    ORDER BY id
                    """
                ),
                {"tid": tid},
            ).all()
            assert len(rows) == count_before
            assert all(r[0] is True for r in rows)
        assert _global_policy_version() == version_before

    def test_fresh_seed_bumps_policy_version(self, db: str):
        """清空业务表后重放 0001+0003：实际插入时条件递增全局版本。"""
        from sensitive_content.db import db_connection

        with db_connection() as conn:
            conn.execute(
                text(
                    """
                    TRUNCATE sensitive_content.sensitive_rule,
                             sensitive_content.sensitive_type,
                             sensitive_content.hit_event,
                             sensitive_content.audit_log,
                             sensitive_content.policy_version
                    RESTART IDENTITY CASCADE
                    """
                )
            )
            conn.exec_driver_sql(
                (MIGRATIONS_DIR / "0001_init.sql").read_text(encoding="utf-8")
            )
        # 0001 种子后全局版本为 0；0003 首次插入应 +1
        assert _global_policy_version() == 0
        _replay_0003()
        assert _global_policy_version() == 1
        # 再重放不空涨
        _replay_0003()
        assert _global_policy_version() == 1
