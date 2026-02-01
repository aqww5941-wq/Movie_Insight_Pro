import os
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from models import Movie, Base
from utils.helpers import AIAgent
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("backfill")

# 数据库连接
db_url = f"postgresql+psycopg2://{os.getenv('PG_USER')}:{os.getenv('PG_PASSWORD')}@{os.getenv('PG_HOST')}:{os.getenv('PG_PORT')}/{os.getenv('PG_DBNAME')}"
engine = create_engine(db_url)
SessionLocal = sessionmaker(bind=engine)

def backfill():
    session = SessionLocal()
    # 1. 查找所有没有向量但有简介的电影
    stmt = select(Movie).where(Movie.embedding.is_(None), Movie.summary != "")
    movies = session.execute(stmt).scalars().all()
    
    logger.info(f"🔍 发现 {len(movies)} 条待处理的旧数据")
    
    for movie in movies:
        try:
            logger.info(f"🧠 正在转换: {movie.title}")
            vector = AIAgent.generate_embedding(movie.summary)
            if vector:
                movie.embedding = vector
                session.commit()
                logger.info(f"✅ 成功")
        except Exception as e:
            logger.error(f"❌ 失败 {movie.title}: {e}")
            session.rollback()
    
    session.close()
    logger.info("🏁 补全任务结束")

if __name__ == "__main__":
    backfill()