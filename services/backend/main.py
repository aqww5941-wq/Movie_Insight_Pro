from fastapi import FastAPI, Query, HTTPException, Depends, Path
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from fastapi.encoders import jsonable_encoder # 🚀 引入此工具处理序列化
import logging
import re
from fastapi import Request 
from typing import List, Optional, Literal, Dict, Any
from datetime import datetime
from agents.movie_agent import MovieAgent

# 引入 SQLAlchemy 异步组件
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_, desc, asc
from sqlalchemy.sql.expression import case

# 引入本地模块
from models import Movie, Base
from database import get_db, engine

import redis.asyncio as redis # 建议用异步驱动
import json

# --- 日志配置 ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- FastAPI 应用 ---
app = FastAPI(
    title="🎬 Movie Insight Pro",
    description="专业的电影数据检索系统 (PostgreSQL + Async Edition)",
    version="3.1.0"
)

# 初始化 AI Agent
try:
    movie_assistant = MovieAgent()
    logger.info("✅ AI Agent 初始化成功")
except Exception as e:
    logger.error(f"❌ Agent 初始化失败: {e}")
    movie_assistant = None

# 添加 CORS 支持
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- 数据模型 (Pydantic) ---
class MovieBase(BaseModel):
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

class AgentChatRequest(BaseModel):
    query: str = Field(..., example="帮我找几部评分高于9分的科幻电影")

class AgentChatResponse(BaseModel):
    status: str
    agent_answer: str
    movie_titles: Optional[List[MovieBase]] = None
    timestamp: str


class MovieDetail(MovieBase):
    url: Optional[str] = None
    summary: Optional[str] = None
    created_at: Optional[datetime] = None

class MovieListResponse(BaseModel):
    total: int
    page: int
    limit: int
    total_pages: int
    items: List[MovieBase]

# --- 启动事件：自动建表 ---
@app.on_event("startup")
async def startup():
    # 注意：生产环境建议使用 Alembic 进行迁移，这里保留自动建表方便开发
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

# --- 辅助函数：从 AI 文本中提取电影名称 ---
def extract_movie_titles_from_text(text: str) -> List[str]:
    """从 AI 返回的文本中提取电影名称"""
    titles = []
    # 方法1: 匹配 《电影名》
    pattern1 = r'《([^》]+)》'
    titles.extend(re.findall(pattern1, text))
    
    # 方法2: 匹配 **电影名**
    pattern2 = r'\*\*《?([^*》]+)》?\*\*'
    matches2 = re.findall(pattern2, text)
    for match in matches2:
        clean_title = match.strip()
        if clean_title not in titles:
            titles.append(clean_title)
    
    # 去重
    return list(set(titles))

# 根据查询关键词搜索电影 (ORM版) ---
async def search_movies_by_keywords(query: str, session: AsyncSession, limit: int = 5):
    """根据用户查询关键词从数据库搜索电影，返回对象"""
    try:
        stmt = select(Movie).where(
            or_(
                Movie.title.ilike(f"%{query}%"),
                Movie.summary.ilike(f"%{query}%")
            )
        ).order_by(desc(Movie.rating)).limit(limit)

        result = await session.execute(stmt)
        return result.scalars().all()
    except Exception as e:
        logger.error(f"❌ 搜索报错: {e}")
        return []


# 初始化连接
redis_client = redis.Redis(host='redis', port=6379, decode_responses=True)

# --- 路由定义 ---

@app.post("/agent/chat", tags=["AI 智能助手"], response_model=AgentChatResponse)
async def chat_with_movie_agent(request: AgentChatRequest, raw_request: Request, db: AsyncSession = Depends(get_db)):
    client_ip = raw_request.headers.get("X-Real-IP") or raw_request.client.host
    logger.info(f"📍 请求来源 IP: {client_ip}")
    """
    🤖 AI 影评专家接口 (Async + PG)
    """
    if not movie_assistant:
        raise HTTPException(status_code=503, detail="AI 服务初始化失败")
    
    
    try:
        cache_key = f"ai_cache:{request.query}"
        cached_res = await redis_client.get(cache_key)

        if cached_res:
            logger.info(f"⚡️ 命中 Redis 语义缓存: {request.query}")
            return AgentChatResponse(**json.loads(cached_res))
        
        
        # 1. AI 回复
        ai_response = movie_assistant.ask(request.query)

        logger.info(f"🤖 AI 响应长度: {len(ai_response)} 字符")
        # 2. 提取电影名
        extracted_titles = extract_movie_titles_from_text(ai_response)
        movie_objects_found = []

        # 3. 验证或搜索
        if extracted_titles:
            # 验证 AI 提到的电影是否在数据库中
            for title in extracted_titles:
                # 使用 ILIKE 进行不区分大小写的模糊匹配 (Postgres 特性)
                stmt = select(Movie).where(Movie.title.ilike(f"%{title}%")).limit(1)
                res = await db.execute(stmt)
                movie_item = res.scalar_one_or_none()
               
                if movie_item:
                # 转化为 Pydantic 模型，方便序列化
                    movie_objects_found.append(MovieBase.model_validate(movie_item))

        if not movie_objects_found:
            # 检查 AI 的回复里是否包含“相似度”关键字，如果包含，说明向量工具已运行但提取失败
            if "相似度" not in ai_response:
                logger.info("💡 触发 Fallback:AI 结果为空，正在执行 pg_trgm 全文检索...")
                fallback_items = await search_movies_by_keywords(request.query, db, limit=5)
                # 将 Fallback 拿到的 SQLAlchemy 对象也转换为 Pydantic
                movie_objects_found = [MovieBase.model_validate(m) for m in fallback_items]
        
        response_data = AgentChatResponse(
            status="success", 
            agent_answer=ai_response, 
            movie_titles=movie_objects_found,
            timestamp=datetime.now().isoformat()
        )
        await redis_client.setex(cache_key, 86400, response_data.model_dump_json())

        return response_data
        
    except Exception as e:
        logger.error(f"❌ Agent 运行异常: {str(e)}")
        raise HTTPException(status_code=500, detail=f"AI 处理出错: {str(e)}")


@app.get("/movies", tags=["电影检索"], response_model=MovieListResponse)
async def list_movies(
    q: Optional[str] = None,
    source: Optional[str] = None,
    year: Optional[str] = None,
    min_rating: Optional[float] = None,
    sort_by: Optional[str] = "rating", # 🚀 补充这个接收参数
    page: int = 1,
    limit: int = 20,
    db: AsyncSession = Depends(get_db)
):
    # 🚀 只有在第一页且没有搜索关键词时才触发缓存，保证性能与新鲜度的平衡
    # 只有当没有任何过滤条件时，才使用那个“通用首页缓存”
    use_cache = (page == 1 and not any([q, source, year, min_rating]))
    cache_key = "movies:list:pure_home_p1"

    if use_cache:
        cached_data = await redis_client.get(cache_key)
        if cached_data:
            logger.info("⚡️ 命中电影列表首页缓存")
            return json.loads(cached_data)
    
    # 1. 构建基础查询
    stmt = select(Movie)
    
    if q:
        search_pattern = f"%{q}%"
        stmt = stmt.where(or_(
            Movie.title.ilike(search_pattern),
            Movie.director.ilike(search_pattern),
            Movie.stars.ilike(search_pattern)
        ))
    if source:
        stmt = stmt.where(Movie.source == source)
    if year:
        stmt = stmt.where(Movie.year == year)
    if min_rating:
        stmt = stmt.where(Movie.rating >= min_rating)

    if sort_by == "year":
        order_col = desc(Movie.year)
    elif sort_by == "rating_count":
        order_col = desc(Movie.rating_count)
    else:
        order_col = desc(Movie.rating)
        
    # 2. 计算总数 (优化性能，只查 ID)
    # 使用 subquery 来确保 count 计算的是过滤后的总数
    subquery = stmt.subquery()
    count_stmt = select(func.count()).select_from(subquery)
    total = (await db.execute(count_stmt)).scalar() or 0

    # 3. 分页与排序
    stmt = stmt.order_by(order_col).offset((page - 1) * limit).limit(limit)
    
    result = await db.execute(stmt)
    items = result.scalars().all()

    safe_items = [MovieBase.model_validate(item).model_dump() for item in items]
    
    response_payload = {
        "total": total,
        "page": page,
        "limit": limit,
        "total_pages": (total + limit - 1) // limit if total > 0 else 0,
        "has_next": page * limit < total,
        "has_prev": page > 1,
        "items": safe_items # 💡 使用 jsonable_encoder 确保可以被 JSON 序列化
    }
    # 🚀 将结果存入缓存，过期时间设为 10 分钟 (600秒)，既快又准
    if use_cache:
        await redis_client.setex(cache_key, 600, json.dumps(response_payload, default=str))
    return response_payload

@app.get("/movies/{movie_id}", tags=["电影检索"], response_model=MovieDetail)
async def get_movie_detail(
    movie_id: int = Path(..., description="电影ID"), 
    db: AsyncSession = Depends(get_db)
):
    """获取电影完整详情 (ORM版)"""
    stmt = select(Movie).where(Movie.id == movie_id)
    result = await db.execute(stmt)
    movie = result.scalar_one_or_none()
    
    if not movie:
        raise HTTPException(status_code=404, detail=f"未找到 ID 为 {movie_id} 的电影")
    
    return movie

@app.get("/stats/platforms", tags=["数据统计"])
async def platform_stats(db: AsyncSession = Depends(get_db)):
    """平台数据分布统计 (ORM版)"""
    try:
        stmt = select(
            Movie.source,
            func.count().label('count'),
            func.round(func.avg(Movie.rating), 2).label('avg_rating'),
            func.max(Movie.rating).label('max_rating'),
            func.min(Movie.rating).label('min_rating')
        ).where(Movie.source.is_not(None))\
         .group_by(Movie.source)\
         .order_by(desc('count'))
        
        result = await db.execute(stmt)
        # 将 Row 对象转换为字典列表
        return result.all()
    except Exception as e:
        logger.error(f"统计查询错误: {e}")
        raise HTTPException(status_code=500, detail="统计查询失败")

@app.get("/surprise-me", tags=["特色功能"], response_model=MovieDetail)
async def random_movie(
    min_rating: float = Query(8.0, ge=0, le=10, description="最低评分"),
    db: AsyncSession = Depends(get_db)
):
    """随机推荐 (PG 特供版)"""
    try:
        # Postgres 使用 func.random()，MySQL 使用 func.rand()
        stmt = select(Movie)\
            .where(Movie.rating >= min_rating)\
            .order_by(func.random())\
            .limit(1)
            
        result = await db.execute(stmt)
        movie = result.scalar_one_or_none()
        
        if not movie:
            raise HTTPException(status_code=404, detail=f"没有找到评分 >= {min_rating} 的电影")
            
        return movie
    except Exception as e:
        logger.error(f"推荐查询错误: {e}")
        raise HTTPException(status_code=500, detail="推荐查询失败")
    
@app.on_event("startup")
async def startup_event():
    logger.info("📡 正在初始化系统基础设施...")

    # 1. 确保数据库表结构已同步 (SQLAlchemy Async)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("✅ PostgreSQL 数据库表结构校验完成")
    except Exception as e:
        logger.error(f"❌ 数据库连接或建表失败: {e}")

    # 2. Redis 连通性预热
    try:
        # 使用 ping 确保 Redis 容器已完全启动并可连接
        await redis_client.ping()
        logger.info("✅ Redis 缓存服务连接就绪")
    except Exception as e:
        logger.warning(f"⚠️ Redis 未就绪，系统将以无缓存模式运行: {e}")

    # 3. AI 链路预热 (SSL 握手与连接池初始化)
    if movie_assistant:
        try:
            # 这里的 ask("hi") 是为了提前建立 HTTPS 连接，减少用户第一次提问的延迟
            # 如果你有 Redis，甚至可以给预热结果也设个特殊的 key
            movie_assistant.ask("hi") 
            logger.info("🚀 AI 语义链路预热完成 (SSL Handshake Done)")
        except Exception as e:
            logger.warning(f"⚠️ AI 预热失败，可能网络环境不稳定: {e}")
    else:
        logger.error("❌ AI Agent 未定义，请检查 API Key 配置")

    logger.info("✨ 所有启动任务处理完毕，API 服务已就绪")
        
@app.get("/health", tags=["系统"])
async def health_check(db: AsyncSession = Depends(get_db)):
    """健康检查 (ORM版)"""
    redis_status = "connected"
    try:
        await redis_client.ping()
    except:
        redis_status = "disconnected"
    try:
        # 简单查询确认 DB 连接
        stmt = select(func.count(Movie.id))
        result = await db.execute(stmt)
        count = result.scalar() or 0
        
        return {
            "status": "healthy",
            "database": "PostgreSQL connected",
            "orm": "SQLAlchemy Async",
            "redis": redis_status, # 🚀 增加 Redis 监控
            "total_movies": count,
            "ai_agent": "enabled" if movie_assistant else "disabled",
            "timestamp": datetime.now().isoformat()
        }
    except Exception as e:
        return {
            "status": "unhealthy",
            "error": str(e)
        }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)