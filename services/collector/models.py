from sqlalchemy import Column, Integer, String, Float, Text, DateTime, func, Index
from sqlalchemy.orm import DeclarativeBase
from pgvector.sqlalchemy import Vector
# ==========================================
# 1. ORM 模型定义 (独立定义以解耦)
# ==========================================

class Base(DeclarativeBase):
    pass

class Movie(Base):
    __tablename__ = "movies"

    id = Column(Integer, primary_key=True, autoincrement=True, index=True)
    title = Column(String(255), nullable=False)
    year = Column(String(100), nullable=True)
    rating = Column(Float)
    rating_count = Column(Integer, default=0)
    source = Column(String(50))
    # ⚠️ 关键：URL 必须是唯一索引，才能触发 ON CONFLICT 更新
    url = Column(String(500), unique=True, nullable=False) 
    director = Column(String(500))
    stars = Column(Text)
    summary = Column(Text)
    cover_url = Column(String(500))

    embedding = Column(Vector(1536), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # 🚀 在模型末尾添加复合索引声明
    __table_args__ = (

        Index(
            "movies_embedding_hnsw_idx", 
            embedding, 
            postgresql_using='hnsw', 
            postgresql_with={'m': 16, 'ef_construction': 64},
            postgresql_ops={'embedding': 'vector_cosine_ops'}
        ),
        # 2. 对应你的 init.sql：Trigram 模糊搜索索引
        # 注意：这里我们手动映射你 SQL 里的联合字段搜索逻辑
        Index(
            "idx_movie_search_trgm",
            func.coalesce(title, '').concat(' ').concat(func.coalesce(summary, '')),
            postgresql_using='gin',
            postgresql_ops={None: 'gin_trgm_ops'} # 对表达式使用 trgm 插件
        ),
    )