"""
Movie Insight Pro - 主应用入口
电影数据检索与 AI 推荐系统
Version: 4.0.0
"""
from fastapi import FastAPI, Query, Depends, Path, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from contextlib import asynccontextmanager
from pydantic import BaseModel, Field
import logging
import re
from typing import List, Optional
from datetime import datetime

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
from agents.movie_agent import MovieAgent
from utils.helpers import AIAgent

# 配置加载
settings = get_settings()

# 日志配置
logging.basicConfig(
    level=getattr(logging, settings.log_level),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


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


def build_embedding_vector(query: str) -> Optional[str]:
    """生成查询的向量表示"""
    if not query:
        return None
    try:
        vector = AIAgent.generate_embedding(query)
        if not vector:
            return None
        return "[" + ",".join(map(str, vector)) + "]"
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
        search_text = func.coalesce(Movie.title, "") + " " + func.coalesce(Movie.summary, "")
        similarity_expr = func.similarity(search_text, query)
        vector_str = build_embedding_vector(query)

        if vector_str:
            # 混合搜索：向量相似度 + 文本相似度
            vec_sim = 1 - Movie.embedding.op("<=>")(vector_str)
            hybrid_score = (
                0.6 * func.coalesce(vec_sim, 0.0) + 
                0.4 * func.coalesce(similarity_expr, 0.0)
            )
            stmt = (
                select(Movie)
                .where(or_(
                    Movie.embedding.is_not(None), 
                    search_text.op("%")(query)
                ))
                .order_by(desc(hybrid_score), desc(Movie.rating))
                .limit(limit)
            )
        else:
            # 纯文本搜索
            stmt = (
                select(Movie)
                .where(search_text.op("%")(query))
                .order_by(desc(similarity_expr), desc(Movie.rating))
                .limit(limit)
            )

        result = await session.execute(stmt)
        return result.scalars().all()
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
        
        # AI 处理
        ai_response = app.state.movie_agent.ask(request.query)
        logger.info(f"🤖 AI 响应: {len(ai_response)} 字符")
        
        # 提取电影名称
        extracted_titles = extract_movie_titles(ai_response)
        movie_results = []
        
        # 验证电影是否存在于数据库
        if extracted_titles:
            for title in extracted_titles[:10]:  # 限制最多10部
                stmt = select(Movie).where(Movie.title.ilike(f"%{title}%")).limit(1)
                result = await db.execute(stmt)
                movie = result.scalar_one_or_none()
                if movie:
                    movie_results.append(MovieBase.model_validate(movie))
        
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
        
        return response
        
    except Exception as e:
        logger.error(f"AI 处理异常: {e}", exc_info=True)
        raise AIServiceError(detail=f"AI 处理失败: {str(e)}")


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
        search_text = func.coalesce(Movie.title, "") + " " + func.coalesce(Movie.summary, "")
        similarity = func.similarity(search_text, q)
        vector_str = build_embedding_vector(q)
        
        if vector_str:
            vec_sim = 1 - Movie.embedding.op("<=>")(vector_str)
            hybrid_score = 0.6 * func.coalesce(vec_sim, 0.0) + 0.4 * func.coalesce(similarity, 0.0)
            stmt = stmt.where(or_(Movie.embedding.is_not(None), search_text.op("%")(q)))
            order_exprs.append(desc(hybrid_score))
        else:
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
    
    # 计算总数
    count_stmt = select(func.count()).select_from(stmt.subquery())
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
