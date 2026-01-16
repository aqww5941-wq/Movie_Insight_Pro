from fastapi import FastAPI, Query, HTTPException, Depends, Path
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import pymysql
import os
import logging
from typing import List, Optional, Literal
from datetime import datetime
from fastapi.staticfiles import StaticFiles  # 1. 导入
from fastapi.responses import FileResponse      # 2. 导入

# --- 日志配置 ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- FastAPI 应用 ---
app = FastAPI(
    title="🎬 Movie Insight Pro",
    description="专业的电影数据检索系统",
    version="3.0.0"
)

# 添加 CORS 支持
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- 数据模型 ---
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
    cover_url: Optional[str] = None  # <-- 必须加上这一行

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
    has_next: bool  # 是否有下一页
    has_prev: bool  # 是否有上一页
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

# --- API 接口 ---


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

# 必须先挂载目录，否则 FileResponse 找不到文件
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