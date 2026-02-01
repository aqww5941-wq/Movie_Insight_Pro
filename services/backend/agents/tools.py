import psycopg2
from psycopg2.extras import RealDictCursor
import os
from langchain.tools import tool

# 数据库配置 - 改为 PostgreSQL
DB_CONFIG = {
    "host": os.getenv("POSTGRES_HOST", "movie-postgres"), # 对应 docker-compose 中的服务名
    "port": int(os.getenv("POSTGRES_PORT", 5432)),
    "user": os.getenv("POSTGRES_USER", "root"),
    "password": os.getenv("POSTGRES_PASSWORD", "000000"),
    "dbname": os.getenv("POSTGRES_DB", "movie_db")
}

@tool
def get_movie_table_schema() -> str:
    """
    当你想知道电影表(movies)有哪些字段、字段类型是什么时使用。
    """
    return """
    表名: movies
    字段信息:
    - id (int): 自增主键
    - title (varchar): 电影名称
    - year (varchar): 上映年份 (现在支持 100 字符，可存储 'Expected 2026' 等)
    - rating (float): 评分 (0-10)
    - rating_count (int): 评价人数
    - director (varchar): 导演
    - stars (text): 主演 (已扩容)
    - source (varchar): 来源 (imdb/douban)
    - summary (text): 剧情简介
    - cover_url (varchar): 封面图链接
    """

@tool
def query_movie_db(sql: str) -> str:
    """
    执行 SQL 查询语句来获取电影数据。
    当你确定了要查询的条件时使用。
    注意：只允许 SELECT 语句。PostgreSQL 中模糊查询建议使用 ILIKE。
    """
    try:
        # 使用 psycopg2 连接 Postgres
        conn = psycopg2.connect(**DB_CONFIG, cursor_factory=RealDictCursor)
        with conn.cursor() as cursor:
            # 安全逻辑
            clean_sql = sql.strip().lower()
            if not clean_sql.startswith("select"):
                return "错误：仅支持 SELECT 查询。"
            
            cursor.execute(sql)
            result = cursor.fetchall()
            return str(result) if result else "查询成功，但未找到匹配数据。"
    except Exception as e:
        return f"数据库执行出错: {str(e)}"
    finally:
        if 'conn' in locals():
            conn.close()