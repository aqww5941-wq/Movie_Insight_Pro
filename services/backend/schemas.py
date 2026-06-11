"""Pydantic 数据模型 — 请求/响应 schema"""

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field


class MovieBase(BaseModel):
    """电影基础模型"""
    id: int
    title: str
    year: Optional[str] = None
    rating: Optional[float] = None
    rating_count: Optional[int] = None
    source: Optional[str] = None
    director: Optional[str] = None
    stars: Optional[str] = None
    cover_url: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class MovieDetail(MovieBase):
    """电影详情模型"""
    url: Optional[str] = None
    summary: Optional[str] = None
    created_at: Optional[datetime] = None


class MovieListResponse(BaseModel):
    """电影列表响应"""
    total: int
    page: int
    limit: int
    total_pages: int
    has_next: bool = False
    has_prev: bool = False
    items: List[MovieBase]


class AgentChatRequest(BaseModel):
    """AI 对话请求"""
    query: str = Field(..., min_length=1, max_length=500, example="推荐几部高分科幻电影")


class AgentChatResponse(BaseModel):
    """AI 对话响应"""
    status: str
    agent_answer: str
    movie_titles: Optional[List[MovieBase]] = None
    timestamp: str


class RagSearchRequest(BaseModel):
    """RAG 检索请求（不经过 Agent）"""
    query: str = Field(..., min_length=1, max_length=500, example="给我推荐和《Just Mercy》类似的电影")


class RagSearchResponse(BaseModel):
    """RAG 检索响应（不经过 Agent）"""
    status: str
    query: str
    total: int
    items: List[MovieBase]
    timestamp: str


class HealthResponse(BaseModel):
    """健康检查响应"""
    status: str
    database: str
    cache: str
    ai_agent: str
    total_movies: int
    timestamp: str
