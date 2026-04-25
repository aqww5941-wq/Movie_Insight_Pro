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
from typing import List, Optional
from datetime import datetime
from time import perf_counter

# 本地模块导入
from config import get_settings
from database import get_db, engine
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
from sqlalchemy import select, func, or_, desc

# AI Agent
from agents.movie_agent import MovieAgent, MovieAgentError
from utils.helpers import AIAgent

# 配置加载
settings = get_settings()

# 日志配置
logging.basicConfig(
    level=getattr(logging, settings.log_level),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


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
    redoc_url="/redoc"
)

# 初始化 AI Agent
try:
    app.state.movie_agent = MovieAgent()
    logger.info("✅ AI Agent 初始化成功")
except Exception as e:
    logger.error(f"❌ AI Agent 初始化失败: {e}")
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
        # 检查缓存
        cache_key = f"ai:chat:{request.query}"
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

        # 快速检索路径：对明确检索意图优先走数据库查询，避免每次都调用 LLM。
        if is_retrieval_query(request.query):
            fast_movies = await search_movies_fast(request.query, db, limit=5)
            if fast_movies:
                answer_text = format_fast_retrieval_answer(request.query, fast_movies)
            else:
                answer_text = "未检索到匹配电影，请尝试更短的关键词，例如：科幻、悬疑、高分。"

            response = AgentChatResponse(
                status="success",
                agent_answer=answer_text,
                movie_titles=[MovieBase.model_validate(m) for m in fast_movies] if fast_movies else None,
                timestamp=datetime.now().isoformat()
            )
            await cache_manager.set(cache_key, response.model_dump(), ttl=3600)
            elapsed_ms = int((perf_counter() - started_at) * 1000)
            logger.info(f"⚡ 快速检索完成: {elapsed_ms}ms | 查询: {request.query}")
            return response
        
        # AI 处理：放入线程执行并设置超时，避免长时间阻塞导致前端先超时
        ai_timeout_seconds = max(1, int(getattr(settings, "ai_request_timeout_seconds", 15)))
        try:
            ai_response = await asyncio.wait_for(
                asyncio.to_thread(app.state.movie_agent.ask, request.query),
                timeout=ai_timeout_seconds
            )
        except asyncio.TimeoutError:
            elapsed_ms = int((perf_counter() - started_at) * 1000)
            logger.warning(
                "⏱️ AI 调用超时: %dms (阈值: %ds) | 查询: %s",
                elapsed_ms,
                ai_timeout_seconds,
                request.query
            )
            fallback_text = (
                "这个问题处理时间有点长，我先给你一个快速入口：\n"
                "1. 电影推荐（如：推荐5部高分科幻片）\n"
                "2. 条件筛选（如：2020年后，评分8分以上）\n"
                "3. 相似电影（如：类似《星际穿越》的电影）\n"
                "你也可以换个更具体的电影问题，我会更快返回。"
            )
            response = AgentChatResponse(
                status="success",
                agent_answer=fallback_text,
                movie_titles=None,
                timestamp=datetime.now().isoformat()
            )
            await cache_manager.set(cache_key, response.model_dump(), ttl=600)
            return response

        logger.info(f"🤖 AI 响应: {len(ai_response)} 字符")
        
        # 提取电影名称
        extracted_titles = extract_movie_titles(ai_response)
        movie_results = []
        
        # 验证电影是否存在于数据库
        if extracted_titles:
            matched_movies = await fetch_movies_by_titles(extracted_titles, db, limit=10)
            movie_results = [MovieBase.model_validate(movie) for movie in matched_movies]
        
        # 如果没有找到，使用搜索回退
        if not movie_results and "相似度" not in ai_response:
            logger.info("💡 执行搜索回退...")
            fallback_movies = await search_movies_by_keywords(request.query, db, limit=5)
            movie_results = [MovieBase.model_validate(m) for m in fallback_movies]
        
        # 构建响应
        response = AgentChatResponse(
            status="success",
            agent_answer=ai_response,
            movie_titles=movie_results if movie_results else None,
            timestamp=datetime.now().isoformat()
        )
        
        # 缓存结果
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
    raw_request: Request,
    db: AsyncSession = Depends(get_db)
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

            full_parts: List[str] = []
            async for delta in app.state.movie_agent.stream_answer(request.query):
                if not delta:
                    continue
                full_parts.append(delta)
                yield _sse_event("chunk", {"delta": delta})

            ai_response = "".join(full_parts).strip()
            if not ai_response:
                ai_response = "我暂时没有生成到有效回答，请换个问法再试。"
                yield _sse_event("chunk", {"delta": ai_response})

            extracted_titles = extract_movie_titles(ai_response)
            movie_results: List[MovieBase] = []

            if extracted_titles:
                matched_movies = await fetch_movies_by_titles(extracted_titles, db, limit=10)
                movie_results = [MovieBase.model_validate(movie) for movie in matched_movies]

            if not movie_results:
                fallback_movies = await search_movies_by_keywords(request.query, db, limit=5)
                movie_results = [MovieBase.model_validate(movie) for movie in fallback_movies]

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
            yield _sse_event("error", {"detail": f"AI 处理失败: {detail}"})
        except Exception as e:
            root_message = _extract_exception_message(e)
            logger.error(f"AI 流式处理异常: {root_message}", exc_info=True)
            yield _sse_event("error", {"detail": f"AI 处理失败: {root_message}"})

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


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
    
    # 搜索条件
    if q:
        search_text = func.concat(func.coalesce(Movie.title, ""), " ", func.coalesce(Movie.summary, ""))
        similarity = func.similarity(search_text, q)

        # 优先保证 API 可用性，暂时走稳定的文本检索路径。
        stmt = stmt.where(search_text.op("%")(q))
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
        stmt = (
            select(
                Movie.source,
                func.count().label('count'),
                func.round(func.avg(Movie.rating), 2).label('avg_rating'),
                func.max(Movie.rating).label('max_rating'),
                func.min(Movie.rating).label('min_rating')
            )
            .where(Movie.source.is_not(None))
            .group_by(Movie.source)
            .order_by(desc('count'))
        )
        
        result = await db.execute(stmt)
        stats = [dict(row._mapping) for row in result.all()]
        
        await cache_manager.set(cache_key, stats, ttl=1800)
        return stats
        
    except Exception as e:
        logger.error(f"统计查询错误: {e}")
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
