"""
同步数据库连接池（供 LangChain 同步工具使用）
工具运行在线程池中，使用 psycopg2 ThreadedConnectionPool 复用连接。
"""
import logging
import threading
from typing import Optional

import psycopg2
from psycopg2 import pool as psycopg2_pool
from psycopg2.extras import RealDictCursor

from core.config import get_settings

logger = logging.getLogger(__name__)

_pool: Optional[psycopg2_pool.ThreadedConnectionPool] = None
_lock = threading.Lock()


def _build_dsn() -> str:
    settings = get_settings()
    return (
        f"host={settings.pg_host} "
        f"port={settings.pg_port} "
        f"user={settings.pg_user} "
        f"password={settings.pg_password} "
        f"dbname={settings.pg_dbname}"
    )


def get_sync_pool() -> psycopg2_pool.ThreadedConnectionPool:
    """获取或懒初始化同步连接池"""
    global _pool
    if _pool is not None:
        return _pool

    with _lock:
        if _pool is not None:
            return _pool
        try:
            dsn = _build_dsn()
            _pool = psycopg2_pool.ThreadedConnectionPool(
                minconn=2,
                maxconn=10,
                dsn=dsn,
            )
            logger.info("✅ psycopg2 同步连接池初始化完成 (min=2, max=10)")
        except Exception as e:
            logger.error("❌ psycopg2 连接池初始化失败: %s", e)
            raise
        return _pool


def get_sync_conn():
    """从连接池获取一个连接（带 RealDictCursor）"""
    pool = get_sync_pool()
    conn = pool.getconn()
    conn.cursor_factory = RealDictCursor
    return conn


def put_sync_conn(conn):
    """归还连接到连接池"""
    if _pool is not None and conn is not None:
        try:
            _pool.putconn(conn)
        except Exception as e:
            logger.warning("归还连接失败: %s", e)


def close_sync_pool():
    """关闭同步连接池（在应用 shutdown 时调用）"""
    global _pool
    with _lock:
        if _pool is not None:
            _pool.closeall()
            _pool = None
            logger.info("psycopg2 同步连接池已关闭")
