from fastapi import FastAPI, Query, HTTPException, Depends, Path
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
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
class AgentChatRequest(BaseModel):
    query: str = Field(..., example="帮我找几部评分高于9分的科幻电影")

class AgentChatResponse(BaseModel):
    status: str
    agent_answer: str
    movie_titles: Optional[List[str]] = None
    timestamp: str

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

# --- 辅助函数：根据查询关键词搜索电影 (ORM版) ---
async def search_movies_by_keywords(query: str, session: AsyncSession, limit: int = 5) -> List[str]:
    """根据用户查询关键词从数据库搜索电影，返回标题列表"""
    try:
        stmt = select(Movie.title)
        conditions = []

        if any(k in query for k in ["温馨", "感人", "温暖", "治愈", "亲情", "家庭"]):
            conditions.append(Movie.rating >= 8.5)

        elif any(k in query for k in ["科幻", "太空", "未来", "星际"]):
            conditions.append(or_(Movie.summary.ilike("%科幻%"), Movie.title.ilike("%科幻%")))

        elif any(k in query for k in ["高分", "评分高", "评分最高", "经典", "佳片"]):
            conditions.append(Movie.rating >= 9.0)

        elif any(k in query for k in ["悬疑", "烧脑", "推理", "侦探"]):
            conditions.append(or_(Movie.summary.ilike("%悬疑%"), Movie.title.ilike("%悬疑%"))) # 修正拼写

        elif any(k in query for k in ["喜剧", "搞笑", "轻松", "幽默"]):
            conditions.append(or_(Movie.summary.ilike("%喜剧%"), Movie.title.ilike("%喜剧%"))) # 修正拼写

        elif any(k in query for k in ["动作", "战争", "枪战", "特工"]):
            conditions.append(or_(Movie.summary.ilike("%动作%"), Movie.title.ilike("%动作%"))) # 修正拼写

        elif any(k in query for k in ["最新", "新片", "近期", "2025", "2026"]):
            # 注意：year 字段在模型中定义为 String，比较时可能需要适配
            conditions.append(Movie.year >= "2025") 

        elif "导演" in query or "执导" in query:
            director_match = re.search(r'([\u4e00-\u9fa5]+|[A-Za-z\s]+)(?:导演|执导)', query)
            if director_match:
                director_name = director_match.group(1).strip()
                conditions.append(Movie.director.ilike(f"%{director_name}%"))
        
        # 构建查询
        if not conditions:
            # 默认：返回评分最高的
            stmt = stmt.order_by(desc(Movie.rating))
        else:
            stmt = stmt.where(or_(*conditions) if len(conditions) > 1 else conditions[0])
            stmt = stmt.order_by(desc(Movie.rating)) # 即使有关键词，也按评分排序

        stmt = stmt.limit(limit)
        result = await session.execute(stmt)
        return result.scalars().all()
    except Exception as e:
        logger.error(f"❌ 搜索出错: {e}")
        return []


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
        logger.info(f"📩 收到查询: {request.query}")
        
        # 1. AI 回复
        ai_response = movie_assistant.ask(request.query)

        logger.info(f"🤖 AI 响应长度: {len(ai_response)} 字符")
        # 2. 提取电影名
        extracted_titles = extract_movie_titles_from_text(ai_response)
        movie_titles_found = []

        # 3. 验证或搜索
        if extracted_titles:
            # 验证 AI 提到的电影是否在数据库中
            for title in extracted_titles:
                # 使用 ILIKE 进行不区分大小写的模糊匹配 (Postgres 特性)
                stmt = select(Movie.title).where(Movie.title.ilike(f"%{title}%")).limit(1)
                res = await db.execute(stmt)
                if res.scalar():
                    movie_titles_found.append(title)

        if not movie_titles_found:
            # 检查 AI 的回复里是否包含“相似度”关键字，如果包含，说明向量工具已运行但提取失败
            if "相似度" not in ai_response:
                logger.info("⚠️ AI 未提及库内电影且未触发向量检索，执行关键词搜索兜底...")
                movie_titles_found = await search_movies_by_keywords(request.query, db, limit=5)
        
        return AgentChatResponse(
            status="success", 
            agent_answer=ai_response, 
            movie_titles=movie_titles_found,
            timestamp=datetime.now().isoformat()
        )
        
    except Exception as e:
        logger.error(f"❌ Agent 运行异常: {str(e)}")
        raise HTTPException(status_code=500, detail=f"AI 处理出错: {str(e)}")


@app.get("/movies", tags=["电影检索"], response_model=MovieListResponse)
async def list_movies(
    q: Optional[str] = None,
    source: Optional[str] = None,
    year: Optional[str] = None,
    min_rating: Optional[float] = None,
    page: int = 1,
    limit: int = 20,
    db: AsyncSession = Depends(get_db)
):
    # 1. 构建基础查询
    stmt = select(Movie)
    
    if q:
        search_pattern = f"%{q}%"
        stmt = stmt.where(or_(
            Movie.title.like(search_pattern),
            Movie.director.like(search_pattern),
            Movie.stars.like(search_pattern)
        ))
    if source:
        stmt = stmt.where(Movie.source == source)
    if year:
        stmt = stmt.where(Movie.year == year)
    if min_rating:
        stmt = stmt.where(Movie.rating >= min_rating)
        
    # 2. 计算总数 (优化性能，只查 ID)
    # 使用 subquery 来确保 count 计算的是过滤后的总数
    subquery = stmt.subquery()
    count_stmt = select(func.count()).select_from(subquery)
    total = (await db.execute(count_stmt)).scalar() or 0

    # 3. 分页与排序
    stmt = stmt.order_by(desc(Movie.rating)).offset((page - 1) * limit).limit(limit)
    
    result = await db.execute(stmt)
    items = result.scalars().all()
    
    return {
        "total": total,
        "page": page,
        "limit": limit,
        "total_pages": (total + limit - 1) // limit if total > 0 else 0,
        "has_next": page * limit < total,
        "has_prev": page > 1,
        "items": items  
    }

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
    try:
        # 预热 AI 连接，把 SSL 握手在启动时就做完
        movie_assistant.ask("hi") 
        logger.info("🚀 AI 链路预热完成")
    except:
        logger.warning("⚠️ 预热失败，可能网络未就绪")
        
@app.get("/health", tags=["系统"])
async def health_check(db: AsyncSession = Depends(get_db)):
    """健康检查 (ORM版)"""
    try:
        # 简单查询确认 DB 连接
        stmt = select(func.count(Movie.id))
        result = await db.execute(stmt)
        count = result.scalar() or 0
        
        return {
            "status": "healthy",
            "database": "PostgreSQL connected",
            "orm": "SQLAlchemy Async",
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