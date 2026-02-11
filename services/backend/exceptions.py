"""
异常处理模块
定义自定义异常和全局异常处理器
"""
from fastapi import HTTPException, Request, status
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from typing import Any, Dict
import logging

logger = logging.getLogger(__name__)


class MovieNotFoundError(HTTPException):
    """电影未找到异常"""
    def __init__(self, movie_id: int = None, detail: str = None):
        if detail is None:
            detail = f"电影未找到" if movie_id is None else f"电影 ID {movie_id} 未找到"
        super().__init__(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


class DatabaseError(HTTPException):
    """数据库操作异常"""
    def __init__(self, detail: str = "数据库操作失败"):
        super().__init__(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=detail)


class AIServiceError(HTTPException):
    """AI 服务异常"""
    def __init__(self, detail: str = "AI 服务暂不可用"):
        super().__init__(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail)


async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """处理 HTTP 异常"""
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": exc.detail,
            "status_code": exc.status_code,
            "path": str(request.url)
        }
    )


async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """处理请求验证异常"""
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "error": "请求参数验证失败",
            "details": exc.errors(),
            "path": str(request.url)
        }
    )


async def general_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """处理未捕获的异常"""
    logger.error(f"未处理的异常: {exc}", exc_info=True)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "error": "服务器内部错误",
            "detail": str(exc) if logger.level == logging.DEBUG else "请联系管理员",
            "path": str(request.url)
        }
    )
