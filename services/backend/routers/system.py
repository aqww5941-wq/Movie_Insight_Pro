"""系统路由：/、/health、/images/proxy"""

import logging
from datetime import datetime
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.cache import cache_manager
from core.config import get_settings
from core.exceptions import DatabaseError
from db.database import get_db
from db.models import Movie
from schemas import HealthResponse

logger = logging.getLogger(__name__)
settings = get_settings()
router = APIRouter()


def _is_allowed_image_host(raw_url: str) -> bool:
    try:
        parsed = urlparse(raw_url)
        host = (parsed.hostname or "").lower()
        if not host.endswith("doubanio.com"):
            return False
        # 路径遍历防护
        path = parsed.path or ""
        if ".." in path:
            return False
        return True
    except Exception:
        return False


@router.get("/", tags=["系统"])
async def root():
    return {
        "name": settings.app_name,
        "version": settings.app_version,
        "status": "running",
        "docs": "/docs",
    }


@router.get("/health", tags=["系统"], response_model=HealthResponse)
async def health_check(request: Request, db: AsyncSession = Depends(get_db)):
    cache_status = "connected" if cache_manager._initialized else "disconnected"

    try:
        stmt = select(func.count(Movie.id))
        result = await db.execute(stmt)
        count = result.scalar() or 0
        db_status = "connected"
    except Exception as e:
        logger.error("健康检查数据库错误: %s", e)
        count = 0
        db_status = f"error: {e}"

    ai_status = "enabled" if request.app.state.movie_agent else "disabled"
    return HealthResponse(
        status="healthy" if db_status == "connected" else "degraded",
        database=db_status,
        cache=cache_status,
        ai_agent=ai_status,
        total_movies=count,
        timestamp=datetime.now().isoformat(),
    )


@router.get("/images/proxy", tags=["系统"])
async def image_proxy(url: str = Query(..., min_length=8, max_length=2048)):
    if not _is_allowed_image_host(url):
        raise DatabaseError(detail="仅允许代理 doubanio 图片地址")

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        "Referer": "https://movie.douban.com/",
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }

    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code >= 400:
                raise DatabaseError(detail=f"图片拉取失败: HTTP {resp.status_code}")
            content_type = resp.headers.get("content-type", "image/jpeg")
            return StreamingResponse(
                iter([resp.content]),
                media_type=content_type,
                headers={"Cache-Control": "public, max-age=86400"},
            )
    except DatabaseError:
        raise
    except Exception as e:
        logger.warning("图片代理失败: %s", e)
        raise DatabaseError(detail="图片代理失败")
