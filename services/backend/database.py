"""
数据库连接管理模块
提供异步数据库连接和会话管理
"""
import asyncio

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from config import get_settings
import logging

logger = logging.getLogger(__name__)
settings = get_settings()

# 创建异步引擎
engine = create_async_engine(
    settings.database_url,
    echo=settings.debug,
    pool_pre_ping=True,  # 连接前检查
    pool_size=10,         # 连接池大小
    max_overflow=20,      # 最大溢出连接数
    pool_recycle=3600,    # 连接回收时间（秒）
)

# 创建 Session 工厂
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False
)


async def get_db():
    """获取数据库会话（依赖注入）"""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception as e:
            logger.error(f"数据库会话错误: {e}")
            try:
                await asyncio.shield(session.rollback())
            except Exception as rollback_error:
                logger.warning(f"数据库回滚失败: {rollback_error}")
            raise
        finally:
            try:
                await asyncio.shield(session.close())
            except Exception as close_error:
                logger.warning(f"数据库会话关闭失败: {close_error}")
