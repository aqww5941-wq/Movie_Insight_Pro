"""
Movie Insight Pro - 主应用入口
电影数据检索与 AI 推荐系统
Version: 4.0.0
"""
from fastapi import FastAPI, Query, Depends, Path, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.responses import StreamingResponse
from contextlib import asynccontextmanager
from pydantic import BaseModel, Field
import logging
import re
import asyncio
import json
import os
from urllib.parse import urlparse
from typing import Any, List, Optional
from datetime import datetime
from time import perf_counter
from http import HTTPStatus
import httpx

# 本地模块导入
from config import get_settings
from database import get_db, engine, AsyncSessionLocal
from models import Movie, Base
from cache import cache_manager
from exceptions import (
    MovieNotFoundError, 
    DatabaseError, 
    AIServiceError,
    http_exception_handler,
    validation_exception_handler,
    general_exception_handler
)

# 引入 SQLAlchemy 异步组件
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_, desc, text, cast, Numeric

# AI Agent
from agents.movie_agent import MovieAgent, MovieAgentError
from agents.skills import SkillRouter
from utils.helpers import AIAgent

# 配置加载
settings = get_settings()

# 日志配置
logging.basicConfig(
    level=getattr(logging, settings.log_level),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)
LOCAL_DB_NO_MATCH_TEXT = "本地数据库里没有符合条件的影视作品哦"
INTENT_RULE_ROUTER = SkillRouter()


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
    model: str
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

def _is_noise_message(text: str) -> bool:
    normalized = text.strip().strip('"\'').lower()
    if normalized == "request":
        return True
    if normalized.startswith("keyerror") and "request" in normalized:
        return True
    return False

def _extract_exception_message(exc: Exception) -> str:
    messages = []
    current = exc
    visited = set()

    while current and id(current) not in visited:
        visited.add(id(current))
        text = str(current).strip()
        if text and not _is_noise_message(text):
            messages.append(text)
        current = current.__cause__ or current.__context__

    if messages:
        return messages[-1]
    return "AI 上游服务调用失败（可能是网络、密钥或配额问题）"


def _sse_event(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _is_allowed_image_host(raw_url: str) -> bool:
    try:
        parsed = urlparse(raw_url)
        host = (parsed.hostname or "").lower()
        return host.endswith("doubanio.com")
    except Exception:
        return False


# ==========================================
# 生命周期管理
# ==========================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    # 启动时执行
    logger.info("🚀 应用启动中...")
    
    # 初始化缓存
    await cache_manager.initialize()
    
    # 检查数据库连接
    try:
        async with engine.connect() as conn:
            await conn.execute(select(1))
        logger.info("✅ PostgreSQL 数据库连接就绪")
    except Exception as e:
        logger.error(f"❌ 数据库连接失败: {e}")
    
    # AI Agent 预热
    if app.state.movie_agent:
        try:
            app.state.movie_agent.ask("hi")
            logger.info("🤖 AI Agent 预热完成")
        except Exception as e:
            logger.warning(f"⚠️ AI Agent 预热失败: {e}")
    
    logger.info("✨ 应用启动完成")
    
    yield
    
    # 关闭时执行
    logger.info("🔄 应用关闭中...")
    await cache_manager.close()
    await engine.dispose()
    logger.info("👋 应用已关闭")


# ==========================================
# FastAPI 应用初始化
# ==========================================
app = FastAPI(
    title=settings.app_name,
    description="专业的电影数据检索与 AI 推荐系统 (PostgreSQL + Redis + AI)",
    version=settings.app_version,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    root_path=settings.root_path,
    root_path_in_servers=True
)

# 初始化 AI Agent
try:
    app.state.movie_agent = MovieAgent()
    logger.info("✅ AI Agent 初始化成功")
except Exception as e:
    logger.error(f"❌ AI Agent 初始化失败: {e}", exc_info=True)
    app.state.movie_agent = None

# CORS 配置
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 异常处理器注册
app.add_exception_handler(RequestValidationError, validation_exception_handler)
app.add_exception_handler(Exception, general_exception_handler)


# ==========================================
# 数据模型 (Pydantic)
# ==========================================
class MovieBase(BaseModel):
    """电影基础模型"""
    id: int
    title: str
    year: Optional[str] = None
    rating: Optional[float] = None             
    rating_count: Optional[int] = None
    source: Optional[str] = None
    director: Optional[str] = None
    stars: Optional[str] = None
    cover_url: Optional[str] = None
    
    class Config:
        from_attributes = True


class MovieDetail(MovieBase):
    """电影详情模型"""
    url: Optional[str] = None
    summary: Optional[str] = None
    created_at: Optional[datetime] = None


class MovieListResponse(BaseModel):
    """电影列表响应"""
    total: int
    page: int
    limit: int
    total_pages: int
    has_next: bool = False
    has_prev: bool = False
    items: List[MovieBase]


class AgentChatRequest(BaseModel):
    """AI 对话请求"""
    query: str = Field(..., min_length=1, max_length=500, example="推荐几部高分科幻电影")


class AgentChatResponse(BaseModel):
    """AI 对话响应"""
    status: str
    agent_answer: str
    movie_titles: Optional[List[MovieBase]] = None
    timestamp: str


class RagSearchRequest(BaseModel):
    """RAG 检索请求（不经过 Agent）"""
    query: str = Field(..., min_length=1, max_length=500, example="给我推荐和《Just Mercy》类似的电影")


class RagSearchResponse(BaseModel):
    """RAG 检索响应（不经过 Agent）"""
    status: str
    query: str
    total: int
    items: List[MovieBase]
    timestamp: str


class HealthResponse(BaseModel):
    """健康检查响应"""
    status: str
    database: str
    cache: str
    ai_agent: str
    total_movies: int
    timestamp: str


# ==========================================
# 工具函数
# ==========================================
def extract_movie_titles(text: str) -> List[str]:
    """从文本中提取电影名称"""
    titles = []
    
    # 匹配 《电影名》
    pattern1 = r'《([^》]+)》'
    titles.extend(re.findall(pattern1, text))
    
    # 匹配 **电影名**
    pattern2 = r'\*\*《?([^*》]+)》?\*\*'
    matches = re.findall(pattern2, text)
    titles.extend([m.strip() for m in matches if m.strip() not in titles])
    
    return list(set(titles))


def is_retrieval_query(query: str) -> bool:
    """判断是否为检索意图，命中后走快速查询路径。"""
    if not query:
        return False

    q = query.lower().strip()
    retrieval_keywords = [
        "推荐", "找", "搜索", "检索", "有没有", "想看", "类似", "高分", "评分", "片单",
        "科幻", "悬疑", "喜剧", "爱情", "动作", "恐怖", "战争", "动画", "纪录片", "电影"
    ]
    return any(word in q for word in retrieval_keywords)


def is_smalltalk_query(query: str) -> bool:
    """识别无需调用大模型的闲聊问题。"""
    if not query:
        return False

    q = query.strip().lower()
    keywords = [
        "你是谁", "你是干嘛的", "你能做什么", "你会什么", "自我介绍",
        "你可以干什么", "你能干什么", "你能做啥", "你可以做什么", "你能帮我什么",
        "who are you", "what can you do", "introduce yourself"
    ]
    return any(k in q for k in keywords)


def build_smalltalk_answer(query: str) -> str:
    """闲聊问题本地回复，降低延迟并提升稳定性。"""
    _ = query
    return (
        "我是 Movie Insight Pro 的电影助手。\n"
        "我可以帮你：\n"
        "1. 按关键词快速检索电影（如：高分科幻、悬疑烧脑）。\n"
        "2. 按条件筛选（年份、评分、平台）。\n"
        "3. 做相似影片推荐（如：和《肖申克的救赎》类似）。\n"
        "你可以直接说：推荐几部高分悬疑电影。"
    )


def get_ai_timeout_seconds(query: str) -> int:
    """根据意图复杂度动态计算 AI 超时时间。"""
    base_timeout = max(1, int(getattr(settings, "ai_request_timeout_seconds", 30)))
    extra_timeout = max(0, int(getattr(settings, "ai_multi_intent_extra_timeout_seconds", 20)))
    hard_cap = max(10, int(getattr(settings, "ai_hard_timeout_cap_seconds", 75)))
    labels = INTENT_RULE_ROUTER.route_rule_multi(query or "")
    active_labels = [label for label in labels if label != "general"]
    is_multi_intent = len(active_labels) >= 2
    if is_multi_intent:
        # 多意图链路按意图数扩展超时，对 comparison 场景再额外放宽。
        timeout = base_timeout + extra_timeout * (len(active_labels) - 1)
        if "comparison" in active_labels:
            timeout += max(40, extra_timeout * 2)
        capped = min(timeout, hard_cap)
        if capped < timeout:
            logger.info("🧭 AI 超时阈值被硬上限截断: raw=%ss cap=%ss | labels=%s", timeout, hard_cap, ",".join(active_labels))
        return capped
    capped = min(base_timeout, hard_cap)
    if capped < base_timeout:
        logger.info("🧭 AI 超时阈值被硬上限截断: raw=%ss cap=%ss | labels=single", base_timeout, hard_cap)
    return capped


def should_use_fast_retrieval(query: str) -> bool:
    """仅对单意图检索问题启用快速检索短路。"""
    q = (query or "").lower().strip()
    if not q:
        return False

    # 对比类问题必须走 AI 推理链路，避免被快速检索模板短路。
    comparison_patterns = [
        r"\bvs\b",
        r"\bversus\b",
        r"对比",
        r"比较",
        r"区别",
        r"差异",
        r"与.{0,20}(比|对比|比较)",
        r"和.{0,20}(比|对比|比较)",
    ]
    if any(re.search(pattern, q, re.IGNORECASE) for pattern in comparison_patterns):
        return False

    labels = INTENT_RULE_ROUTER.route_rule_multi(query or "")
    if "comparison" in labels:
        return False
    if len(labels) >= 2 and "general" not in labels:
        return False
    return is_retrieval_query(query)


def format_fast_retrieval_answer(query: str, movies: List[Movie]) -> str:
    """构建快速检索路径的文本回答。"""
    lines = [f"已为你快速检索到 {len(movies)} 部相关电影："]
    for idx, movie in enumerate(movies, start=1):
        rating_text = f"{movie.rating}" if movie.rating is not None else "暂无"
        year_text = movie.year or "未知年份"
        source_text = movie.source or "unknown"
        lines.append(f"{idx}. 《{movie.title}》({year_text}) | 评分: {rating_text} | 来源: {source_text}")

    lines.append("你可以继续说：再来 5 部，或只看某年份/某类型。")
    return "\n".join(lines)


def format_timeout_fallback_answer(query: str, movies: List[Movie]) -> str:
    """构建 AI 超时后的兜底文案，避免与快速检索短路结果混淆。"""
    if not movies:
        return "AI 推理超时，请稍后重试或缩短问题。"
    base = format_fast_retrieval_answer(query, movies)
    return "AI 推理超时，以下为本地候选结果（非完整对比分析）：\n" + base


def extract_comparison_target(query: str) -> Optional[str]:
    text = (query or "").strip()
    if not text:
        return None
    patterns = [
        r"(?:与|和)([^，。；;,.]{1,30}?)(?:对比|比较|相比)",
        r"(?:对比|比较)([^，。；;,.]{1,30})",
    ]
    for pattern in patterns:
        m = re.search(pattern, text)
        if m:
            target = m.group(1).strip("《》\"' ")
            if target:
                return target
    return None


def extract_recommendation_part(query: str) -> str:
    text = (query or "").strip()
    if not text:
        return ""
    text = re.sub(r"[，,。；;]?\s*(?:并)?(?:与|和)[^，。；;,.]{1,30}?(?:对比|比较|相比)", "", text)
    text = re.sub(r"[，,。；;]?\s*(?:对比|比较).*$", "", text)
    return text.strip() or query


async def build_timeout_fallback_movies(query: str, labels: List[str], db: AsyncSession, limit: int = 5) -> List[Movie]:
    # 多意图对比场景：优先用“推荐片段 + 对比对象”组合召回，避免整句污染检索。
    if "comparison" in labels and len(labels) >= 2:
        rec_query = extract_recommendation_part(query)
        target = extract_comparison_target(query)

        collected: List[Movie] = []
        seen_ids = set()
        for q in [rec_query, target]:
            if not q:
                continue
            rows = await search_movies_fast(q, db, limit=limit)
            for m in rows:
                if m.id not in seen_ids:
                    seen_ids.add(m.id)
                    collected.append(m)
                    if len(collected) >= limit:
                        return collected
        if collected:
            return collected
    return await search_movies_fast(query, db, limit=limit)


def build_local_recommendation_fallback(candidates: List[dict]) -> str:
    """当大模型超时时的兜底推荐文案（仅本地候选）。"""
    lines = ["已基于本地数据库为你找到这些相关影视作品："]
    for idx, item in enumerate(candidates[:5], start=1):
        title = item.get("title") or "未知片名"
        year = item.get("year") or "未知年份"
        rating = item.get("rating") if item.get("rating") is not None else "暂无"
        similarity = item.get("similarity")
        sim_text = f"{similarity:.3f}" if isinstance(similarity, (int, float)) else "N/A"
        lines.append(f"{idx}. 《{title}》({year}) | 评分: {rating} | 相似度: {sim_text}")
    return "\n".join(lines)


def to_grounding_candidates(candidates: List[tuple[Movie, float]]) -> List[dict]:
    payload = []
    for movie, score in candidates:
        payload.append({
            "id": movie.id,
            "title": movie.title,
            "year": movie.year,
            "rating": movie.rating,
            "rating_count": movie.rating_count,
            "source": movie.source,
            "director": movie.director,
            "stars": movie.stars,
            "summary": movie.summary,
            "cover_url": movie.cover_url,
            "similarity": float(score),
        })
    return payload


async def search_movies_for_ai_mode(
    query: str,
    session: AsyncSession
) -> List[tuple[Movie, float]]:
    """
    AI 模式专用检索：必须先从本地库按相似度召回，达阈值才允许推荐。
    """
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
    embedding = build_embedding_vector(q)
    picked: List[tuple[Movie, float]] = []

    search_text = func.concat(
        func.coalesce(Movie.title, ""),
        " ",
        func.coalesce(Movie.summary, ""),
        " ",
        func.coalesce(Movie.director, ""),
        " ",
        func.coalesce(Movie.stars, ""),
    )
    similarity_expr = func.similarity(search_text, q)

    # 主路径：RRF 融合（向量召回 + 文本召回）
    if embedding:
        vector_str = "[" + ",".join(map(str, embedding)) + "]"
        vector_sql = text("""
            SELECT
                id,
                COALESCE(1 - (embedding <=> CAST(:qvec AS vector)), 0.0) AS vec_score
            FROM movies
            WHERE embedding IS NOT NULL
            ORDER BY embedding <=> CAST(:qvec AS vector)
            LIMIT :pool
        """)
        vector_rows = (await session.execute(
            vector_sql,
            {"qvec": vector_str, "pool": candidate_pool}
        )).all()
    else:
        vector_rows = []

    text_rows = (await session.execute(
        select(Movie.id, similarity_expr.label("text_score"))
        .order_by(desc(similarity_expr))
        .limit(candidate_pool)
    )).all()

    fused_map: dict[int, dict] = {}
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
        vec_rank = item["vec_rank"]
        text_rank = item["text_rank"]
        vec_score = item["vec_score"]
        text_score = max(0.0, item["text_score"])

        rrf_score = 0.0
        if vec_rank is not None:
            rrf_score += 1.0 / (rrf_k + vec_rank)
        if text_rank is not None:
            rrf_score += 1.0 / (rrf_k + text_rank)

        semantic_score = vector_weight * vec_score + text_weight * text_score
        final_score = semantic_score + rrf_weight * rrf_score
        ranked_ids.append((mid, final_score, semantic_score))

    ranked_ids.sort(key=lambda x: x[1], reverse=True)
    ranked_ids = ranked_ids[:deep_rerank_topn]

    if ranked_ids:
        movie_ids = [mid for mid, _, _ in ranked_ids]
        movies = (await session.execute(select(Movie).where(Movie.id.in_(movie_ids)))).scalars().all()
        movie_map = {m.id: m for m in movies}
        semantic_map = {mid: semantic_score for mid, _, semantic_score in ranked_ids}
        fused_map_score = {mid: fused_score for mid, fused_score, _ in ranked_ids}
        ordered_ids = [mid for mid, _, _ in ranked_ids]

        # 二阶段重排：RRF 后使用深度模型做相关性重排
        if deep_rerank_enabled:
            rerank_candidates = []
            for mid in ordered_ids:
                movie = movie_map.get(mid)
                if not movie:
                    continue
                rerank_candidates.append({
                    "id": mid,
                    "title": movie.title,
                    "year": movie.year,
                    "director": movie.director,
                    "stars": movie.stars,
                    "summary": movie.summary,
                })
            try:
                llm_ordered_ids, llm_score_map = await asyncio.wait_for(
                    asyncio.to_thread(
                        _deep_rerank_with_model,
                        q,
                        rerank_candidates,
                        deep_rerank_model
                    ),
                    timeout=deep_rerank_timeout
                )
            except Exception as e:
                logger.warning("Deep rerank 超时/异常，回退 RRF: %s", e)
                llm_ordered_ids, llm_score_map = [], {}
            if llm_ordered_ids:
                base_scores = [fused_map_score[mid] for mid in ordered_ids]
                base_norm = _minmax_normalize(base_scores)
                base_norm_map = {mid: base_norm[idx] for idx, mid in enumerate(ordered_ids)}
                blended: list[tuple[int, float]] = []
                for mid in ordered_ids:
                    llm_score = llm_score_map.get(mid, 0.0)
                    final_blend = (1.0 - deep_rerank_weight) * base_norm_map[mid] + deep_rerank_weight * llm_score
                    blended.append((mid, final_blend))
                blended.sort(key=lambda x: x[1], reverse=True)
                ordered_ids = [mid for mid, _ in blended]
                logger.info(
                    "🧠 Deep rerank 生效: query=%s | candidates=%d | model=%s",
                    q,
                    len(ordered_ids),
                    deep_rerank_model
                )

        ordered_ids = ordered_ids[:limit]
        for mid in ordered_ids:
            movie = movie_map.get(mid)
            semantic_score = semantic_map.get(mid, 0.0)
            if movie and semantic_score >= threshold:
                picked.append((movie, semantic_score))
        if picked:
            logger.info(
                "🧠 AI RRF 检索命中: %d 条 | query=%s | threshold=%.3f",
                len(picked), q, threshold
            )

    # 第二阶段：放宽检索条件（不使用 % 操作符），再按阈值过滤
    if not picked:
        broad_stmt = (
            select(Movie, similarity_expr)
            .order_by(desc(similarity_expr), desc(Movie.rating), desc(Movie.rating_count))
            .limit(limit)
        )
        broad_rows = (await session.execute(broad_stmt)).all()
        picked = [
            (movie, float(score))
            for movie, score in broad_rows
            if score is not None and float(score) >= threshold
        ]

    # 兜底：标题 token 命中时可视为高相关
    if not picked:
        stop_tokens = {"电影", "影视", "推荐", "类似", "想看", "有没有", "什么", "哪些", "一下", "作品"}
        zh_tokens = [t for t in re.findall(r"[\u4e00-\u9fff]{2,}", q) if t not in stop_tokens]
        en_tokens = [t for t in re.findall(r"[A-Za-z0-9]{2,}", q)]
        tokens = list(dict.fromkeys(zh_tokens + en_tokens))
        conditions = []
        for token in tokens[:6]:
            pattern = f"%{token}%"
            conditions.extend([Movie.title.ilike(pattern), Movie.summary.ilike(pattern)])

        if conditions:
            token_stmt = (
                select(Movie)
                .where(or_(*conditions))
                .order_by(desc(Movie.rating), desc(Movie.rating_count))
                .limit(min(limit, 6))
            )
            token_rows = (await session.execute(token_stmt)).scalars().all()
            picked = [(movie, 0.88) for movie in token_rows]

    # 最后一层兜底：完整标题直接命中
    if not picked:
        like_stmt = (
            select(Movie)
            .where(Movie.title.ilike(f"%{q}%"))
            .order_by(desc(Movie.rating), desc(Movie.rating_count))
            .limit(min(limit, 5))
        )
        like_rows = (await session.execute(like_stmt)).scalars().all()
        picked = [(movie, 0.999) for movie in like_rows]

    return picked


async def search_movies_fast(query: str, session: AsyncSession, limit: int = 5) -> List[Movie]:
    """轻量快速检索：优先关键词 ILIKE，避免 trigram/向量导致的慢查询。"""
    q = (query or "").strip()
    if not q:
        return []

    domain_terms = [
        "科幻", "悬疑", "喜剧", "爱情", "动作", "恐怖", "战争", "动画", "纪录片", "犯罪", "冒险",
        "高分", "经典", "治愈", "烧脑", "温馨", "电影", "导演", "演员"
    ]
    keywords = [term for term in domain_terms if term in q]

    # 补充英文/数字 token
    keywords.extend([t for t in re.findall(r"[A-Za-z0-9]+", q) if len(t) >= 2])
    keywords = list(dict.fromkeys(keywords))

    conditions = []
    for kw in keywords[:5]:
        pattern = f"%{kw}%"
        conditions.extend([Movie.title.ilike(pattern), Movie.summary.ilike(pattern)])

    # 若没有可提取关键词，退化成整句模糊匹配
    if not conditions:
        pattern = f"%{q}%"
        conditions = [Movie.title.ilike(pattern), Movie.summary.ilike(pattern)]

    stmt = (
        select(Movie)
        .where(or_(*conditions))
        .order_by(desc(Movie.rating), desc(Movie.rating_count))
        .limit(limit)
    )
    result = await session.execute(stmt)
    return result.scalars().all()


async def fetch_movies_by_titles(titles: List[str], session: AsyncSession, limit: int = 10) -> List[Movie]:
    """批量按标题模糊匹配，减少逐条查询的数据库往返。"""
    cleaned = [t.strip() for t in titles if t and t.strip()]
    if not cleaned:
        return []

    conditions = [Movie.title.ilike(f"%{title}%") for title in cleaned[:limit]]
    stmt = select(Movie).where(or_(*conditions)).limit(limit)
    result = await session.execute(stmt)
    return result.scalars().all()


def build_embedding_vector(query: str) -> Optional[List[float]]:
    """生成查询向量（pgvector 需要一维 float 列表）"""
    if not query:
        return None
    try:
        vector = AIAgent.generate_embedding(query)
        if not vector:
            return None

        if not isinstance(vector, list):
            logger.warning(f"Embedding 类型异常: {type(vector)}")
            return None

        cleaned = []
        for value in vector:
            if isinstance(value, (int, float)):
                cleaned.append(float(value))
            else:
                logger.warning(f"Embedding 元素类型异常: {type(value)}")
                return None

        if not cleaned:
            return None

        return cleaned
    except Exception as e:
        logger.warning(f"向量生成失败: {e}")
        return None


async def search_movies_by_keywords(
    query: str, 
    session: AsyncSession, 
    limit: int = 5
) -> List[Movie]:
    """
    混合搜索：向量搜索 + 全文检索
    """
    try:
        search_text = func.concat(func.coalesce(Movie.title, ""), " ", func.coalesce(Movie.summary, ""))
        similarity_expr = func.similarity(search_text, query)
        # 优先保证接口稳定：这里先使用文本相似度检索。
        # 说明：ORM + pgvector 在当前环境存在参数绑定异常，向量检索保留在 agents/tools.py 的原生 SQL 工具中。
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

        # 兜底1：宽松模糊匹配（适合“推荐几部高分科幻电影”这类自然语言）
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

        # 兜底2：抽取关键词再匹配，进一步提高召回率
        tokens = re.findall(r"[A-Za-z0-9]+", query)
        stopwords = {"recommend", "movie", "movies", "please", "show", "find"}
        keywords = [t for t in tokens if len(t) >= 2 and t.lower() not in stopwords]

        # 中文查询常常无法按空格分词，补充领域词典匹配。
        domain_terms = [
            "科幻", "悬疑", "喜剧", "爱情", "动作", "恐怖", "战争", "动画", "纪录片", "犯罪", "冒险",
            "高分", "经典", "治愈", "烧脑", "温馨", "电影", "导演", "演员"
        ]
        keywords.extend([term for term in domain_terms if term in query])
        keywords = list(dict.fromkeys(keywords))
        if keywords:
            conditions = []
            for kw in keywords[:5]:
                pattern = f"%{kw}%"
                conditions.extend([Movie.title.ilike(pattern), Movie.summary.ilike(pattern)])

            keyword_stmt = (
                select(Movie)
                .where(or_(*conditions))
                .order_by(desc(Movie.rating), desc(Movie.rating_count))
                .limit(limit)
            )
            keyword_result = await session.execute(keyword_stmt)
            return keyword_result.scalars().all()

        return []
    except Exception as e:
        logger.error(f"搜索异常: {e}")
        return []


# ==========================================
# 路由定义
# ==========================================


@app.get("/", tags=["系统"])
async def root():
    """API 根路径"""
    return {
        "name": settings.app_name,
        "version": settings.app_version,
        "status": "running",
        "docs": "/docs"
    }


@app.get("/health", tags=["系统"], response_model=HealthResponse)
async def health_check(db: AsyncSession = Depends(get_db)):
    """健康检查接口"""
    # 检查缓存
    cache_status = "connected" if cache_manager._initialized else "disconnected"
    
    # 检查数据库
    try:
        stmt = select(func.count(Movie.id))
        result = await db.execute(stmt)
        count = result.scalar() or 0
        db_status = "connected"
    except Exception as e:
        logger.error(f"健康检查数据库错误: {e}")
        count = 0
        db_status = f"error: {str(e)}"
    
    return HealthResponse(
        status="healthy" if db_status == "connected" else "degraded",
        database=db_status,
        cache=cache_status,
        ai_agent="enabled" if app.state.movie_agent else "disabled",
        total_movies=count,
        timestamp=datetime.now().isoformat()
    )


@app.get("/images/proxy", tags=["系统"])
async def image_proxy(url: str = Query(..., min_length=8, max_length=2048)):
    """图片代理：为受防盗链限制的图片提供后端中转。"""
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
                headers={"Cache-Control": "public, max-age=86400"}
            )
    except DatabaseError:
        raise
    except Exception as e:
        logger.warning("图片代理失败: %s", e)
        raise DatabaseError(detail="图片代理失败")


@app.post("/agent/chat", tags=["AI 助手"], response_model=AgentChatResponse)
async def chat_with_agent(
    request: AgentChatRequest,
    raw_request: Request,
    db: AsyncSession = Depends(get_db)
):
    """
    🤖 AI 智能助手对话接口
    支持电影推荐、查询、对比等功能
    """
    if not app.state.movie_agent:
        raise AIServiceError()
    
    started_at = perf_counter()
    client_ip = raw_request.headers.get("X-Real-IP") or raw_request.client.host
    logger.info(f"📍 AI 请求来源: {client_ip} | 查询: {request.query}")
    
    try:
        route_labels = INTENT_RULE_ROUTER.route_rule_multi(request.query or "")
        fast_path = False
        logger.info(
            "🧭 chat route decision | fast_path=%s | labels=%s | query=%s",
            fast_path,
            ",".join(route_labels),
            request.query
        )

        # 检查缓存：加入路由决策与模型信息，避免误命中历史/异构策略结果
        model_name = ""
        try:
            model_name = str(getattr(getattr(app.state.movie_agent, "llm", None), "model_name", "") or "")
        except Exception:
            model_name = ""
        labels_key = ",".join(route_labels)
        cache_key = f"ai:chat:react:v7:{model_name}:fast{int(fast_path)}:labels[{labels_key}]:q:{request.query}"
        cached = await cache_manager.get(cache_key)
        if cached:
            logger.info(f"⚡ 缓存命中: {request.query}")
            import json
            return AgentChatResponse(**json.loads(cached))

        # 闲聊快速路径：不调用外部模型，避免无必要的长耗时请求。
        if is_smalltalk_query(request.query):
            response = AgentChatResponse(
                status="success",
                agent_answer=build_smalltalk_answer(request.query),
                movie_titles=None,
                timestamp=datetime.now().isoformat()
            )
            await cache_manager.set(cache_key, response.model_dump(), ttl=3600)
            elapsed_ms = int((perf_counter() - started_at) * 1000)
            logger.info(f"⚡ 闲聊快速回复: {elapsed_ms}ms | 查询: {request.query}")
            return response

        # 检索类问题优先走本地快速检索，避免大模型超时
        if fast_path:
            fast_movies = await search_movies_fast(request.query, db, limit=5)
            if fast_movies:
                fast_text = format_fast_retrieval_answer(request.query, fast_movies)
                response = AgentChatResponse(
                    status="success",
                    agent_answer=fast_text,
                    movie_titles=[MovieBase.model_validate(movie) for movie in fast_movies],
                    timestamp=datetime.now().isoformat()
                )
                await cache_manager.set(cache_key, response.model_dump(), ttl=600)
                elapsed_ms = int((perf_counter() - started_at) * 1000)
                logger.info(f"⚡ 快速检索回复: {elapsed_ms}ms | 查询: {request.query}")
                return response

        # AI 模式：走 ReAct 工具调用链（非流式）
        ai_response = await asyncio.to_thread(
            app.state.movie_agent.ask,
            request.query
        )

        logger.info(f"🤖 AI 响应: {len(ai_response)} 字符")

        extracted_titles = extract_movie_titles(ai_response)
        matched_movies = await fetch_movies_by_titles(extracted_titles, db, limit=10) if extracted_titles else []
        movie_results = [MovieBase.model_validate(movie) for movie in matched_movies]

        response = AgentChatResponse(
            status="success",
            agent_answer=ai_response,
            movie_titles=movie_results if movie_results else None,
            timestamp=datetime.now().isoformat()
        )
        await cache_manager.set(cache_key, response.model_dump(), ttl=86400)

        elapsed_ms = int((perf_counter() - started_at) * 1000)
        logger.info(f"✅ AI 对话完成: {elapsed_ms}ms | 查询: {request.query}")
        return response
        
    except MovieAgentError as e:
        detail = str(e)
        if _is_noise_message(detail):
            detail = "AI 上游服务调用失败（可能是网络、密钥或配额问题）"
        logger.error(f"AI 处理异常: {detail}", exc_info=True)
        raise AIServiceError(detail=f"AI 处理失败: {detail}")
    except Exception as e:
        root_message = _extract_exception_message(e)
        logger.error(f"AI 处理异常: {root_message}", exc_info=True)
        raise AIServiceError(detail=f"AI 处理失败: {root_message}")


@app.post("/agent/chat/stream", tags=["AI 助手"])
async def chat_with_agent_stream(
    request: AgentChatRequest,
    raw_request: Request
):
    """
    🤖 AI 流式对话接口（SSE）
    用于前端实时展示 token，并在完成后附带相关电影结果。
    """
    if not app.state.movie_agent:
        raise AIServiceError()

    started_at = perf_counter()
    client_ip = raw_request.headers.get("X-Real-IP") or raw_request.client.host
    logger.info(f"📍 AI 流式请求来源: {client_ip} | 查询: {request.query}")

    async def event_generator():
        ai_task: Optional[asyncio.Task] = None
        try:
            # 闲聊快速路径：流式分片返回本地答案
            if is_smalltalk_query(request.query):
                quick_text = build_smalltalk_answer(request.query)
                for i in range(0, len(quick_text), 24):
                    yield _sse_event("chunk", {"delta": quick_text[i:i + 24]})
                    await asyncio.sleep(0.01)

                response_payload = {
                    "status": "success",
                    "agent_answer": quick_text,
                    "movie_titles": None,
                    "timestamp": datetime.now().isoformat()
                }
                yield _sse_event("done", response_payload)
                elapsed_ms = int((perf_counter() - started_at) * 1000)
                logger.info(f"⚡ 流式闲聊完成: {elapsed_ms}ms | 查询: {request.query}")
                return

            # 检索类问题优先走本地快速检索，避免大模型超时
            route_labels = INTENT_RULE_ROUTER.route_rule_multi(request.query or "")
            fast_path = False
            logger.info(
                "🧭 stream route decision | fast_path=%s | labels=%s | query=%s",
                fast_path,
                ",".join(route_labels),
                request.query
            )
            if fast_path:
                async with AsyncSessionLocal() as db:
                    fast_movies = await search_movies_fast(request.query, db, limit=5)
                if fast_movies:
                    fast_text = format_fast_retrieval_answer(request.query, fast_movies)
                    for i in range(0, len(fast_text), 24):
                        yield _sse_event("chunk", {"delta": fast_text[i:i + 24]})
                        await asyncio.sleep(0.01)

                    response_payload = {
                        "status": "success",
                        "agent_answer": fast_text,
                        "movie_titles": [MovieBase.model_validate(m).model_dump() for m in fast_movies],
                        "timestamp": datetime.now().isoformat()
                    }
                    yield _sse_event("done", response_payload)
                    elapsed_ms = int((perf_counter() - started_at) * 1000)
                    logger.info(f"⚡ 流式快速检索完成: {elapsed_ms}ms | 查询: {request.query}")
                    return

            # 多意图问题走流式执行（单请求持续心跳，最终 done）
            labels = route_labels
            if len(labels) >= 2 and "general" not in labels:
                logger.info("📊 多意图流式执行[%s] | 查询: %s", ",".join(labels), request.query)
                # 先发一个进度块，避免长时间无数据导致链路被中间层断开
                yield _sse_event("chunk", {"delta": "正在进行多意图分析，请稍候...\n"})

                ai_task = asyncio.create_task(
                    asyncio.to_thread(
                        app.state.movie_agent.ask,
                        request.query
                    )
                )
                while True:
                    try:
                        ai_response = await asyncio.wait_for(asyncio.shield(ai_task), timeout=1)
                        break
                    except asyncio.TimeoutError:
                        # 心跳，保持 SSE 活跃
                        yield _sse_event("chunk", {"delta": ""})

                extracted_titles = extract_movie_titles(ai_response)
                if extracted_titles:
                    async with AsyncSessionLocal() as db:
                        matched_movies = await fetch_movies_by_titles(extracted_titles, db, limit=10)
                else:
                    matched_movies = []
                movie_results = [MovieBase.model_validate(movie) for movie in matched_movies]
                response_payload = {
                    "status": "success",
                    "agent_answer": ai_response,
                    "movie_titles": [m.model_dump() for m in movie_results] if movie_results else None,
                    "timestamp": datetime.now().isoformat()
                }
                yield _sse_event("done", response_payload)
                elapsed_ms = int((perf_counter() - started_at) * 1000)
                logger.info(f"✅ 多意图流式完成: {elapsed_ms}ms | 查询: {request.query}")
                return

            ai_response = await asyncio.to_thread(
                app.state.movie_agent.ask,
                request.query
            )

            ai_response = (ai_response or "").strip()
            if not ai_response:
                ai_response = "本地数据库未匹配到可用结果。"

            for i in range(0, len(ai_response), 24):
                yield _sse_event("chunk", {"delta": ai_response[i:i + 24]})
                await asyncio.sleep(0.01)

            extracted_titles = extract_movie_titles(ai_response)
            if extracted_titles:
                async with AsyncSessionLocal() as db:
                    matched_movies = await fetch_movies_by_titles(extracted_titles, db, limit=10)
            else:
                matched_movies = []
            movie_results = [MovieBase.model_validate(movie) for movie in matched_movies]

            response_payload = {
                "status": "success",
                "agent_answer": ai_response,
                "movie_titles": [m.model_dump() for m in movie_results] if movie_results else None,
                "timestamp": datetime.now().isoformat()
            }
            yield _sse_event("done", response_payload)

            elapsed_ms = int((perf_counter() - started_at) * 1000)
            logger.info(f"✅ AI 流式对话完成: {elapsed_ms}ms | 查询: {request.query}")

        except MovieAgentError as e:
            detail = str(e)
            if _is_noise_message(detail):
                detail = "AI 上游服务调用失败（可能是网络、密钥或配额问题）"
            logger.error(f"AI 流式处理异常: {detail}", exc_info=True)
            yield _sse_event("server_error", {"detail": f"AI 处理失败: {detail}"})
        except asyncio.CancelledError:
            client = ""
            try:
                client = f"{raw_request.client.host}:{raw_request.client.port}" if raw_request and raw_request.client else "unknown"
            except Exception:
                client = "unknown"
            logger.warning("⚠️ SSE 任务被取消（可能是客户端断开或服务端取消）| client=%s | 查询: %s", client, request.query)
            if ai_task and not ai_task.done():
                ai_task.cancel()
            return
        except Exception as e:
            root_message = _extract_exception_message(e)
            logger.error(f"AI 流式处理异常: {root_message}", exc_info=True)
            yield _sse_event("server_error", {"detail": f"AI 处理失败: {root_message}"})
        finally:
            if ai_task and not ai_task.done():
                ai_task.cancel()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/agent/chat/stream", tags=["AI 助手"])
async def chat_with_agent_stream_get(
    q: str = Query(..., min_length=1, max_length=500, description="AI 对话查询"),
    raw_request: Request = None
):
    """SSE GET 版本，供 EventSource 使用。"""
    req = AgentChatRequest(query=q)
    return await chat_with_agent_stream(req, raw_request)


@app.get("/movies", tags=["电影检索"], response_model=MovieListResponse)
async def list_movies(
    q: Optional[str] = Query(None, description="搜索关键词"),
    source: Optional[str] = Query(None, description="来源平台"),
    year: Optional[str] = Query(None, description="年份"),
    min_rating: Optional[float] = Query(None, ge=0, le=10, description="最低评分"),
    sort_by: Optional[str] = Query("rating", description="排序字段: rating/year/rating_count"),
    page: int = Query(1, ge=1, description="页码"),
    limit: int = Query(20, ge=1, le=100, description="每页数量"),
    db: AsyncSession = Depends(get_db)
):
    """
    电影列表查询接口
    支持搜索、过滤、排序和分页
    """
    # 缓存策略：仅首页无过滤条件时使用
    use_cache = (page == 1 and not any([q, source, year, min_rating]))
    cache_key = f"movies:list:p{page}:l{limit}"
    
    if use_cache:
        cached = await cache_manager.get(cache_key)
        if cached:
            logger.info("⚡ 列表缓存命中")
            import json
            return json.loads(cached)
    
    # 构建查询
    stmt = select(Movie)
    order_exprs = []
    q_norm = None
    typo_threshold = float(getattr(settings, "search_typo_similarity_threshold", 0.16))
    
    # 搜索条件
    if q:
        q_norm = re.sub(r"[\s\W_]+", "", q.lower())
        search_text = func.concat(
            func.coalesce(Movie.title, ""), " ",
            func.coalesce(Movie.summary, ""), " ",
            func.coalesce(Movie.director, ""), " ",
            func.coalesce(Movie.stars, "")
        )
        similarity = func.similarity(search_text, q)
        like_pattern = f"%{q.strip()}%"
        norm_like_pattern = f"%{q_norm}%"
        norm_title = func.regexp_replace(func.lower(func.coalesce(Movie.title, "")), r"[[:space:][:punct:]_]+", "", "g")
        norm_summary = func.regexp_replace(func.lower(func.coalesce(Movie.summary, "")), r"[[:space:][:punct:]_]+", "", "g")
        title_word_sim = func.word_similarity(func.lower(func.coalesce(Movie.title, "")), q.lower())
        summary_word_sim = func.word_similarity(func.lower(func.coalesce(Movie.summary, "")), q.lower())

        # 标题检索增强：
        # 1) 大小写无关 + ILIKE
        # 2) 归一化后匹配（去空格/标点）
        # 3) trigram 拼写容错
        stmt = stmt.where(or_(
            search_text.op("%")(q),
            Movie.title.ilike(like_pattern),
            Movie.summary.ilike(like_pattern),
            norm_title.ilike(norm_like_pattern),
            norm_summary.ilike(norm_like_pattern),
            title_word_sim >= typo_threshold,
            summary_word_sim >= typo_threshold,
            similarity >= typo_threshold
        ))
        order_exprs.append(desc(title_word_sim))
        order_exprs.append(desc(similarity))
    
    # 过滤条件
    if source:
        stmt = stmt.where(func.lower(Movie.source) == source.lower())
    if year:
        stmt = stmt.where(Movie.year == year)
    if min_rating is not None:
        stmt = stmt.where(Movie.rating >= min_rating)
    
    # 排序
    if sort_by == "year":
        order_exprs.append(desc(Movie.year))
    elif sort_by == "rating_count":
        order_exprs.append(desc(Movie.rating_count))
    else:
        order_exprs.append(desc(Movie.rating))
    
    stmt = stmt.order_by(*order_exprs)
    
    # 计算总数（去掉排序，避免在 count 子查询中触发无关的向量参数绑定）
    count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
    total = (await db.execute(count_stmt)).scalar() or 0
    
    # 分页
    stmt = stmt.offset((page - 1) * limit).limit(limit)
    result = await db.execute(stmt)
    items = result.scalars().all()

    # 文本路径未命中时，走向量语义回退（用于误拼兜底）
    if q and page == 1:
        vector = build_embedding_vector(q)
        if vector:
            vec_threshold = float(getattr(settings, "search_vector_similarity_threshold", 0.45))
            vec_pool = limit
            vector_str = "[" + ",".join(map(str, vector)) + "]"

            where_parts = ["embedding IS NOT NULL"]
            params = {"qvec": vector_str, "lim": vec_pool}
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

            existing_ids = {movie.id for movie in items}
            candidate_ids = [
                int(row.id)
                for row in vec_rows
                if float(row.vec_score or 0.0) >= vec_threshold and int(row.id) not in existing_ids
            ]

            if candidate_ids:
                vec_movies = (await db.execute(select(Movie).where(Movie.id.in_(candidate_ids)))).scalars().all()
                by_id = {m.id: m for m in vec_movies}
                for mid in candidate_ids:
                    movie = by_id.get(mid)
                    if movie:
                        items.append(movie)

                if len(items) > limit:
                    items = items[:limit]
    # 文本路径未命中时，走向量语义回退（用于误拼兜底）
    if q and total == 0:
        vector = build_embedding_vector(q)
        if vector:
            vec_threshold = float(getattr(settings, "search_vector_similarity_threshold", 0.45))
            vector_str = "[" + ",".join(map(str, vector)) + "]"
            vector_sql = text("""
                SELECT id, COALESCE(1 - (embedding <=> CAST(:qvec AS vector)), 0.0) AS vec_score
                FROM movies
                WHERE embedding IS NOT NULL
                ORDER BY embedding <=> CAST(:qvec AS vector)
                LIMIT :lim
            """)
            vec_rows = (await db.execute(vector_sql, {"qvec": vector_str, "lim": limit})).all()
            vec_ids = [int(row.id) for row in vec_rows if float(row.vec_score or 0.0) >= vec_threshold]
            if vec_ids:
                vec_movies = (await db.execute(select(Movie).where(Movie.id.in_(vec_ids)))).scalars().all()
                by_id = {m.id: m for m in vec_movies}
                items = [by_id[mid] for mid in vec_ids if mid in by_id]
                total = len(items)
    
    # 构建响应
    response = MovieListResponse(
        total=total,
        page=page,
        limit=limit,
        total_pages=(total + limit - 1) // limit if total > 0 else 0,
        has_next=page * limit < total,
        has_prev=page > 1,
        items=[MovieBase.model_validate(item) for item in items]
    )
    
    # 缓存结果
    if use_cache:
        await cache_manager.set(cache_key, response.model_dump(), ttl=600)
    
    return response


@app.post("/movies/rag-search", tags=["电影检索"], response_model=RagSearchResponse)
async def movies_rag_search(
    request: RagSearchRequest,
    raw_request: Request,
    db: AsyncSession = Depends(get_db)
):
    """RAG 检索接口：仅执行 RRF + 专用 reranker，不经过 Agent。"""
    started_at = perf_counter()
    client_ip = raw_request.headers.get("X-Real-IP") or raw_request.client.host
    logger.info("📍 RAG 检索请求来源: %s | 查询: %s", client_ip, request.query)

    candidates = await search_movies_for_ai_mode(request.query, db)
    items = [MovieBase.model_validate(movie) for movie, _ in candidates]
    elapsed_ms = int((perf_counter() - started_at) * 1000)
    logger.info("✅ RAG 检索完成: %sms | hits=%d | 查询: %s", elapsed_ms, len(items), request.query)

    return RagSearchResponse(
        status="success",
        query=request.query,
        total=len(items),
        items=items,
        timestamp=datetime.now().isoformat()
    )


@app.get("/movies/{movie_id}", tags=["电影检索"], response_model=MovieDetail)
async def get_movie_detail(
    movie_id: int = Path(..., gt=0, description="电影ID"),
    db: AsyncSession = Depends(get_db)
):
    """获取电影详情"""
    # 尝试缓存
    cache_key = f"movie:detail:{movie_id}"
    cached = await cache_manager.get(cache_key)
    if cached:
        import json
        return json.loads(cached)
    
    stmt = select(Movie).where(Movie.id == movie_id)
    result = await db.execute(stmt)
    movie = result.scalar_one_or_none()
    
    if not movie:
        raise MovieNotFoundError(movie_id=movie_id)
    
    detail = MovieDetail.model_validate(movie)
    await cache_manager.set(cache_key, detail.model_dump(), ttl=3600)
    
    return detail


@app.get("/stats/platforms", tags=["数据统计"])
async def platform_statistics(db: AsyncSession = Depends(get_db)):
    """平台数据统计"""
    # 缓存统计数据
    cache_key = "stats:platforms"
    cached = await cache_manager.get(cache_key)
    if cached:
        import json
        return json.loads(cached)
    
    try:
        count_expr = func.count().label("count")
        avg_rating_expr = func.round(
            cast(func.avg(Movie.rating), Numeric),
            2
        ).label("avg_rating")
        max_rating_expr = func.max(Movie.rating).label("max_rating")
        min_rating_expr = func.min(Movie.rating).label("min_rating")

        stmt = (
            select(
                Movie.source,
                count_expr,
                avg_rating_expr,
                max_rating_expr,
                min_rating_expr,
            )
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


@app.get("/surprise-me", tags=["特色功能"], response_model=MovieDetail)
async def random_recommendation(
    min_rating: float = Query(8.0, ge=0, le=10, description="最低评分"),
    db: AsyncSession = Depends(get_db)
):
    """随机电影推荐"""
    try:
        stmt = (
            select(Movie)
            .where(Movie.rating >= min_rating)
            .order_by(func.random())
            .limit(1)
        )
        
        result = await db.execute(stmt)
        movie = result.scalar_one_or_none()
        
        if not movie:
            raise MovieNotFoundError(detail=f"没有找到评分 >= {min_rating} 的电影")
        
        return MovieDetail.model_validate(movie)
        
    except Exception as e:
        logger.error(f"推荐查询错误: {e}")
        raise DatabaseError(detail="推荐查询失败")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=settings.debug,
        log_level=settings.log_level.lower()
    )
