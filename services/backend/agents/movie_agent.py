import logging
import os
from typing import Any, TypedDict

from dotenv import load_dotenv
from langchain.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import create_react_agent

logger = logging.getLogger(__name__)

try:
    from .tools import get_movie_table_schema, query_movie_db, semantic_movie_search
    from .skills import SKILL_SPECS, init_router, select_skills
except ImportError:
    import sys

    sys.path.append(os.path.dirname(os.path.abspath(__file__)))
    from tools import get_movie_table_schema, query_movie_db, semantic_movie_search
    from skills import SKILL_SPECS, init_router, select_skills

load_dotenv()


class MovieAgentError(RuntimeError):
    pass


class AgentWorkflowState(TypedDict, total=False):
    query: str
    intents: list[str]
    current_intent_index: int
    skill_name: str
    current_query: str
    system_instruction: str
    messages: list[Any]
    strict_retry: bool
    retry_count: int
    intent_results: list[dict[str, str]]
    executed_intents: list[str]
    final_answer: str
    error: str


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
    _SYSTEM_INSTRUCTION = (
        "你是 Movie Insight Pro 的电影数据库 ReAct 助手。\n"
        "你没有任何电影内部知识，必须通过工具观察事实后再回答。\n"
        "执行规则：\n"
        "1) 电影相关问题必须先调用工具，禁止直接凭记忆作答。\n"
        "2) 推荐/氛围/剧情语义类问题，优先调用 semantic_movie_search。\n"
        "3) 结构化筛选/统计/排序类问题，调用 query_movie_db（仅 SELECT）。\n"
        "4) 不确定字段时先调用 get_movie_table_schema。\n"
        "5) 最终回答使用中文，先给结论，再给依据；若调用了语义检索，必须展示相似度得分。\n"
        "5.1) 输出要简洁：总长度控制在 280 字内，最多 5 条要点。\n"
        "6) 如果工具返回空结果，明确告知用户“本地数据库未匹配到结果”，不要编造。\n"
        "7) 控制调用成本：单个子任务最多调用 4 次工具，禁止对同一查询语句重复调用同一工具。"
    )

    def __init__(self):
        api_key = (os.getenv("DASHSCOPE_API_KEY") or "").strip()
        base_url = (os.getenv("DASHSCOPE_BASE_URL") or "https://dashscope.aliyuncs.com/compatible-mode/v1").strip()
        model = (os.getenv("QWEN_MODEL") or "qwen3.5-397b-a17b").strip()

        self.llm = ChatOpenAI(
            model=model,
            temperature=0,
            api_key=api_key,
            base_url=base_url,
        )
        logger.info("MovieAgent LLM 初始化: model=%s, base_url=%s", model, base_url)

        init_router(self.llm)

        self._tool_result_cache: dict[str, str] = {}

        self.raw_tool_map = {
            "get_movie_table_schema": get_movie_table_schema,
            "query_movie_db": query_movie_db,
            "semantic_movie_search": semantic_movie_search,
        }
        self.tool_map = self._build_cached_tool_map()
        self.tools = list(self.tool_map.values())

        self.skill_agents = {}
        for name, spec in SKILL_SPECS.items():
            selected_tools = [
                self.tool_map[tool_name]
                for tool_name in spec.tool_names
                if tool_name in self.tool_map
            ]
            if not selected_tools:
                selected_tools = self.tools
            self.skill_agents[name] = create_react_agent(self.llm, selected_tools)

        self.workflow = self._build_workflow()

    @staticmethod
    def _normalize_cache_text(value: str) -> str:
        return " ".join((value or "").strip().lower().split())

    def _cache_get(self, tool_name: str, payload: str) -> str | None:
        key = f"{tool_name}:{self._normalize_cache_text(payload)}"
        return self._tool_result_cache.get(key)

    def _cache_set(self, tool_name: str, payload: str, result: str) -> str:
        key = f"{tool_name}:{self._normalize_cache_text(payload)}"
        self._tool_result_cache[key] = result
        return result

    def _build_cached_tool_map(self):
        schema_tool = self.raw_tool_map["get_movie_table_schema"]
        sql_tool = self.raw_tool_map["query_movie_db"]
        semantic_tool = self.raw_tool_map["semantic_movie_search"]

        @tool("get_movie_table_schema")
        def get_movie_table_schema_cached() -> str:
            """获取 movies 表结构。"""
            cached = self._cache_get("get_movie_table_schema", "__noargs__")
            if cached is not None:
                logger.info("🧩 Tool cache hit: get_movie_table_schema")
                return cached
            result = schema_tool.invoke({})
            return self._cache_set("get_movie_table_schema", "__noargs__", result)

        @tool("query_movie_db")
        def query_movie_db_cached(sql: str) -> str:
            """执行只读 SQL 查询（仅 SELECT）。"""
            cached = self._cache_get("query_movie_db", sql)
            if cached is not None:
                logger.info("🧩 Tool cache hit: query_movie_db")
                return cached
            result = sql_tool.invoke({"sql": sql})
            return self._cache_set("query_movie_db", sql, result)

        @tool("semantic_movie_search")
        def semantic_movie_search_cached(query: str) -> str:
            """按语义向量检索电影。"""
            cached = self._cache_get("semantic_movie_search", query)
            if cached is not None:
                logger.info("🧩 Tool cache hit: semantic_movie_search | query=%s", query)
                return cached
            result = semantic_tool.invoke({"query": query})
            return self._cache_set("semantic_movie_search", query, result)

        return {
            "get_movie_table_schema": get_movie_table_schema_cached,
            "query_movie_db": query_movie_db_cached,
            "semantic_movie_search": semantic_movie_search_cached,
        }

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

    @staticmethod
    def _has_tool_messages(messages: list[Any]) -> bool:
        for message in messages:
            msg_type = getattr(message, "type", "")
            if msg_type == "tool":
                return True
            if message.__class__.__name__.lower() == "toolmessage":
                return True
        return False

    def _build_intent_query(self, state: AgentWorkflowState, skill_name: str) -> str:
        user_query = state.get("query", "")
        history = state.get("intent_results") or []
        if skill_name == "analytics":
            return (
                "请只执行 analytics 子任务。\n"
                "目标：先完成结构化筛选/排序查询，输出可用于后续对比的候选电影与关键指标。\n"
                "限制：最多调用 3 次工具；拿到候选后立即停止检索并总结。\n"
                f"用户原始问题：{user_query}"
            )
        if skill_name == "comparison":
            previous = "\n\n".join(
                f"[{idx + 1}] {item['intent']}结果:\n{item['answer']}"
                for idx, item in enumerate(history)
            )
            return (
                "请只执行 comparison 子任务。\n"
                "目标：对用户指定对象进行对比分析，必要时补充查询。\n"
                "限制：最多调用 2 次工具；优先复用已完成子任务结果，禁止重复查询同名电影（中英文别名仅保留一次）。\n"
                f"用户原始问题：{user_query}\n"
                f"已完成子任务结果：\n{previous if previous else '无'}"
            )
        if skill_name == "recommendation":
            return (
                "请只执行 recommendation 子任务。\n"
                "目标：根据用户偏好推荐候选电影，并给出推荐理由。\n"
                "限制：最多调用 2 次工具；只检索推荐主题，不要检索对比对象；若已得到 >=3 部候选，立即输出结果。\n"
                f"用户原始问题：{user_query}"
            )
        return user_query

    def _compose_final_answer(self, query: str, intent_results: list[dict[str, str]]) -> str:
        if not intent_results:
            return ""
        if len(intent_results) == 1:
            return intent_results[0].get("answer", "").strip()

        material = "\n\n".join(
            f"[{idx + 1}] {item['intent']}:\n{item['answer']}"
            for idx, item in enumerate(intent_results)
        )
        prompt = (
            "你是电影助手。请把多阶段子任务结果整合为一个中文最终答复。\n"
            "要求：\n"
            "1) 先给结论，再给依据\n"
            "2) 保留关键数据与对比结论\n"
            "3) 不编造，必须基于给定材料\n\n"
            f"用户问题：{query}\n\n"
            f"子任务结果：\n{material}"
        )
        try:
            response = self.llm.invoke([("user", prompt)])
            merged = self._chunk_to_text(getattr(response, "content", "")).strip()
            if merged:
                return merged
        except Exception as e:
            logger.warning("Final answer compose failed: %s", e)
        return "\n\n".join(
            f"{item['intent']}结果：\n{item['answer']}" for item in intent_results if item.get("answer")
        )

    def _build_workflow(self):
        graph = StateGraph(AgentWorkflowState)
        graph.add_node("detect_intents", self._wf_detect_intents)
        graph.add_node("prepare_context", self._wf_prepare_context)
        graph.add_node("select_next_intent", self._wf_select_next_intent)
        graph.add_node("invoke_intent", self._wf_invoke_intent)
        graph.add_node("mark_strict_retry", self._wf_mark_strict_retry)
        graph.add_node("collect_intent_result", self._wf_collect_intent_result)
        graph.add_node("finalize_answer", self._wf_finalize_answer)
        graph.add_node("finalize_error", self._wf_finalize_error)

        graph.add_edge(START, "detect_intents")
        graph.add_edge("detect_intents", "prepare_context")
        graph.add_edge("prepare_context", "select_next_intent")
        graph.add_conditional_edges(
            "select_next_intent",
            self._wf_decide_next_intent,
            {
                "invoke_intent": "invoke_intent",
                "finalize_answer": "finalize_answer",
                "finalize_error": "finalize_error",
            },
        )
        graph.add_conditional_edges(
            "invoke_intent",
            self._wf_decide_after_invoke,
            {
                "collect_intent_result": "collect_intent_result",
                "mark_strict_retry": "mark_strict_retry",
                "finalize_error": "finalize_error",
            },
        )
        graph.add_edge("mark_strict_retry", "invoke_intent")
        graph.add_edge("collect_intent_result", "select_next_intent")
        graph.add_edge("finalize_answer", END)
        graph.add_edge("finalize_error", END)
        return graph.compile()

    def _wf_detect_intents(self, state: AgentWorkflowState) -> AgentWorkflowState:
        query = (state.get("query") or "").strip()
        specs = select_skills(query)
        ordered = [spec.name for spec in specs] or ["general"]
        intents: list[str] = []
        for name in ordered:
            if name not in intents:
                intents.append(name)
        logger.info("MovieAgent workflow intents=%s", intents)
        return {"intents": intents}

    def _wf_prepare_context(self, state: AgentWorkflowState) -> AgentWorkflowState:
        return {
            "current_intent_index": 0,
            "intent_results": [],
            "executed_intents": [],
            "strict_retry": False,
            "retry_count": 0,
        }

    def _wf_select_next_intent(self, state: AgentWorkflowState) -> AgentWorkflowState:
        intents = state.get("intents") or []
        idx = state.get("current_intent_index", 0)
        executed = state.get("executed_intents") or []
        while idx < len(intents) and intents[idx] in executed:
            idx += 1
        if idx >= len(intents):
            return {"current_intent_index": idx}

        skill_name = intents[idx]
        skill = SKILL_SPECS.get(skill_name, SKILL_SPECS["general"])
        intent_query = self._build_intent_query(state, skill_name)
        system_instruction = self._SYSTEM_INSTRUCTION + "\n" + skill.instruction
        logger.info("MovieAgent workflow execute intent[%s]=%s", idx, skill_name)
        return {
            "skill_name": skill_name,
            "current_query": intent_query,
            "system_instruction": system_instruction,
            "strict_retry": False,
            "retry_count": 0,
        }

    def _wf_decide_next_intent(self, state: AgentWorkflowState) -> str:
        intents = state.get("intents") or []
        idx = state.get("current_intent_index", 0)
        if not intents:
            return "finalize_error"
        if idx < len(intents):
            return "invoke_intent"
        return "finalize_answer"

    def _wf_invoke_intent(self, state: AgentWorkflowState) -> AgentWorkflowState:
        skill_name = state.get("skill_name") or "general"
        system_instruction = state.get("system_instruction") or self._SYSTEM_INSTRUCTION
        intent_query = state.get("current_query") or state.get("query") or ""
        strict_retry = state.get("strict_retry", False)

        if strict_retry:
            system_instruction += "\n补充规则：本次回答前必须至少调用 1 次工具。"

        agent = self.skill_agents.get(skill_name, self.skill_agents["general"])
        result = agent.invoke(
            {
                "messages": [
                    ("system", system_instruction),
                    ("user", intent_query),
                ]
            },
            config={"recursion_limit": 8},
        )
        messages = result.get("messages") or []
        return {"messages": messages}

    def _wf_decide_after_invoke(self, state: AgentWorkflowState) -> str:
        messages = state.get("messages") or []
        retry_count = state.get("retry_count", 0)
        if not messages:
            return "finalize_error"
        if self._has_tool_messages(messages):
            return "collect_intent_result"
        if retry_count < 1:
            return "mark_strict_retry"
        return "finalize_error"

    def _wf_mark_strict_retry(self, state: AgentWorkflowState) -> AgentWorkflowState:
        return {
            "strict_retry": True,
            "retry_count": state.get("retry_count", 0) + 1,
        }

    def _wf_collect_intent_result(self, state: AgentWorkflowState) -> AgentWorkflowState:
        messages = state.get("messages") or []
        if not messages:
            return {"error": "暂时没有拿到可用结果，请稍后再试。"}

        final_content = getattr(messages[-1], "content", "")
        final_message = self._chunk_to_text(final_content).strip()
        if not final_message:
            return {"error": "暂时没有拿到可用结果，请稍后再试。"}

        history = list(state.get("intent_results") or [])
        skill_name = state.get("skill_name", "general")
        history.append({"intent": skill_name, "answer": final_message})
        executed = list(state.get("executed_intents") or [])
        if skill_name not in executed:
            executed.append(skill_name)
        return {
            "intent_results": history,
            "executed_intents": executed,
            "current_intent_index": state.get("current_intent_index", 0) + 1,
            "strict_retry": False,
            "retry_count": 0,
        }

    def _wf_finalize_answer(self, state: AgentWorkflowState) -> AgentWorkflowState:
        query = state.get("query", "")
        intent_results = state.get("intent_results") or []
        final_answer = self._compose_final_answer(query, intent_results).strip()
        if not final_answer:
            return {"error": "暂时没有拿到可用结果，请稍后再试。"}
        return {"final_answer": final_answer}

    def _wf_finalize_error(self, state: AgentWorkflowState) -> AgentWorkflowState:
        messages = state.get("messages") or []
        if messages and not self._has_tool_messages(messages):
            return {"error": "Agent 未执行工具调用，无法基于数据库事实作答。"}
        return {"error": "暂时没有拿到可用结果，请稍后再试。"}

    def ask(self, query: str) -> str:
        user_query = (query or "").strip()
        if not user_query:
            return "请告诉我你想查询的电影问题。"
        self._tool_result_cache.clear()
        try:
            result_state = self.workflow.invoke({"query": user_query})
            error = (result_state or {}).get("error")
            if error:
                raise MovieAgentError(error)
            answer = (result_state or {}).get("final_answer", "").strip()
            if not answer:
                return "暂时没有拿到可用结果，请稍后再试。"
            return answer
        except Exception as exc:
            root_message = _extract_root_error_message(exc)
            if _is_noise_message(root_message):
                root_message = "AI 上游服务调用失败（可能是网络、密钥或配额问题）"
            logger.error("MovieAgent 调用失败: %s", root_message, exc_info=True)
            raise MovieAgentError(root_message) from exc


if __name__ == "__main__":
    print("🚀 使用 LangGraph 驱动的 Agent 启动成功！")
    agent = MovieAgent()
    question = "帮我找评分最高的科幻电影，然后和盗梦空间对比一下"
    try:
        response = agent.ask(question)
        print("\n🤖 AI 的回答是：\n")
        print(response)
    except Exception as e:
        print(f"\n❌ 运行出错: {e}")
