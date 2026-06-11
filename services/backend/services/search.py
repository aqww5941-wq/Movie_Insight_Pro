"""检索服务模块：语义搜索、关键词搜索、向量回退"""

import asyncio
import json
import logging
import os
import re
from http import HTTPStatus
from typing import Any, Dict, List, Optional

from sqlalchemy import desc, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from core.cache import cache_manager
from core.config import get_settings
from db.models import Movie
from schemas import MovieBase
from utils.helpers import AIAgent

logger = logging.getLogger(__name__)
settings = get_settings()


def _minmax_normalize(values: list[float]) -> list[float]:
    if not values:
        return []
    lo = min(values)
    hi = max(values)
    if hi - lo < 1e-9:
        return [1.0 for _ in values]
    return [(v - lo) / (hi - lo) for v in values]


def _deep_rerank_with_model(
    query: str,
    candidates: list[dict[str, Any]],
    model: str,
) -> tuple[list[int], dict[int, float]]:
    if not query or not candidates:
        return [], {}
    try:
        import dashscope
    except Exception:
        logger.warning("Deep rerank 失败: dashscope 不可用")
        return [], {}

    api_key = (os.getenv("DASHSCOPE_API_KEY") or "").strip()
    if not api_key:
        return [], {}

    documents = []
    index_to_id: dict[int, int] = {}
    for idx, item in enumerate(candidates):
        doc = (
            f"标题:{item.get('title', '')}\n"
            f"年份:{item.get('year', '')}\n"
            f"导演:{item.get('director', '')}\n"
            f"主演:{item.get('stars', '')}\n"
            f"简介:{(item.get('summary', '') or '')[:260]}"
        )
        documents.append(doc)
        index_to_id[idx] = int(item["id"])

    try:
        resp = dashscope.TextReRank.call(
            model=model,
            query=query,
            documents=documents,
            top_n=len(documents),
            api_key=api_key,
        )
        if resp.status_code != HTTPStatus.OK:
            logger.warning("Deep rerank 调用失败: %s", getattr(resp, "message", "unknown"))
            return [], {}
    except Exception as e:
        logger.warning("Deep rerank 异常: %s", e)
        return [], {}

    output = getattr(resp, "output", None)
    if output is None and isinstance(resp, dict):
        output = resp.get("output")
    results = []
    if isinstance(output, dict):
        results = output.get("results") or output.get("rerank_results") or []
    elif isinstance(output, list):
        results = output

    score_map: dict[int, float] = {}
    ordered_ids: list[int] = []
    for row in results:
        try:
            if not isinstance(row, dict):
                continue
            idx = row.get("index")
            if idx is None:
                idx = row.get("document_id")
            idx = int(idx)
            mid = index_to_id.get(idx)
            if mid is None:
                continue
            score = float(row.get("relevance_score", row.get("score", 0.0)))
            score = max(0.0, min(1.0, score))
            if mid not in score_map:
                score_map[mid] = score
                ordered_ids.append(mid)
        except Exception:
            continue

    if not ordered_ids:
        return [], {}
    return ordered_ids, score_map


async def build_embedding_vector(query: str) -> Optional[List[float]]:
    """生成查询向量（带 Redis 缓存，pgvector 需要一维 float 列表）"""
    if not query:
        return None

    cache_key = f"emb:{query}"
    try:
        cached = await cache_manager.get(cache_key)
        if cached:
            logger.info("⚡ Embedding 缓存命中: %s", query[:40])
            return json.loads(cached)
    except Exception:
        pass

    try:
        vector = AIAgent.generate_embedding(query)
        if not vector:
            return None

        if not isinstance(vector, list):
            logger.warning("Embedding 类型异常: %s", type(vector))
            return None

        cleaned = []
        for value in vector:
            if isinstance(value, (int, float)):
                cleaned.append(float(value))
            else:
                logger.warning("Embedding 元素类型异常: %s", type(value))
                return None

        if not cleaned:
            return None

        try:
            await cache_manager.set(cache_key, cleaned, ttl=86400)
        except Exception:
            pass

        return cleaned
    except Exception as e:
        logger.warning("向量生成失败: %s", e)
        return None


async def _vector_fallback_search(
    query: str,
    db: AsyncSession,
    limit: int = 20,
    source: Optional[str] = None,
    year: Optional[str] = None,
    min_rating: Optional[float] = None,
    exclude_ids: Optional[set[int]] = None,
) -> List[Movie]:
    """向量语义回退：当文本检索无结果或结果不足时，走 pgvector 相似度补召回。"""
    threshold = float(getattr(settings, "search_vector_similarity_threshold", 0.45))
    vector = await build_embedding_vector(query)
    if not vector:
        return []

    vector_str = "[" + ",".join(map(str, vector)) + "]"
    where_parts = ["embedding IS NOT NULL"]
    params: dict = {"qvec": vector_str, "lim": limit}

    if source:
        where_parts.append("LOWER(source) = LOWER(:source)")
        params["source"] = source
    if year:
        where_parts.append("year = :year")
        params["year"] = year
    if min_rating is not None:
        where_parts.append("rating >= :min_rating")
        params["min_rating"] = min_rating

    vector_sql = text(f"""
        SELECT id, COALESCE(1 - (embedding <=> CAST(:qvec AS vector)), 0.0) AS vec_score
        FROM movies
        WHERE {' AND '.join(where_parts)}
        ORDER BY embedding <=> CAST(:qvec AS vector)
        LIMIT :lim
    """)
    vec_rows = (await db.execute(vector_sql, params)).all()

    candidate_ids = [
        int(row.id)
        for row in vec_rows
        if float(row.vec_score or 0.0) >= threshold
        and (exclude_ids is None or int(row.id) not in exclude_ids)
    ]
    if not candidate_ids:
        return []

    vec_movies = (await db.execute(select(Movie).where(Movie.id.in_(candidate_ids)))).scalars().all()
    by_id = {m.id: m for m in vec_movies}
    return [by_id[mid] for mid in candidate_ids if mid in by_id]


async def search_movies_for_ai_mode(
    query: str,
    session: AsyncSession,
) -> List[tuple[Movie, float]]:
    """AI 模式专用检索：RRF 融合（向量 + 文本）+ 多层兜底。"""
    q = (query or "").strip()
    if not q:
        return []

    limit = max(1, int(getattr(settings, "ai_retrieval_limit", 8)))
    threshold = float(getattr(settings, "ai_similarity_threshold", 0.16))
    vector_weight = float(getattr(settings, "ai_hybrid_vector_weight", 0.65))
    text_weight = float(getattr(settings, "ai_hybrid_text_weight", 0.35))
    candidate_pool = max(limit, int(getattr(settings, "ai_hnsw_candidate_pool", 80)))
    rrf_k = max(1, int(getattr(settings, "ai_rrf_k", 60)))
    rrf_weight = float(getattr(settings, "ai_rrf_weight", 0.20))
    deep_rerank_enabled = bool(getattr(settings, "ai_deep_rerank_enabled", False))
    deep_rerank_topn = max(limit, int(getattr(settings, "ai_deep_rerank_topn", 24)))
    deep_rerank_weight = float(getattr(settings, "ai_deep_rerank_weight", 0.45))
    deep_rerank_timeout = max(3, int(getattr(settings, "ai_deep_rerank_timeout_seconds", 12)))
    deep_rerank_model = str(getattr(settings, "ai_deep_rerank_model", "gte-rerank-v2") or "gte-rerank-v2")

    # 精确标题短路
    SHORT_CIRCUIT_KEYWORDS = ["推荐", "找", "类似", "有没有", "想看", "氛围", "治愈", "烧脑", "片单", "好看"]
    if len(q) <= 40 and not any(kw in q for kw in SHORT_CIRCUIT_KEYWORDS):
        exact_stmt = (
            select(Movie)
            .where(Movie.title.ilike(f"%{q}%"))
            .order_by(desc(Movie.rating), desc(Movie.rating_count))
            .limit(limit)
        )
        exact_movies = (await session.execute(exact_stmt)).scalars().all()
        if exact_movies:
            logger.info("⚡ 精确标题短路命中: %s | hits=%d", q, len(exact_movies))
            return [(m, 1.0) for m in exact_movies]

    embedding = await build_embedding_vector(q)
    picked: List[tuple[Movie, float]] = []

    search_text = func.concat(
        func.coalesce(Movie.title, ""), " ",
        func.coalesce(Movie.summary, ""), " ",
        func.coalesce(Movie.director, ""), " ",
        func.coalesce(Movie.stars, ""),
    )
    similarity_expr = func.similarity(search_text, q)

    # RRF 融合（向量 + 文本）
    if embedding:
        vector_str = "[" + ",".join(map(str, embedding)) + "]"
        vector_sql = text("""
            SELECT id, COALESCE(1 - (embedding <=> CAST(:qvec AS vector)), 0.0) AS vec_score
            FROM movies
            WHERE embedding IS NOT NULL
            ORDER BY embedding <=> CAST(:qvec AS vector)
            LIMIT :pool
        """)
        vector_rows = (await session.execute(vector_sql, {"qvec": vector_str, "pool": candidate_pool})).all()
    else:
        vector_rows = []

    text_rows = (await session.execute(
        select(Movie.id, similarity_expr.label("text_score"))
        .order_by(desc(similarity_expr))
        .limit(candidate_pool)
    )).all()

    fused_map: Dict[int, dict] = {}
    for idx, row in enumerate(vector_rows, start=1):
        mid = int(row.id)
        item = fused_map.setdefault(mid, {"vec_rank": None, "text_rank": None, "vec_score": 0.0, "text_score": 0.0})
        item["vec_rank"] = idx
        item["vec_score"] = float(row.vec_score or 0.0)

    for idx, row in enumerate(text_rows, start=1):
        mid = int(row.id)
        item = fused_map.setdefault(mid, {"vec_rank": None, "text_rank": None, "vec_score": 0.0, "text_score": 0.0})
        item["text_rank"] = idx
        item["text_score"] = float(row.text_score or 0.0)

    ranked_ids: List[tuple[int, float, float]] = []
    for mid, item in fused_map.items():
        rrf_score = 0.0
        if item["vec_rank"] is not None:
            rrf_score += 1.0 / (rrf_k + item["vec_rank"])
        if item["text_rank"] is not None:
            rrf_score += 1.0 / (rrf_k + item["text_rank"])

        semantic_score = vector_weight * item["vec_score"] + text_weight * max(0.0, item["text_score"])
        final_score = semantic_score + rrf_weight * rrf_score
        ranked_ids.append((mid, final_score, semantic_score))

    ranked_ids.sort(key=lambda x: x[1], reverse=True)
    ranked_ids = ranked_ids[:deep_rerank_topn]

    if ranked_ids:
        movie_ids = [mid for mid, _, _ in ranked_ids]
        movies = (await session.execute(select(Movie).where(Movie.id.in_(movie_ids)))).scalars().all()
        movie_map = {m.id: m for m in movies}
        semantic_map = {mid: sem for mid, _, sem in ranked_ids}
        fused_map_score = {mid: fs for mid, fs, _ in ranked_ids}
        ordered_ids = [mid for mid, _, _ in ranked_ids]

        if deep_rerank_enabled:
            rerank_candidates = [
                {"id": mid, "title": movie_map[mid].title, "year": movie_map[mid].year,
                 "director": movie_map[mid].director, "stars": movie_map[mid].stars,
                 "summary": movie_map[mid].summary}
                for mid in ordered_ids if mid in movie_map
            ]
            try:
                llm_ordered_ids, llm_score_map = await asyncio.wait_for(
                    asyncio.to_thread(_deep_rerank_with_model, q, rerank_candidates, deep_rerank_model),
                    timeout=deep_rerank_timeout,
                )
            except Exception as e:
                logger.warning("Deep rerank 超时/异常，回退 RRF: %s", e)
                llm_ordered_ids, llm_score_map = [], {}
            if llm_ordered_ids:
                base_scores = [fused_map_score[mid] for mid in ordered_ids]
                base_norm = _minmax_normalize(base_scores)
                base_norm_map = {mid: base_norm[idx] for idx, mid in enumerate(ordered_ids)}
                blended = [
                    (mid, (1.0 - deep_rerank_weight) * base_norm_map[mid] + deep_rerank_weight * llm_score_map.get(mid, 0.0))
                    for mid in ordered_ids
                ]
                blended.sort(key=lambda x: x[1], reverse=True)
                ordered_ids = [mid for mid, _ in blended]
                logger.info("🧠 Deep rerank 生效: query=%s | candidates=%d | model=%s", q, len(ordered_ids), deep_rerank_model)

        ordered_ids = ordered_ids[:limit]
        for mid in ordered_ids:
            movie = movie_map.get(mid)
            if movie and semantic_map.get(mid, 0.0) >= threshold:
                picked.append((movie, semantic_map[mid]))
        if picked:
            logger.info("🧠 AI RRF 检索命中: %d 条 | query=%s | threshold=%.3f", len(picked), q, threshold)

    # 第二阶段：宽松检索
    if not picked:
        broad_rows = (await session.execute(
            select(Movie, similarity_expr).order_by(desc(similarity_expr), desc(Movie.rating), desc(Movie.rating_count)).limit(limit)
        )).all()
        picked = [(m, float(s)) for m, s in broad_rows if s is not None and float(s) >= threshold]

    # 关键词 token 兜底
    if not picked:
        stop_tokens = {"电影", "影视", "推荐", "类似", "想看", "有没有", "什么", "哪些", "一下", "作品"}
        zh_tokens = [t for t in re.findall(r"[一-鿿]{2,}", q) if t not in stop_tokens]
        en_tokens = [t for t in re.findall(r"[A-Za-z0-9]{2,}", q)]
        tokens = list(dict.fromkeys(zh_tokens + en_tokens))
        conditions = []
        for token in tokens[:6]:
            pattern = f"%{token}%"
            conditions.extend([Movie.title.ilike(pattern), Movie.summary.ilike(pattern)])
        if conditions:
            token_rows = (await session.execute(
                select(Movie).where(or_(*conditions)).order_by(desc(Movie.rating), desc(Movie.rating_count)).limit(min(limit, 6))
            )).scalars().all()
            picked = [(m, 0.88) for m in token_rows]

    # 最后一层：完整标题直接命中
    if not picked:
        like_rows = (await session.execute(
            select(Movie).where(Movie.title.ilike(f"%{q}%")).order_by(desc(Movie.rating), desc(Movie.rating_count)).limit(min(limit, 5))
        )).scalars().all()
        picked = [(m, 0.999) for m in like_rows]

    return picked


async def fetch_movies_by_titles(titles: List[str], session: AsyncSession, limit: int = 10) -> List[Movie]:
    """批量按标题模糊匹配"""
    cleaned = [t.strip() for t in titles if t and t.strip()]
    if not cleaned:
        return []
    conditions = [Movie.title.ilike(f"%{title}%") for title in cleaned[:limit]]
    stmt = select(Movie).where(or_(*conditions)).limit(limit)
    result = await session.execute(stmt)
    return result.scalars().all()


async def search_movies_by_keywords(
    query: str,
    session: AsyncSession,
    limit: int = 5,
) -> List[Movie]:
    """混合搜索：trigram 文本相似度 + 关键词兜底"""
    try:
        search_text = func.concat(func.coalesce(Movie.title, ""), " ", func.coalesce(Movie.summary, ""))
        similarity_expr = func.similarity(search_text, query)
        stmt = (
            select(Movie)
            .where(search_text.op("%")(query))
            .order_by(desc(similarity_expr), desc(Movie.rating))
            .limit(limit)
        )
        result = await session.execute(stmt)
        movies = result.scalars().all()
        if movies:
            return movies

        like_pattern = f"%{query.strip()}%"
        fallback_stmt = (
            select(Movie)
            .where(or_(Movie.title.ilike(like_pattern), Movie.summary.ilike(like_pattern)))
            .order_by(desc(Movie.rating), desc(Movie.rating_count))
            .limit(limit)
        )
        fallback_result = await session.execute(fallback_stmt)
        movies = fallback_result.scalars().all()
        if movies:
            return movies

        tokens = re.findall(r"[A-Za-z0-9]+", query)
        stopwords = {"recommend", "movie", "movies", "please", "show", "find"}
        keywords = [t for t in tokens if len(t) >= 2 and t.lower() not in stopwords]
        domain_terms = [
            "科幻", "悬疑", "喜剧", "爱情", "动作", "恐怖", "战争", "动画", "纪录片", "犯罪", "冒险",
            "高分", "经典", "治愈", "烧脑", "温馨", "电影", "导演", "演员",
        ]
        keywords.extend([term for term in domain_terms if term in query])
        keywords = list(dict.fromkeys(keywords))
        if keywords:
            conditions = []
            for kw in keywords[:5]:
                pattern = f"%{kw}%"
                conditions.extend([Movie.title.ilike(pattern), Movie.summary.ilike(pattern)])
            keyword_stmt = (
                select(Movie).where(or_(*conditions)).order_by(desc(Movie.rating), desc(Movie.rating_count)).limit(limit)
            )
            keyword_result = await session.execute(keyword_stmt)
            return keyword_result.scalars().all()

        return []
    except Exception as e:
        logger.error("搜索异常: %s", e)
        return []
