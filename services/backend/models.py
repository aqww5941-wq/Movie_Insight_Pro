from sqlalchemy import Column, Integer, String, Float, Text, DateTime, func
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
