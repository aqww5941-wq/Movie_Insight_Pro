"""AI 助手路由：/agent/chat、/agent/chat/stream"""

import asyncio
import json
import logging
import re
from datetime import datetime
from time import perf_counter
from typing import List

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from agents.movie_agent import MovieAgentError
from agents.skills import SkillRouter
from core.cache import cache_manager
from core.exceptions import AIServiceError
from db.database import AsyncSessionLocal, get_db
from schemas import AgentChatRequest, AgentChatResponse, MovieBase
from services.search import fetch_movies_by_titles
from utils.helpers import is_noise_message, extract_root_error_message

logger = logging.getLogger(__name__)
INTENT_RULE_ROUTER = SkillRouter()
router = APIRouter()


def extract_movie_titles(text: str) -> List[str]:
    """从文本中提取电影名称"""
    titles = []
    pattern1 = r"《([^》]+)》"
    titles.extend(re.findall(pattern1, text))
    pattern2 = r"\*\*《?([^*》]+)》?\*\*"
    matches = re.findall(pattern2, text)
    titles.extend([m.strip() for m in matches if m.strip() not in titles])
    return list(set(titles))


def is_smalltalk_query(query: str) -> bool:
    """识别无需调用大模型的闲聊问题"""
    if not query:
        return False
    q = query.strip().lower()
    keywords = [
        "你是谁", "你是干嘛的", "你能做什么", "你会什么", "自我介绍",
        "你可以干什么", "你能干什么", "你能做啥", "你可以做什么", "你能帮我什么",
        "who are you", "what can you do", "introduce yourself",
    ]
    return any(k in q for k in keywords)


def build_smalltalk_answer(query: str) -> str:
    """闲聊问题本地回复"""
    _ = query
    return (
        "我是 Movie Insight Pro 的电影助手。\n"
        "我可以帮你：\n"
        "1. 按关键词快速检索电影（如：高分科幻、悬疑烧脑）。\n"
        "2. 按条件筛选（年份、评分、平台）。\n"
        "3. 做相似影片推荐（如：和《肖申克的救赎》类似）。\n"
        "你可以直接说：推荐几部高分悬疑电影。"
    )


def _sse_event(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


@router.post("/agent/chat", tags=["AI 助手"], response_model=AgentChatResponse)
async def chat_with_agent(
    request_body: AgentChatRequest,
    raw_request: Request,
    db: AsyncSession = Depends(get_db),
):
    """AI 智能助手对话接口（非流式）"""
    app = raw_request.app
    if not app.state.movie_agent:
        raise AIServiceError()

    started_at = perf_counter()
    client_ip = raw_request.headers.get("X-Real-IP") or raw_request.client.host
    logger.info("📍 AI 请求来源: %s | 查询: %s", client_ip, request_body.query)

    try:
        route_labels = INTENT_RULE_ROUTER.route_rule_multi(request_body.query or "")
        logger.info("🧭 chat route decision | labels=%s | query=%s", ",".join(route_labels), request_body.query)

        model_name = ""
        try:
            model_name = str(getattr(getattr(app.state.movie_agent, "llm", None), "model_name", "") or "")
        except Exception:
            model_name = ""
        labels_key = ",".join(route_labels)
        cache_key = f"ai:chat:react:v8:{model_name}:labels[{labels_key}]:q:{request_body.query}"
        cached = await cache_manager.get(cache_key)
        if cached:
            logger.info("⚡ 缓存命中: %s", request_body.query)
            return AgentChatResponse(**json.loads(cached))

        if is_smalltalk_query(request_body.query):
            response = AgentChatResponse(
                status="success",
                agent_answer=build_smalltalk_answer(request_body.query),
                movie_titles=None,
                timestamp=datetime.now().isoformat(),
            )
            await cache_manager.set(cache_key, response.model_dump(), ttl=3600)
            elapsed_ms = int((perf_counter() - started_at) * 1000)
            logger.info("⚡ 闲聊快速回复: %dms | 查询: %s", elapsed_ms, request_body.query)
            return response

        ai_response = await asyncio.to_thread(app.state.movie_agent.ask, request_body.query)
        logger.info("🤖 AI 响应: %d 字符", len(ai_response))

        extracted_titles = extract_movie_titles(ai_response)
        matched_movies = await fetch_movies_by_titles(extracted_titles, db, limit=10) if extracted_titles else []
        movie_results = [MovieBase.model_validate(movie) for movie in matched_movies]

        response = AgentChatResponse(
            status="success",
            agent_answer=ai_response,
            movie_titles=movie_results if movie_results else None,
            timestamp=datetime.now().isoformat(),
        )
        await cache_manager.set(cache_key, response.model_dump(), ttl=86400)

        elapsed_ms = int((perf_counter() - started_at) * 1000)
        logger.info("✅ AI 对话完成: %dms | 查询: %s", elapsed_ms, request_body.query)
        return response

    except MovieAgentError as e:
        detail = str(e)
        if is_noise_message(detail):
            detail = "AI 上游服务调用失败（可能是网络、密钥或配额问题）"
        logger.error("AI 处理异常: %s", detail, exc_info=True)
        raise AIServiceError(detail=f"AI 处理失败: {detail}")
    except Exception as e:
        root_message = extract_root_error_message(e)
        logger.error("AI 处理异常: %s", root_message, exc_info=True)
        raise AIServiceError(detail=f"AI 处理失败: {root_message}")


@router.post("/agent/chat/stream", tags=["AI 助手"])
async def chat_with_agent_stream(
    request_body: AgentChatRequest,
    raw_request: Request,
):
    """AI 流式对话接口（SSE）"""
    app = raw_request.app
    if not app.state.movie_agent:
        raise AIServiceError()

    started_at = perf_counter()
    client_ip = raw_request.headers.get("X-Real-IP") or raw_request.client.host
    logger.info("📍 AI 流式请求来源: %s | 查询: %s", client_ip, request_body.query)

    async def event_generator():
        try:
            if is_smalltalk_query(request_body.query):
                quick_text = build_smalltalk_answer(request_body.query)
                for i in range(0, len(quick_text), 24):
                    yield _sse_event("chunk", {"delta": quick_text[i: i + 24]})
                    await asyncio.sleep(0.01)
                yield _sse_event("done", {
                    "status": "success",
                    "agent_answer": quick_text,
                    "movie_titles": None,
                    "timestamp": datetime.now().isoformat(),
                })
                elapsed_ms = int((perf_counter() - started_at) * 1000)
                logger.info("⚡ 流式闲聊完成: %dms | 查询: %s", elapsed_ms, request_body.query)
                return

            route_labels = INTENT_RULE_ROUTER.route_rule_multi(request_body.query or "")
            logger.info("🧭 stream route decision | labels=%s | query=%s", ",".join(route_labels), request_body.query)

            model_name = ""
            try:
                model_name = str(getattr(getattr(app.state.movie_agent, "llm", None), "model_name", "") or "")
            except Exception:
                model_name = ""
            labels_key = ",".join(route_labels)
            cache_key = f"ai:chat:stream:v1:{model_name}:labels[{labels_key}]:q:{request_body.query}"
            cached = await cache_manager.get(cache_key)
            if cached:
                cached_data = json.loads(cached)
                cached_answer = cached_data.get("agent_answer", "")
                cached_titles = cached_data.get("movie_titles")
                for i in range(0, len(cached_answer), 16):
                    yield _sse_event("chunk", {"delta": cached_answer[i: i + 16]})
                    await asyncio.sleep(0.005)
                yield _sse_event("done", {
                    "status": "success",
                    "agent_answer": cached_answer,
                    "movie_titles": cached_titles,
                    "timestamp": datetime.now().isoformat(),
                })
                elapsed_ms = int((perf_counter() - started_at) * 1000)
                logger.info("⚡ 流式缓存命中: %dms | 查询: %s", elapsed_ms, request_body.query)
                return

            full_answer = ""
            async for event in app.state.movie_agent.astream(request_body.query):
                event_type = event.get("type")

                if event_type == "token":
                    token = event.get("content", "")
                    full_answer += token
                    yield _sse_event("chunk", {"delta": token})
                elif event_type == "tool_start":
                    yield _sse_event("tool_start", {"tool_name": event.get("tool_name", "unknown")})
                elif event_type == "tool_end":
                    yield _sse_event("tool_end", {"tool_name": event.get("tool_name", "unknown")})
                elif event_type == "phase":
                    yield _sse_event("phase", {"intent": event.get("intent", "")})
                elif event_type == "error":
                    yield _sse_event("server_error", {"detail": event.get("message", "AI 处理失败")})
                    return
                elif event_type == "done":
                    full_answer = event.get("answer", full_answer)

            if not full_answer.strip():
                full_answer = "本地数据库未匹配到可用结果。"
                yield _sse_event("chunk", {"delta": full_answer})

            extracted_titles = extract_movie_titles(full_answer)
            if extracted_titles:
                async with AsyncSessionLocal() as db:
                    matched_movies = await fetch_movies_by_titles(extracted_titles, db, limit=10)
            else:
                matched_movies = []
            movie_results = [MovieBase.model_validate(m) for m in matched_movies]

            response_payload = {
                "status": "success",
                "agent_answer": full_answer,
                "movie_titles": [m.model_dump() for m in movie_results] if movie_results else None,
                "timestamp": datetime.now().isoformat(),
            }
            yield _sse_event("done", response_payload)

            try:
                await cache_manager.set(cache_key, response_payload, ttl=3600)
            except Exception:
                pass

            elapsed_ms = int((perf_counter() - started_at) * 1000)
            logger.info("✅ AI 流式对话完成: %dms | 查询: %s", elapsed_ms, request_body.query)

        except MovieAgentError as e:
            detail = str(e)
            if is_noise_message(detail):
                detail = "AI 上游服务调用失败（可能是网络、密钥或配额问题）"
            logger.error("AI 流式处理异常: %s", detail, exc_info=True)
            yield _sse_event("server_error", {"detail": f"AI 处理失败: {detail}"})
        except asyncio.CancelledError:
            client = ""
            try:
                client = f"{raw_request.client.host}:{raw_request.client.port}" if raw_request and raw_request.client else "unknown"
            except Exception:
                client = "unknown"
            logger.warning("⚠️ SSE 任务被取消 | client=%s | 查询: %s", client, request_body.query)
            return
        except Exception as e:
            root_message = extract_root_error_message(e)
            logger.error("AI 流式处理异常: %s", root_message, exc_info=True)
            yield _sse_event("server_error", {"detail": f"AI 处理失败: {root_message}"})

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/agent/chat/stream", tags=["AI 助手"])
async def chat_with_agent_stream_get(
    q: str = Query(..., min_length=1, max_length=500, description="AI 对话查询"),
    raw_request: Request = None,
):
    """SSE GET 版本，供 EventSource 使用。"""
    req = AgentChatRequest(query=q)
    return await chat_with_agent_stream(req, raw_request)
