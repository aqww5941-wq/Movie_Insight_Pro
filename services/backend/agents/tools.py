import psycopg2
from psycopg2.extras import RealDictCursor
import os
import sys
from langchain.tools import tool

# 确保能导入 utils.helpers 中的 AIAgent
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils.helpers import AIAgent

# 数据库配置
DB_CONFIG = {
    "host": os.getenv("PG_HOST", "db"), # 对应 docker-compose 中的服务名
    "port": int(os.getenv("PG_PORT", 5432)),
    "user": os.getenv("PG_USER", "root"),
    "password": os.getenv("PG_PASSWORD", "000000"),
    "dbname": os.getenv("PG_DBNAME", "movie_db")
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
    - stars (text): 主演
    - source (varchar): 来源
    - summary (text): 剧情简介
    - embedding (vector): 1536维语义向量 (用于氛围/含义搜索)
    """

@tool
def semantic_movie_search(query: str) -> str:
    """
    [新增核心功能] 当用户描述某种“氛围”、“类型”或电影内容，而不是特定标题时使用。
    例如：“帮我找几部像《星际穿越》那样震撼的科幻片” 或 “想看点温馨治愈的电影”。
    """
    print(f"🚀 [DEBUG] 向量检索被触发！查询内容: {query}") # 调试
    try:
        # 1. 将用户的提问转化为 1536 维向量
        query_vector = AIAgent.generate_embedding(query)
        if not query_vector:
            return "无法通过 AI 生成向量，请检查 API 配置。"

        # 2. 连接数据库进行向量检索
        conn = psycopg2.connect(**DB_CONFIG, cursor_factory=RealDictCursor)
        with conn.cursor() as cursor:
            # 使用 <=> (余弦距离) 算子。距离越小相似度越高。
            # 1 - (embedding <=> query_vector) 即为相似度
            sql = """
            SELECT title, year, rating, summary, 
                   (1 - (embedding <=> %s::vector)) AS similarity
            FROM movies
            WHERE embedding IS NOT NULL
            ORDER BY embedding <=> %s::vector
            LIMIT 5;
            """
            # 注意：psycopg2 传入列表时，需要转为字符串格式让 pgvector 识别
            vector_str = "[" + ",".join(map(str, query_vector)) + "]"
            
            cursor.execute(sql, (vector_str, vector_str))
            results = cursor.fetchall()
            
            if not results:
                return "在数据库中未找到语义匹配的电影。"

            # 格式化输出给 AI
            formatted_res = []
            for r in results:
                formatted_res.append(
                    f"电影:《{r['title']}》({r['year']})\n"
                    f"评分: {r['rating']} | 相似度: {round(r['similarity'], 3)}\n"
                    f"简介: {r['summary'][:100]}...\n"
                )
            
            return "\n".join(formatted_res)

    except Exception as e:
        return f"语义搜索执行出错: {str(e)}"
    finally:
        if 'conn' in locals():
            conn.close()

@tool
def query_movie_db(sql: str) -> str:
    """
    执行 SQL 查询语句。当你确定了要查询的具体条件（如导演、年份、评分）时使用。
    注意：只允许 SELECT 语句。
    """
    # ... 你原有的代码保持不变 ...
    try:
        conn = psycopg2.connect(**DB_CONFIG, cursor_factory=RealDictCursor)
        with conn.cursor() as cursor:
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