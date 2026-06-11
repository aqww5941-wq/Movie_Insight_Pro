"""
缓存管理模块
提供 Redis 缓存的封装和管理
"""
import json
import logging
from typing import Any, Optional
from functools import wraps
import redis.asyncio as redis
from core.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


class CacheManager:
    """Redis 缓存管理器"""
    
    def __init__(self):
        self.redis: Optional[redis.Redis] = None
        self._initialized = False
    
    async def initialize(self):
        """初始化 Redis 连接"""
        try:
            self.redis = redis.from_url(
                settings.redis_url,
                encoding="utf-8",
                decode_responses=True,
                socket_connect_timeout=5,
                socket_keepalive=True
            )
            await self.redis.ping()
            self._initialized = True
            logger.info("✅ Redis 缓存服务初始化成功")
        except Exception as e:
            logger.warning(f"⚠️ Redis 连接失败，缓存功能将被禁用: {e}")
            self.redis = None
            self._initialized = False
    
    async def get(self, key: str) -> Optional[str]:
        """获取缓存"""
        if not self._initialized or not self.redis:
            return None
        try:
            return await self.redis.get(key)
        except Exception as e:
            logger.warning(f"缓存读取失败: {e}")
            return None
    
    async def set(self, key: str, value: Any, ttl: int = None) -> bool:
        """设置缓存"""
        if not self._initialized or not self.redis:
            return False
        try:
            ttl = ttl or settings.cache_ttl
            if isinstance(value, (dict, list)):
                value = json.dumps(value, default=str)
            await self.redis.setex(key, ttl, value)
            return True
        except Exception as e:
            logger.warning(f"缓存写入失败: {e}")
            return False
    
    async def delete(self, key: str) -> bool:
        """删除缓存"""
        if not self._initialized or not self.redis:
            return False
        try:
            await self.redis.delete(key)
            return True
        except Exception as e:
            logger.warning(f"缓存删除失败: {e}")
            return False
    
    async def clear_pattern(self, pattern: str) -> int:
        """清除匹配模式的缓存"""
        if not self._initialized or not self.redis:
            return 0
        try:
            keys = []
            async for key in self.redis.scan_iter(match=pattern):
                keys.append(key)
            if keys:
                return await self.redis.delete(*keys)
            return 0
        except Exception as e:
            logger.warning(f"批量删除缓存失败: {e}")
            return 0
    
    async def close(self):
        """关闭 Redis 连接"""
        if self.redis:
            await self.redis.close()
            logger.info("Redis 连接已关闭")


# 全局缓存管理器实例
cache_manager = CacheManager()


def cached(key_prefix: str, ttl: int = None):
    """缓存装饰器"""
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            # 生成缓存键
            cache_key = f"{key_prefix}:{':'.join(str(arg) for arg in args)}:{':'.join(f'{k}={v}' for k, v in kwargs.items())}"
            
            # 尝试从缓存获取
            cached_value = await cache_manager.get(cache_key)
            if cached_value:
                logger.debug(f"缓存命中: {cache_key}")
                return json.loads(cached_value)
            
            # 执行函数
            result = await func(*args, **kwargs)
            
            # 存入缓存
            await cache_manager.set(cache_key, result, ttl)
            return result
        
        return wrapper
    return decorator
