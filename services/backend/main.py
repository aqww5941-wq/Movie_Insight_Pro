"""
Movie Insight Pro - 主应用入口
电影数据检索与 AI 推荐系统
Version: 4.0.0
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select

from core.cache import cache_manager
from core.config import get_settings
from core.exceptions import (
    general_exception_handler,
    validation_exception_handler,
)
from db.database import engine
from db.db_sync import close_sync_pool
from routers.agent import router as agent_router
from routers.movies import router as movies_router
from routers.system import router as system_router

settings = get_settings()

logging.basicConfig(
    level=getattr(logging, settings.log_level),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    logger.info("🚀 应用启动中...")

    await cache_manager.initialize()

    try:
        async with engine.connect() as conn:
            await conn.execute(select(1))
        logger.info("✅ PostgreSQL 数据库连接就绪")
    except Exception as e:
        logger.error("❌ 数据库连接失败: %s", e)

    if app.state.movie_agent:
        try:
            app.state.movie_agent.ask("hi")
            logger.info("🤖 AI Agent 预热完成")
        except Exception as e:
            logger.warning("⚠️ AI Agent 预热失败: %s", e)

    logger.info("✨ 应用启动完成")
    yield
    logger.info("🔄 应用关闭中...")
    await cache_manager.close()
    close_sync_pool()
    await engine.dispose()
    logger.info("👋 应用已关闭")


app = FastAPI(
    title=settings.app_name,
    description="专业的电影数据检索与 AI 推荐系统 (PostgreSQL + Redis + AI)",
    version=settings.app_version,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    root_path=settings.root_path,
    root_path_in_servers=True,
)

# AI Agent 初始化
try:
    from agents.movie_agent import MovieAgent
    app.state.movie_agent = MovieAgent()
    logger.info("✅ AI Agent 初始化成功")
except Exception as e:
    logger.error("❌ AI Agent 初始化失败: %s", e, exc_info=True)
    app.state.movie_agent = None

# 中间件
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 异常处理器
app.add_exception_handler(RequestValidationError, validation_exception_handler)
app.add_exception_handler(Exception, general_exception_handler)

# 路由注册
app.include_router(system_router)
app.include_router(movies_router)
app.include_router(agent_router)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=settings.debug,
        log_level=settings.log_level.lower(),
    )
