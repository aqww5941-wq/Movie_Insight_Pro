from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
import os

# 构建 Postgres 连接字符串 (注意驱动是 asyncpg)
PG_USER = os.getenv("PG_USER", "root")
PG_PASSWORD = os.getenv("PG_PASSWORD", "000000")
PG_HOST = os.getenv("PG_HOST", "db")
PG_PORT = os.getenv("PG_PORT", "5432")
PG_DB = os.getenv("PG_DBNAME", "movie_db")

DATABASE_URL = f"postgresql+asyncpg://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:{PG_PORT}/{PG_DB}"

# 创建异步引擎
engine = create_async_engine(DATABASE_URL, echo=False)

# 创建 Session 工厂
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False
)

async def get_db():
    async with AsyncSessionLocal() as session:
        yield session