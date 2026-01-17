import pymysql
import os
from langchain.tools import tool

# 数据库配置
DB_CONFIG = {
    "host": os.getenv("MYSQL_HOST", "localhost"),
    "port": int(os.getenv("MYSQL_PORT", 3306)),
    "user": os.getenv("MYSQL_USER", "root"),
    "password": os.getenv("MYSQL_ROOT_PASSWORD", "000000"),
    "database": os.getenv("MYSQL_DATABASE", "movie_db"),
    "charset": 'utf8mb4',
    "cursorclass": pymysql.cursors.DictCursor
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
    - year (varchar): 上映年份
    - rating (float): 评分 (0-10)
    - rating_count (int): 评价人数
    - director (varchar): 导演
    - stars (varchar): 主演
    - source (varchar): 来源(douban/imdb)
    - summary (text): 剧情简介
    """

@tool
def query_movie_db(sql: str) -> str:
    """
    执行 SQL 查询语句来获取电影数据。
    当你确定了要查询的条件（如特定的导演、评分范围）时使用。
    注意：只允许 SELECT 语句。
    """
    try:
        conn = pymysql.connect(**DB_CONFIG)
        with conn.cursor() as cursor:
            # 安全逻辑
            if not sql.strip().lower().startswith("select"):
                return "错误：仅支持 SELECT 查询。"
            
            cursor.execute(sql)
            result = cursor.fetchall()
            return str(result) if result else "查询成功，但未找到匹配数据。"
    except Exception as e:
        return f"数据库执行出错: {str(e)}"
    finally:
        if 'conn' in locals():
            conn.close()