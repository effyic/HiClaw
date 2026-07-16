"""SQLAlchemy engine + 连接池（仿 openagno tenant/db.py 模式）。"""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterator

from sensitive_content.config import db_url

_engine: Any = None


def get_engine() -> Any:
    """返回进程级共享的 SQLAlchemy engine（带连接池）。"""
    global _engine
    if _engine is None:
        from sqlalchemy import create_engine

        _engine = create_engine(
            db_url(),
            pool_size=int(os.environ.get("SENSITIVE_CONTENT_DB_POOL_SIZE", "5")),
            max_overflow=int(os.environ.get("SENSITIVE_CONTENT_DB_POOL_OVERFLOW", "10")),
            pool_pre_ping=True,
            pool_recycle=int(os.environ.get("SENSITIVE_CONTENT_DB_POOL_RECYCLE", "3600")),
        )
    return _engine


def reset_engine() -> None:
    """释放连接池（测试或重载时使用）。"""
    global _engine
    if _engine is not None:
        _engine.dispose()
        _engine = None


@contextmanager
def db_connection() -> Iterator[Any]:
    """从连接池借出连接；正常退出时提交，异常时回滚。"""
    conn = get_engine().connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
