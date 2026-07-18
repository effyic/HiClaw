"""测试夹具：本机 Docker/Podman 可用时拉起真实 Postgres 跑集成测试，否则跳过 DB 用例。"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
import uuid
from pathlib import Path
from typing import Iterator

import pytest

# 测试用 Token（config 在调用时读取环境变量）
os.environ.setdefault("SENSITIVE_CONTENT_ADMIN_TOKEN", "test-admin")
os.environ.setdefault("SENSITIVE_CONTENT_RUNTIME_TOKEN", "test-runtime")

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"

ADMIN_HEADERS = {"Authorization": "Bearer test-admin", "X-Operator": "tester"}
RUNTIME_HEADERS = {"Authorization": "Bearer test-runtime"}

_PG_IMAGE = os.environ.get("SENSITIVE_CONTENT_TEST_PG_IMAGE", "postgres:16-alpine")


def _container_cli() -> str | None:
    """按可用性返回 podman 或 docker（需守护进程真正可用）。"""
    for cli in ("podman", "docker"):
        if shutil.which(cli) is None:
            continue
        try:
            subprocess.run(
                [cli, "info"], check=True, capture_output=True, timeout=20
            )
            return cli
        except Exception:
            continue
    return None


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="session")
def pg_url() -> Iterator[str]:
    """启动一次性 Postgres 容器；不可用时跳过所有依赖 DB 的测试。"""
    cli = _container_cli()
    if cli is None:
        pytest.skip("Docker/Podman not available; skipping DB integration tests")
    port = _free_port()
    name = f"sensitive-content-test-pg-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        [
            cli, "run", "-d", "--rm", "--name", name,
            "-e", "POSTGRES_PASSWORD=postgres",
            "-p", f"127.0.0.1:{port}:5432",
            _PG_IMAGE,
        ],
        check=True,
        capture_output=True,
    )
    url = f"postgresql+psycopg://postgres:postgres@127.0.0.1:{port}/postgres"
    try:
        _wait_ready(url)
        yield url
    finally:
        subprocess.run([cli, "stop", name], capture_output=True)


def _wait_ready(url: str, timeout: float = 120.0) -> None:
    from sqlalchemy import create_engine, text

    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            engine = create_engine(url)
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            engine.dispose()
            return
        except Exception as exc:
            last_exc = exc
            time.sleep(0.5)
    raise RuntimeError(f"postgres container not ready: {last_exc}")


@pytest.fixture(scope="session")
def migrated_db(pg_url: str) -> Iterator[str]:
    """指向容器 DB 并执行一次迁移。"""
    os.environ["SENSITIVE_CONTENT_DB_URL"] = pg_url
    from sensitive_content.db import reset_engine
    from sensitive_content.migrate import run_migrations

    reset_engine()
    run_migrations(MIGRATIONS_DIR, max_wait_seconds=30)
    from sensitive_content.db import db_connection
    from sqlalchemy import text
    with db_connection() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS agno_agent (
                    id BIGSERIAL PRIMARY KEY,
                    tenant_id VARCHAR(64) NOT NULL,
                    role_code VARCHAR(64) NOT NULL DEFAULT 'default',
                    enabled BOOLEAN NOT NULL DEFAULT TRUE,
                    UNIQUE (tenant_id, role_code)
                )
                """
            )
        )
    yield pg_url
    reset_engine()


@pytest.fixture()
def db(migrated_db: str) -> Iterator[str]:
    """每个用例前清空业务表并重放种子数据（迁移文件幂等）。"""
    from sqlalchemy import text

    from sensitive_content.db import db_connection

    with db_connection() as conn:
        conn.execute(
            text(
                """
                TRUNCATE sensitive_content.sensitive_rule,
                         sensitive_content.sensitive_type,
                         sensitive_content.hit_event,
                         sensitive_content.audit_log,
                         sensitive_content.policy_version,
                         sensitive_content.agent_rule_binding,
                         sensitive_content.agent_policy_version
                RESTART IDENTITY CASCADE
                """
            )
        )
        conn.execute(text("TRUNCATE agno_agent RESTART IDENTITY"))
        # 重放含种子数据的迁移片段（幂等）；0002 仅为 DDL，无需重放
        conn.exec_driver_sql(
            (MIGRATIONS_DIR / "0001_init.sql").read_text(encoding="utf-8")
        )
        conn.exec_driver_sql(
            (MIGRATIONS_DIR / "0003_adjust_prompt_action.sql").read_text(
                encoding="utf-8"
            )
        )
    yield migrated_db


@pytest.fixture()
def client(db: str):
    """FastAPI TestClient（测试中关闭后台清理任务）。"""
    from fastapi.testclient import TestClient

    from sensitive_content.api import create_app

    return TestClient(create_app(enable_cleanup=False))
