"""电影检索路由：/movies、/movies/similar、/movies/{id}、/stats/platforms、/surprise-me"""

import json
import logging
import math
import re
from datetime import datetime
from time import perf_counter
from typing import Optional

from fastapi import APIRouter, Depends, Path, Query, Request
from sqlalchemy import cast, desc, func, Numeric, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.cache import cache_manager
from core.config import get_settings
from core.exceptions import DatabaseError, MovieNotFoundError
from db.database import get_db
from db.models import Movie
from schemas import MovieBase, MovieDetail, MovieListResponse, RagSearchRequest, RagSearchResponse
from services.search import (
    _vector_fallback_search,
    build_embedding_vector,
    fetch_movies_by_titles,
    search_movies_for_ai_mode,
)

logger = logging.getLogger(__name__)
settings = get_settings()
router = APIRouter()


@router.get("/movies", tags=["电影检索"], response_model=MovieListResponse)
async def list_movies(
    q: Optional[str] = Query(None, description="搜索关键词"),
    source: Optional[str] = Query(None, description="来源平台"),
    year: Optional[str] = Query(None, description="年份"),
    min_rating: Optional[float] = Query(None, ge=0, le=10, description="最低评分"),
    sort_by: Optional[str] = Query("rating", description="排序字段: rating/year/rating_count"),
    page: int = Query(1, ge=1, description="页码"),
    limit: int = Query(20, ge=1, le=100, description="每页数量"),
    db: AsyncSession = Depends(get_db),
):
    """电影列表查询：支持搜索、过滤、排序和分页"""
    use_cache = page == 1 and not any([q, source, year, min_rating])
    cache_key = f"movies:list:p{page}:l{limit}"

    if use_cache:
        cached = await cache_manager.get(cache_key)
        if cached:
            logger.info("⚡ 列表缓存命中")
            return json.loads(cached)

    stmt = select(Movie)
    order_exprs = []
    q_norm = None
    typo_threshold = float(getattr(settings, "search_typo_similarity_threshold", 0.16))

    if q:
        q_norm = re.sub(r"[\s\W_]+", "", q.lower())
        search_text = func.concat(
            func.coalesce(Movie.title, ""), " ",
            func.coalesce(Movie.summary, ""), " ",
            func.coalesce(Movie.director, ""), " ",
            func.coalesce(Movie.stars, ""),
        )
        similarity = func.similarity(search_text, q)
        like_pattern = f"%{q.strip()}%"
        norm_like_pattern = f"%{q_norm}%"
        norm_title = func.regexp_replace(func.lower(func.coalesce(Movie.title, "")), r"[[:space:][:punct:]_]+", "", "g")
        norm_summary = func.regexp_replace(func.lower(func.coalesce(Movie.summary, "")), r"[[:space:][:punct:]_]+", "", "g")
        title_word_sim = func.word_similarity(func.lower(func.coalesce(Movie.title, "")), q.lower())
        summary_word_sim = func.word_similarity(func.lower(func.coalesce(Movie.summary, "")), q.lower())

        stmt = stmt.where(or_(
            search_text.op("%")(q),
            Movie.title.ilike(like_pattern),
            Movie.summary.ilike(like_pattern),
            norm_title.ilike(norm_like_pattern),
            norm_summary.ilike(norm_like_pattern),
            title_word_sim >= typo_threshold,
            summary_word_sim >= typo_threshold,
            similarity >= typo_threshold,
        ))
        order_exprs.append(desc(title_word_sim))
        order_exprs.append(desc(similarity))

    if source:
        stmt = stmt.where(func.lower(Movie.source) == source.lower())
    if year:
        stmt = stmt.where(Movie.year == year)
    if min_rating is not None:
        stmt = stmt.where(Movie.rating >= min_rating)

    if sort_by == "year":
        order_exprs.append(desc(Movie.year))
    elif sort_by == "rating_count":
        order_exprs.append(desc(Movie.rating_count))
    else:
        order_exprs.append(desc(Movie.rating))

    stmt = stmt.order_by(*order_exprs)

    count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
    total = (await db.execute(count_stmt)).scalar() or 0

    stmt = stmt.offset((page - 1) * limit).limit(limit)
    result = await db.execute(stmt)
    items = result.scalars().all()

    if q and page == 1:
        existing_ids = {movie.id for movie in items}
        vec_movies = await _vector_fallback_search(q, db, limit, source, year, min_rating, existing_ids)
        items.extend(vec_movies)
        if len(items) > limit:
            items = items[:limit]

    if q and total == 0:
        vec_movies = await _vector_fallback_search(q, db, limit)
        if vec_movies:
            items = vec_movies
            total = len(vec_movies)

    response = MovieListResponse(
        total=total,
        page=page,
        limit=limit,
        total_pages=math.ceil(total / limit) if total > 0 else 0,
        has_next=page * limit < total,
        has_prev=page > 1,
        items=[MovieBase.model_validate(item) for item in items],
    )

    if use_cache:
        await cache_manager.set(cache_key, response.model_dump(), ttl=600)

    return response


@router.post("/movies/similar", tags=["电影检索"], response_model=RagSearchResponse)
async def movies_rag_search(
    request: RagSearchRequest,
    raw_request: Request,
    db: AsyncSession = Depends(get_db),
):
    """RAG 检索接口：仅执行 RRF + 专用 reranker，不经过 Agent。"""
    started_at = perf_counter()
    client_ip = raw_request.headers.get("X-Real-IP") or raw_request.client.host
    logger.info("📍 RAG 检索请求来源: %s | 查询: %s", client_ip, request.query)

    cache_key = f"rag:similar:{request.query}"
    cached = await cache_manager.get(cache_key)
    if cached:
        elapsed_ms = int((perf_counter() - started_at) * 1000)
        data = json.loads(cached)
        logger.info("⚡ RAG 缓存命中: %sms | hits=%d | 查询: %s", elapsed_ms, data.get("total", 0), request.query)
        return RagSearchResponse(**data)

    candidates = await search_movies_for_ai_mode(request.query, db)
    items = [MovieBase.model_validate(movie) for movie, _ in candidates]
    elapsed_ms = int((perf_counter() - started_at) * 1000)
    logger.info("✅ RAG 检索完成: %sms | hits=%d | 查询: %s", elapsed_ms, len(items), request.query)

    response = RagSearchResponse(
        status="success",
        query=request.query,
        total=len(items),
        items=items,
        timestamp=datetime.now().isoformat(),
    )
    await cache_manager.set(cache_key, response.model_dump(), ttl=600)
    return response


@router.get("/movies/{movie_id}", tags=["电影检索"], response_model=MovieDetail)
async def get_movie_detail(
    movie_id: int = Path(..., gt=0, description="电影ID"),
    db: AsyncSession = Depends(get_db),
):
    """获取电影详情"""
    cache_key = f"movie:detail:{movie_id}"
    cached = await cache_manager.get(cache_key)
    if cached:
        return json.loads(cached)

    stmt = select(Movie).where(Movie.id == movie_id)
    result = await db.execute(stmt)
    movie = result.scalar_one_or_none()

    if not movie:
        raise MovieNotFoundError(movie_id=movie_id)

    detail = MovieDetail.model_validate(movie)
    await cache_manager.set(cache_key, detail.model_dump(), ttl=3600)
    return detail


@router.get("/stats/platforms", tags=["数据统计"])
async def platform_statistics(db: AsyncSession = Depends(get_db)):
    """平台数据统计"""
    cache_key = "stats:platforms"
    cached = await cache_manager.get(cache_key)
    if cached:
        return json.loads(cached)

    try:
        count_expr = func.count().label("count")
        avg_rating_expr = func.round(cast(func.avg(Movie.rating), Numeric), 2).label("avg_rating")
        max_rating_expr = func.max(Movie.rating).label("max_rating")
        min_rating_expr = func.min(Movie.rating).label("min_rating")

        stmt = (
            select(Movie.source, count_expr, avg_rating_expr, max_rating_expr, min_rating_expr)
            .where(Movie.source.is_not(None))
            .group_by(Movie.source)
            .order_by(count_expr.desc())
        )
        result = await db.execute(stmt)
        stats = [dict(row._mapping) for row in result.all()]
        await cache_manager.set(cache_key, stats, ttl=1800)
        return stats
    except Exception as e:
        logger.exception("统计查询错误")
        raise DatabaseError(detail="统计数据查询失败")


@router.get("/surprise-me", tags=["特色功能"], response_model=MovieDetail)
async def random_recommendation(
    min_rating: float = Query(8.0, ge=0, le=10, description="最低评分"),
    db: AsyncSession = Depends(get_db),
):
    """随机电影推荐"""
    try:
        stmt = (
            select(Movie).where(Movie.rating >= min_rating).order_by(func.random()).limit(1)
        )
        result = await db.execute(stmt)
        movie = result.scalar_one_or_none()
        if not movie:
            raise MovieNotFoundError(detail=f"没有找到评分 >= {min_rating} 的电影")
        return MovieDetail.model_validate(movie)
    except MovieNotFoundError:
        raise
    except Exception as e:
        logger.error("推荐查询错误: %s", e)
        raise DatabaseError(detail="推荐查询失败")
