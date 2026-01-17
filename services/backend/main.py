from fastapi import FastAPI, Query, HTTPException, Depends, Path
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import pymysql
import os
import logging
from typing import List, Optional, Literal
from datetime import datetime
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from agents.movie_agent import MovieAgent
import re


# --- 日志配置 ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- FastAPI 应用 ---
app = FastAPI(
    title="🎬 Movie Insight Pro",
    description="专业的电影数据检索系统",
    version="3.0.0"
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

# --- 数据模型 ---
class AgentChatRequest(BaseModel):
    query: str = Field(..., example="帮我找几部评分高于9分的科幻电影")

class AgentChatResponse(BaseModel):
    """AI Agent 返回格式"""
    status: str
    agent_answer: str
    movie_titles: Optional[List[str]] = None
    timestamp: str

class MovieBase(BaseModel):
    """电影列表展示"""
    id: int
    title: str
    year: Optional[str] = None
    rating: Optional[float] = None
    rating_count: Optional[int] = None
    source: Optional[str] = None
    director: Optional[str] = None
    stars: Optional[str] = None
    cover_url: Optional[str] = None

class MovieDetail(MovieBase):
    """电影完整信息"""
    url: Optional[str] = None
    summary: Optional[str] = None
    created_at: Optional[datetime] = None

class MovieListResponse(BaseModel):
    """列表响应"""
    total: int
    page: int
    limit: int
    total_pages: int
    has_next: bool
    has_prev: bool
    items: List[MovieBase]

# --- 数据库配置 ---
DB_CONFIG = {
    "host": os.getenv("MYSQL_HOST", "localhost"),
    "port": int(os.getenv("MYSQL_PORT", 3306)),
    "user": os.getenv("MYSQL_USER", "root"),
    "password": os.getenv("MYSQL_ROOT_PASSWORD", "000000"),
    "database": os.getenv("MYSQL_DATABASE", "movie_db"),
    "charset": 'utf8mb4',
    "cursorclass": pymysql.cursors.DictCursor
}

def get_db():
    """获取数据库连接"""
    conn = pymysql.connect(**DB_CONFIG)
    try:
        yield conn
    finally:
        conn.close()


# --- 辅助函数：从 AI 文本中提取电影名称 ---
def extract_movie_titles_from_text(text: str) -> List[str]:
    """
    从 AI 返回的文本中提取电影名称
    支持多种格式：《电影名》、**电影名**
    """
    titles = []
    
    # 方法1: 匹配 《电影名》 格式
    pattern1 = r'《([^》]+)》'
    matches1 = re.findall(pattern1, text)
    titles.extend(matches1)
    
    # 方法2: 匹配 **电影名** 格式（Markdown 加粗）
    pattern2 = r'\*\*《?([^*》]+)》?\*\*'
    matches2 = re.findall(pattern2, text)
    for match in matches2:
        clean_title = match.strip()
        if clean_title not in titles:
            titles.append(clean_title)
    
    logger.info(f"📝 从 AI 回复中提取到电影名称: {titles}")
    return titles


# --- 辅助函数：根据查询关键词搜索电影 ---
def search_movies_by_keywords(query: str, db_conn, limit: int = 5) -> List[str]:
    """
    根据用户查询关键词从数据库搜索电影
    返回电影标题列表
    """
    try:
        with db_conn.cursor() as cursor:
            # 关键词映射和查询逻辑
            keywords_found = []
            conditions = []
            params = []
            
            # 1. 温馨/感人类电影
            if any(keyword in query for keyword in ["温馨", "感人", "温暖", "治愈", "亲情", "家庭"]):
                keywords_found.append("温馨")
                conditions.append("rating >= %s")
                params.append(8.5)
                # 可以根据简介或导演筛选
                
            # 2. 科幻类
            elif any(keyword in query for keyword in ["科幻", "太空", "未来", "星际"]):
                keywords_found.append("科幻")
                conditions.append("(summary LIKE %s OR title LIKE %s)")
                params.extend(["%科幻%", "%科幻%"])
                
            # 3. 高分电影
            elif any(keyword in query for keyword in ["高分", "评分高", "评分最高", "经典", "佳片"]):
                keywords_found.append("高分")
                conditions.append("rating >= %s")
                params.append(9.0)
                
            # 4. 悬疑/烧脑
            elif any(keyword in query for keyword in ["悬疑", "烧脑", "推理", "侦探"]):
                keywords_found.append("悬疑")
                conditions.append("(summary LIKE %s OR title LIKE %s)")
                params.extend(["%悬疑%", "%推理%"])
                
            # 5. 喜剧/搞笑
            elif any(keyword in query for keyword in ["喜剧", "搞笑", "轻松", "幽默"]):
                keywords_found.append("喜剧")
                conditions.append("(summary LIKE %s OR title LIKE %s)")
                params.extend(["%喜剧%", "%幽默%"])
                
            # 6. 动作/战争
            elif any(keyword in query for keyword in ["动作", "战争", "枪战", "特工"]):
                keywords_found.append("动作")
                conditions.append("(summary LIKE %s OR title LIKE %s)")
                params.extend(["%动作%", "%战争%"])
                
            # 7. 最新电影
            elif any(keyword in query for keyword in ["最新", "新片", "近期", "2023", "2024", "2025"]):
                keywords_found.append("最新")
                conditions.append("year >= %s")
                params.append("2020")
            
            # 8. 导演相关
            elif "导演" in query or "执导" in query:
                # 尝试提取导演名字
                director_match = re.search(r'([\u4e00-\u9fa5]+|[A-Za-z\s]+)(?:导演|执导)', query)
                if director_match:
                    director_name = director_match.group(1).strip()
                    keywords_found.append(f"导演:{director_name}")
                    conditions.append("director LIKE %s")
                    params.append(f"%{director_name}%")
            
            # 默认：返回评分最高的电影
            if not conditions:
                conditions.append("rating IS NOT NULL")
            
            # 构建 SQL
            where_clause = " AND ".join(conditions)
            sql = f"""
                SELECT DISTINCT title 
                FROM movies 
                WHERE {where_clause}
                ORDER BY rating DESC, rating_count DESC
                LIMIT %s
            """
            params.append(limit)
            
            logger.info(f"🔍 搜索关键词: {keywords_found}")
            logger.info(f"📊 执行 SQL: {sql}")
            logger.info(f"📊 参数: {params}")
            
            cursor.execute(sql, params)
            results = cursor.fetchall()
            
            titles = [row['title'] for row in results]
            logger.info(f"✅ 找到 {len(titles)} 部电影: {titles}")
            return titles
            
    except Exception as e:
        logger.error(f"❌ 搜索电影出错: {e}")
        return []


# --- AI 智能助手路由（优化版）---
@app.post("/agent/chat", tags=["AI 智能助手"], response_model=AgentChatResponse)
async def chat_with_movie_agent(request: AgentChatRequest, db = Depends(get_db)):
    """
    🤖 AI 影评专家接口：支持自然语言直接对话查询
    
    工作流程：
    1. AI 生成推荐说明文字
    2. 从 AI 回复中提取电影名称
    3. 如果提取失败，根据关键词从数据库搜索
    4. 返回：AI 说明 + 电影标题列表
    5. 前端根据标题搜索并展示电影卡片
    """
    if not movie_assistant:
        raise HTTPException(status_code=503, detail="AI 服务初始化失败，请检查 API Key")
    
    try:
        logger.info(f"📩 收到用户查询: {request.query}")
        
        # Step 1: 调用 AI 生成推荐文字
        ai_response = movie_assistant.ask(request.query)
        logger.info(f"🤖 AI 回复: {ai_response[:200]}...")
        
        # Step 2: 从 AI 回复中提取电影名称
        movie_titles = extract_movie_titles_from_text(ai_response)
        
        # Step 3: 如果 AI 没有明确提及电影名，则根据关键词搜索
        if not movie_titles:
            logger.info("⚠️ AI 未提及具体电影，尝试关键词搜索...")
            movie_titles = search_movies_by_keywords(request.query, next(get_db()), limit=5)
        
        # Step 4: 验证这些电影在数据库中是否存在
        validated_titles = []
        if movie_titles:
            with db.cursor() as cursor:
                for title in movie_titles[:10]:  # 最多验证10部
                    # 模糊匹配，因为 AI 可能返回的名字略有差异
                    cursor.execute(
                        "SELECT title FROM movies WHERE title LIKE %s LIMIT 1",
                        (f"%{title}%",)
                    )
                    result = cursor.fetchone()
                    if result:
                        validated_titles.append(result['title'])
        
        logger.info(f"✅ 最终验证通过的电影: {validated_titles}")
        
        # Step 5: 返回结果
        return AgentChatResponse(
            status="success",
            agent_answer=ai_response,
            movie_titles=validated_titles[:5] if validated_titles else None,  # 最多返回5部
            timestamp=datetime.now().isoformat()
        )
        
    except Exception as e:
        logger.error(f"❌ Agent 运行异常: {str(e)}")
        raise HTTPException(status_code=500, detail=f"AI 处理出错: {str(e)}")


# --- 原有的电影检索接口（保持不变）---

@app.get("/movies", tags=["电影检索"], response_model=MovieListResponse)
def list_movies(
    q: Optional[str] = Query(None, description="🔍 搜索关键词（标题/导演/演员）"),
    source: Optional[str] = Query(None, description="来源过滤（douban/imdb）"),
    year: Optional[str] = Query(None, description="年份（精确匹配）"),
    min_rating: Optional[float] = Query(None, ge=0, le=10, description="最低评分"),
    sort_by: Literal["rating", "rating_count", "year", "created_at"] = Query("rating", description="排序字段"),
    order: Literal["asc", "desc"] = Query("desc", description="排序方向"),
    page: int = Query(1, ge=1, description="页码"),
    limit: int = Query(20, ge=1, le=100, description="每页数量"),
    db = Depends(get_db)
):
    """电影列表检索 API"""
    try:
        with db.cursor() as cursor:
            # 构建查询条件
            where_clauses = []
            params = []

            # 智能搜索
            if q:
                where_clauses.append(
                    "(title LIKE %s OR director LIKE %s OR stars LIKE %s)"
                )
                search_pattern = f"%{q}%"
                params.extend([search_pattern, search_pattern, search_pattern])

            if source:
                where_clauses.append("source = %s")
                params.append(source)

            if year:
                where_clauses.append("year = %s")
                params.append(year)

            if min_rating is not None:
                where_clauses.append("rating >= %s")
                params.append(min_rating)

            sql_where = " WHERE " + " AND ".join(where_clauses) if where_clauses else ""

            # 查询总数
            count_sql = f"SELECT COUNT(*) as total FROM movies{sql_where}"
            cursor.execute(count_sql, params)
            total = cursor.fetchone()['total']

            # 分页查询
            offset = (page - 1) * limit
            
            allowed_fields = ["rating", "rating_count", "year", "created_at"]
            if sort_by not in allowed_fields:
                sort_by = "rating"
            
            list_sql = f"""
                SELECT 
                    id, title, year, rating, rating_count, 
                    source, director, stars, cover_url
                FROM movies
                {sql_where}
                ORDER BY {sort_by} {order.upper()}
                LIMIT %s OFFSET %s
            """
            cursor.execute(list_sql, params + [limit, offset])
            items = cursor.fetchall()

            # 计算分页信息
            total_pages = (total + limit - 1) // limit if total > 0 else 0
            has_next = page < total_pages
            has_prev = page > 1

            return {
                "total": total,
                "page": page,
                "limit": limit,
                "total_pages": total_pages,
                "has_next": has_next,
                "has_prev": has_prev,
                "items": items
            }
    
    except pymysql.Error as e:
        logger.error(f"数据库查询错误: {e}")
        raise HTTPException(status_code=500, detail=f"数据库查询失败: {str(e)}")

@app.get("/movies/{movie_id}", tags=["电影检索"], response_model=MovieDetail)
def get_movie_detail(
    movie_id: int = Path(..., description="电影ID"), 
    db = Depends(get_db)
):
    """获取电影完整详情"""
    try:
        with db.cursor() as cursor:
            cursor.execute("SELECT * FROM movies WHERE id = %s", (movie_id,))
            movie = cursor.fetchone()
            
            if not movie:
                raise HTTPException(status_code=404, detail=f"未找到 ID 为 {movie_id} 的电影")
            
            return movie
    
    except pymysql.Error as e:
        logger.error(f"数据库查询错误: {e}")
        raise HTTPException(status_code=500, detail="数据库查询失败")

@app.get("/stats/platforms", tags=["数据统计"])
def platform_stats(db = Depends(get_db)):
    """平台数据分布统计"""
    try:
        with db.cursor() as cursor:
            sql = """
                SELECT 
                    source,
                    COUNT(*) as count,
                    ROUND(AVG(rating), 2) as avg_rating,
                    ROUND(MAX(rating), 2) as max_rating,
                    ROUND(MIN(rating), 2) as min_rating
                FROM movies 
                WHERE source IS NOT NULL
                GROUP BY source
                ORDER BY count DESC
            """
            cursor.execute(sql)
            return cursor.fetchall()
    except pymysql.Error as e:
        logger.error(f"统计查询错误: {e}")
        raise HTTPException(status_code=500, detail="统计查询失败")

@app.get("/surprise-me", tags=["特色功能"], response_model=MovieDetail)
def random_movie(
    min_rating: float = Query(8.0, ge=0, le=10, description="最低评分"),
    db = Depends(get_db)
):
    """随机推荐一部高分电影"""
    try:
        with db.cursor() as cursor:
            sql = """
                SELECT * 
                FROM movies 
                WHERE rating >= %s AND rating IS NOT NULL
                ORDER BY RAND() 
                LIMIT 1
            """
            cursor.execute(sql, (min_rating,))
            movie = cursor.fetchone()
            
            if not movie:
                raise HTTPException(
                    status_code=404,
                    detail=f"未找到评分 >= {min_rating} 的电影"
                )
            
            return movie
    except pymysql.Error as e:
        logger.error(f"推荐查询错误: {e}")
        raise HTTPException(status_code=500, detail="推荐查询失败")

# 挂载静态文件
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/", tags=["前端页面"])
def read_index():
    """直接访问根目录时返回前端页面"""
    return FileResponse('static/index.html')

@app.get("/ui", tags=["前端页面"])
def ui_page():
    """为了方便，也可以通过 /ui 访问"""
    return FileResponse('static/index.html')

@app.get("/health", tags=["系统"])
def health_check(db = Depends(get_db)):
    """健康检查"""
    try:
        with db.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) as count FROM movies")
            result = cursor.fetchone()
            total_movies = result['count']
        
        return {
            "status": "healthy",
            "database": "connected",
            "total_movies": total_movies,
            "ai_agent": "enabled" if movie_assistant else "disabled",
            "timestamp": datetime.now().isoformat()
        }
    except Exception as e:
        return {
            "status": "unhealthy",
            "database": "disconnected",
            "error": str(e)
        }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True
    )