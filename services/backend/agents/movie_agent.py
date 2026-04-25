import os
import logging
from typing import AsyncIterator, Any, List, Dict
from dotenv import load_dotenv

# 1. 导入最新的 LangGraph 预构建组件（这是目前最稳定的方式）
from langgraph.prebuilt import create_react_agent
from langchain_openai import ChatOpenAI

logger = logging.getLogger(__name__)

# 2. 导入你的工具
try:
    from .tools import get_movie_table_schema, query_movie_db, semantic_movie_search
except ImportError:
    import sys
    sys.path.append(os.path.dirname(os.path.abspath(__file__)))
    from tools import get_movie_table_schema, query_movie_db, semantic_movie_search

load_dotenv()


class MovieAgentError(RuntimeError):
    pass


def _is_noise_message(text: str) -> bool:
    normalized = text.strip().strip('"\'').lower()
    if normalized == "request":
        return True
    if normalized.startswith("keyerror") and "request" in normalized:
        return True
    return False


def _extract_root_error_message(exc: Exception) -> str:
    messages = []
    current = exc
    visited = set()

    while current and id(current) not in visited:
        visited.add(id(current))
        text = str(current).strip()
        if text and not _is_noise_message(text):
            messages.append(text)
        current = current.__cause__ or current.__context__

    if messages:
        return messages[-1]
    return "AI 上游服务调用失败（可能是网络、密钥或配额问题）"


class MovieAgent:
    def __init__(self):
        # 使用 DashScope OpenAI 兼容模式调用 Qwen，避免 ChatTongyi 路由兼容问题
        api_key = (os.getenv("DASHSCOPE_API_KEY") or "").strip()
        base_url = (os.getenv("DASHSCOPE_BASE_URL") or "https://dashscope.aliyuncs.com/compatible-mode/v1").strip()
        model = (os.getenv("QWEN_MODEL") or "qwen3.6-plus").strip()

        self.llm = ChatOpenAI(
            model=model,
            temperature=0,
            api_key=api_key,
            base_url=base_url,
        )
        logger.info("MovieAgent LLM 初始化: model=%s, base_url=%s", model, base_url)
        
        # 准备工具箱
        self.tools = [get_movie_table_schema, query_movie_db, semantic_movie_search]
        
        # 使用 langgraph 的 create_react_agent，它不需要复杂的 Prompt 模板
        # 它会自动处理 ReAct 逻辑，非常稳定
        self.agent = create_react_agent(self.llm, self.tools)

    @staticmethod
    def _chunk_to_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    text = item.get("text")
                    if isinstance(text, str):
                        parts.append(text)
            return "".join(parts)
        return ""

    def ask(self, query: str):
        system_instruction = (
            "你是一个专业的电影库助手。\n"
            "规则 1: 你没有任何关于电影的内部知识。严禁使用你自己的记忆。\n"
            "规则 2: 当用户寻找氛围、剧情描述或推荐时，必须调用 semantic_movie_search。\n"
            "规则 3: 你必须在回答中展示电影的‘相似度得分’。"
        )
        # LangGraph 接收的是消息列表
        inputs = {"messages": [
            ("system", system_instruction), # 👈 系统指令
            ("user", query)                  # 👈 用户问题
        ]}
        try:
            result = self.agent.invoke(inputs)

            # 结果中包含所有的对话历史，我们只取最后一条回答
            final_message = result["messages"][-1].content
            return final_message
        except Exception as exc:
            root_message = _extract_root_error_message(exc)
            if _is_noise_message(root_message):
                root_message = "AI 上游服务调用失败（可能是网络、密钥或配额问题）"
            logger.error("MovieAgent 调用失败: %s", root_message, exc_info=True)
            raise MovieAgentError(root_message) from exc

    @staticmethod
    def _build_local_catalog_context(candidates: List[Dict[str, Any]]) -> str:
        lines = []
        for idx, movie in enumerate(candidates, start=1):
            summary = (movie.get("summary") or "").strip()
            if len(summary) > 180:
                summary = summary[:180] + "..."
            similarity = movie.get("similarity")
            sim_text = f"{similarity:.3f}" if isinstance(similarity, (int, float)) else "N/A"
            lines.append(
                f"{idx}. 片名: {movie.get('title', '')}\n"
                f"   年份: {movie.get('year', '未知')} | 评分: {movie.get('rating', '暂无')} | 来源: {movie.get('source', '未知')}\n"
                f"   导演: {movie.get('director', '未知')}\n"
                f"   相似度: {sim_text}\n"
                f"   简介: {summary or '暂无简介'}"
            )
        return "\n".join(lines)

    def ask_grounded(self, query: str, candidates: List[Dict[str, Any]]) -> str:
        """仅基于本地候选电影回答。"""
        catalog_context = self._build_local_catalog_context(candidates)
        system_instruction = (
            "你是本地电影库检索助手。\n"
            "你只能基于给定的“候选电影列表”回答，严禁编造或引入列表外的影视作品。\n"
            "若用户问题与候选电影不完全匹配，也只能在候选范围内给出最相关推荐。\n"
            "回答请使用中文，先给结论，再给推荐理由，保持简洁。"
        )
        user_prompt = (
            f"用户问题：{query}\n\n"
            "候选电影列表（只能从这里选）：\n"
            f"{catalog_context}\n\n"
            "请基于候选列表给出推荐，并尽量提及相似度高的电影。"
        )
        try:
            result = self.llm.invoke([("system", system_instruction), ("user", user_prompt)])
            text = self._chunk_to_text(getattr(result, "content", ""))
            return text.strip() or "我已找到本地候选影片，但暂时无法生成推荐说明。"
        except Exception as exc:
            root_message = _extract_root_error_message(exc)
            if _is_noise_message(root_message):
                root_message = "AI 上游服务调用失败（可能是网络、密钥或配额问题）"
            logger.error("MovieAgent 本地约束回答失败: %s", root_message, exc_info=True)
            raise MovieAgentError(root_message) from exc

    async def stream_answer(self, query: str) -> AsyncIterator[str]:
        """
        直接流式调用大模型，用于前端实时展示 token。
        说明：该路径不走工具调用链，优先保证首字流式体验与稳定性。
        """
        system_instruction = (
            "你是一个专业且友好的电影助手。\n"
            "你可以回答一般问题；当问题与电影相关时，优先给出电影建议。\n"
            "回答请使用中文，简洁、清晰。"
        )
        messages = [
            ("system", system_instruction),
            ("user", query)
        ]

        try:
            async for chunk in self.llm.astream(messages):
                text = self._chunk_to_text(getattr(chunk, "content", ""))
                if text:
                    yield text
        except Exception as exc:
            root_message = _extract_root_error_message(exc)
            if _is_noise_message(root_message):
                root_message = "AI 上游服务调用失败（可能是网络、密钥或配额问题）"
            logger.error("MovieAgent 流式调用失败: %s", root_message, exc_info=True)
            raise MovieAgentError(root_message) from exc

    async def stream_grounded_answer(self, query: str, candidates: List[Dict[str, Any]]) -> AsyncIterator[str]:
        """仅基于本地候选电影进行流式回答。"""
        catalog_context = self._build_local_catalog_context(candidates)
        system_instruction = (
            "你是本地电影库检索助手。\n"
            "你只能基于给定候选电影列表回答，严禁引用列表外影视作品。\n"
            "回答请使用中文，简洁清晰。"
        )
        user_prompt = (
            f"用户问题：{query}\n\n"
            "候选电影列表（只能从这里选）：\n"
            f"{catalog_context}\n\n"
            "请给出推荐结论和简短理由。"
        )
        try:
            async for chunk in self.llm.astream([("system", system_instruction), ("user", user_prompt)]):
                text = self._chunk_to_text(getattr(chunk, "content", ""))
                if text:
                    yield text
        except Exception as exc:
            root_message = _extract_root_error_message(exc)
            if _is_noise_message(root_message):
                root_message = "AI 上游服务调用失败（可能是网络、密钥或配额问题）"
            logger.error("MovieAgent 本地约束流式调用失败: %s", root_message, exc_info=True)
            raise MovieAgentError(root_message) from exc

if __name__ == "__main__":
    print("🚀 使用 LangGraph 驱动的 Agent 启动成功！")
    agent = MovieAgent()
    
    question = "帮我查一下数据库里评分最高的前3部电影是什么？"
    
    try:
        # 直接调用
        response = agent.ask(question)
        print("\n🤖 AI 的回答是：\n")
        print(response)
    except Exception as e:
        print(f"\n❌ 运行出错: {e}")
