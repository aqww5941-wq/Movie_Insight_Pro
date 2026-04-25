"""
配置管理模块
集中管理应用配置，支持从环境变量和配置文件加载
"""
from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache


class Settings(BaseSettings):
    """应用配置类"""
    
    # 应用基础配置
    app_name: str = "Movie Insight Pro"
    app_version: str = "4.0.0"
    debug: bool = False
    log_level: str = "INFO"
    
    # 数据库配置
    pg_user: str = "root"
    pg_password: str = "000000"
    pg_host: str = "db"
    pg_port: int = 5432
    pg_dbname: str = "movie_db"
    
    # Redis 配置
    redis_host: str = "redis"
    redis_port: int = 6379
    redis_password: str = ""
    redis_db: int = 0
    cache_ttl: int = 3600  # 缓存过期时间（秒）
    
    # AI 配置
    dashscope_api_key: str = ""
    ai_request_timeout_seconds: int = 15
    ai_similarity_threshold: float = 0.16
    ai_retrieval_limit: int = 8
    ai_hybrid_vector_weight: float = 0.65
    ai_hybrid_text_weight: float = 0.35
    ai_hnsw_candidate_pool: int = 80
    ai_rrf_k: int = 60
    ai_rrf_weight: float = 0.20
    search_typo_similarity_threshold: float = 0.16
    search_vector_similarity_threshold: float = 0.45
    
    # API 配置
    api_prefix: str = "/api/v1"
    cors_origins: list = ["*"]
    
    # 分页配置
    default_page_size: int = 20
    max_page_size: int = 100
    
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore"
    )
    
    @property
    def database_url(self) -> str:
        """构建数据库连接字符串"""
        return f"postgresql+asyncpg://{self.pg_user}:{self.pg_password}@{self.pg_host}:{self.pg_port}/{self.pg_dbname}"
    
    @property
    def redis_url(self) -> str:
        """构建 Redis 连接字符串"""
        if self.redis_password:
            return f"redis://:{self.redis_password}@{self.redis_host}:{self.redis_port}/{self.redis_db}"
        return f"redis://{self.redis_host}:{self.redis_port}/{self.redis_db}"


@lru_cache
def get_settings() -> Settings:
    """获取配置单例"""
    return Settings()
