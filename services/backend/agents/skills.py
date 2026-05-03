import logging
import os
import re
import sys
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

try:
    sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from utils.helpers import AIAgent
except ImportError:
    AIAgent = None

EMBEDDING_DIRECT_THRESHOLD = 0.70
EMBEDDING_LLM_THRESHOLD = 0.60
EMBEDDING_MARGIN_THRESHOLD = 0.08

COMPARISON_KEYWORDS = ("对比", "比较", "区别", "差异", "vs", "versus")
RECOMMENDATION_KEYWORDS = (
    "推荐", "类似", "想看", "片单", "氛围", "治愈", "烧脑", "好看", "适合", "similar", "recommend"
)
ANALYTICS_KEYWORDS = (
    "最高", "最低", "top", "排名", "统计", "多少", "数量", "均分", "平均", "评分", "年份", "导演", "演员"
)


@dataclass(frozen=True)
class SkillSpec:
    name: str
    tool_names: tuple[str, ...]
    instruction: str
    description: str


SKILL_SPECS: dict[str, SkillSpec] = {
    "recommendation": SkillSpec(
        name="recommendation",
        tool_names=("semantic_movie_search", "get_movie_table_schema"),
        instruction=(
            "当前技能：电影推荐。\n"
            "优先使用 semantic_movie_search 进行语义检索，输出时给出推荐理由与相似度。"
        ),
        description=(
            "根据用户的兴趣、情绪、题材偏好推荐相似电影，"
            "适用于“想看类似xx”“有没有推荐”等场景"
        ),
    ),
    "analytics": SkillSpec(
        name="analytics",
        tool_names=("query_movie_db", "get_movie_table_schema"),
        instruction=(
            "当前技能：结构化查询与统计。\n"
            "优先使用 query_movie_db 生成 SQL 查询，必要时先看表结构。"
        ),
        description=(
            "对电影数据进行结构化查询与统计分析，例如评分排名、数量统计、"
            "平均值、按年份或导演筛选"
        ),
    ),
    "comparison": SkillSpec(
        name="comparison",
        tool_names=("query_movie_db", "semantic_movie_search", "get_movie_table_schema"),
        instruction=(
            "当前技能：影片对比。\n"
            "先检索对比对象，再从评分、年份、类型特征等维度给出对比结论。"
        ),
        description=(
            "对两部或多部电影进行对比分析，包括评分、风格、类型、剧情特点等维度"
        ),
    ),
    "general": SkillSpec(
        name="general",
        tool_names=("semantic_movie_search", "query_movie_db", "get_movie_table_schema"),
        instruction=(
            "当前技能：通用电影问答。\n"
            "根据问题类型自行选择最合适工具，禁止跳过工具直接回答。"
        ),
        description="通用电影问答，根据问题自动选择最合适的检索工具",
    ),
}


class SkillRouter:
    def __init__(self, llm=None):
        self.llm = llm
        self.skill_embeddings_cache: dict[str, list[float]] = {}

    def _get_skill_embedding(self, skill_name: str) -> Optional[list[float]]:
        """获取或计算 skill 描述的 embedding"""
        if skill_name in self.skill_embeddings_cache:
            return self.skill_embeddings_cache[skill_name]

        spec = SKILL_SPECS.get(skill_name)
        if not spec or not AIAgent:
            return None

        try:
            embedding = AIAgent.generate_embedding(spec.description)
            if embedding:
                self.skill_embeddings_cache[skill_name] = embedding
            return embedding
        except Exception as e:
            logger.warning(f"Failed to generate embedding for skill {skill_name}: {e}")
            return None

    def _cosine_similarity(self, a: list[float], b: list[float]) -> float:
        """计算两个向量的余弦相似度"""
        if not a or not b or len(a) != len(b):
            return 0.0
        dot_product = sum(x * y for x, y in zip(a, b))
        norm_a = sum(x * x for x in a) ** 0.5
        norm_b = sum(x * x for x in b) ** 0.5
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot_product / (norm_a * norm_b)

    @staticmethod
    def _extract_skill_label(raw: str) -> Optional[str]:
        text = (raw or "").strip().lower()
        if text in SKILL_SPECS:
            return text
        match = re.search(r"\b(recommendation|analytics|comparison|general)\b", text)
        if match:
            return match.group(1)
        return None

    def route_embedding(self, query: str) -> tuple[Optional[SkillSpec], float, float]:
        """基于 embedding 相似度进行主路由，返回(top1直出结果, top1分数, top2分数)"""
        if not query or not AIAgent:
            return None, -1.0, -1.0

        try:
            query_embedding = AIAgent.generate_embedding(query)
            if not query_embedding:
                return None, -1.0, -1.0

            scored_skills: list[tuple[str, float]] = []
            for skill_name in SKILL_SPECS:
                skill_embedding = self._get_skill_embedding(skill_name)
                if not skill_embedding:
                    continue
                score = self._cosine_similarity(query_embedding, skill_embedding)
                logger.debug(f"Skill {skill_name} embedding score: {score:.3f}")
                scored_skills.append((skill_name, score))

            if not scored_skills:
                return None, -1.0, -1.0

            scored_skills.sort(key=lambda item: item[1], reverse=True)
            top1_name, top1_score = scored_skills[0]
            top2_score = scored_skills[1][1] if len(scored_skills) > 1 else 0.0
            gap = top1_score - top2_score

            logger.info(
                "🧠 Embedding top1=%s(%.3f), top2=%.3f, gap=%.3f",
                top1_name,
                top1_score,
                top2_score,
                gap,
            )

            if top1_score >= EMBEDDING_DIRECT_THRESHOLD and gap > EMBEDDING_MARGIN_THRESHOLD:
                logger.info("🧠 Route by embedding direct hit: %s", top1_name)
                return SKILL_SPECS[top1_name], top1_score, top2_score

            return None, top1_score, top2_score
        except Exception as e:
            logger.warning(f"Embedding routing failed: {e}")
            return None, -1.0, -1.0

    def route_llm(self, query: str) -> Optional[SkillSpec]:
        """低置信度场景下由 LLM 裁决技能"""
        if not self.llm or not query:
            return None

        try:
            prompt = (
                "你是一个意图分类路由器。\n"
                "任务：根据用户问题在以下标签中选择一个最合适的标签：\n"
                "recommendation, analytics, comparison, general\n\n"
                "输出约束（必须严格遵守）：\n"
                "1) 只能输出一个标签\n"
                "2) 不要输出解释、标点、引号、JSON、代码块\n"
                "3) 输出必须是小写\n\n"
                f"用户问题：{query}"
            )
            response = self.llm.invoke([("user", prompt)])
            skill_name = self._extract_skill_label(getattr(response, "content", ""))
            if skill_name in SKILL_SPECS:
                logger.info("🤖 Route by LLM decider: %s", skill_name)
                return SKILL_SPECS[skill_name]
            logger.warning("LLM router returned invalid label: %s", getattr(response, "content", ""))
            return None
        except Exception as e:
            logger.warning(f"LLM routing failed: {e}")
            return None

    def route_rule(self, query: str) -> SkillSpec:
        """基于关键词规则选择技能（兜底）"""
        q = (query or "").lower().strip()
        if not q:
            return SKILL_SPECS["general"]

        if any(keyword in q for keyword in COMPARISON_KEYWORDS):
            logger.info("📋 Route by rule: comparison")
            return SKILL_SPECS["comparison"]
        if any(keyword in q for keyword in RECOMMENDATION_KEYWORDS):
            logger.info("📋 Route by rule: recommendation")
            return SKILL_SPECS["recommendation"]
        if any(keyword in q for keyword in ANALYTICS_KEYWORDS):
            logger.info("📋 Route by rule: analytics")
            return SKILL_SPECS["analytics"]
        logger.info("📋 Route by rule: general (default)")
        return SKILL_SPECS["general"]

    def route_rule_multi(self, query: str) -> list[str]:
        """基于规则识别多意图，返回有序 skill 名称列表。"""
        q = (query or "").lower().strip()
        if not q:
            return ["general"]

        labels: list[str] = []
        if any(keyword in q for keyword in ANALYTICS_KEYWORDS):
            labels.append("analytics")
        if any(keyword in q for keyword in RECOMMENDATION_KEYWORDS):
            labels.append("recommendation")
        if any(keyword in q for keyword in COMPARISON_KEYWORDS):
            labels.append("comparison")

        if not labels:
            return ["general"]
        if "comparison" in labels and len(labels) > 1:
            labels = [label for label in labels if label != "comparison"] + ["comparison"]

        # 保持顺序去重
        deduped: list[str] = []
        for label in labels:
            if label not in deduped:
                deduped.append(label)
        return deduped

    def route_llm_multi(self, query: str) -> list[str]:
        """让 LLM 识别多意图，仅用于多标签判定。"""
        if not self.llm or not query:
            return []

        try:
            prompt = (
                "你是一个意图路由器。\n"
                "任务：识别用户问题涉及的一个或多个技能标签。\n"
                "可选标签：recommendation, analytics, comparison, general\n\n"
                "输出约束（必须严格遵守）：\n"
                "1) 仅输出标签，多个标签用英文逗号分隔\n"
                "2) 不要解释，不要 JSON，不要代码块\n"
                "3) 只允许使用上述四个标签\n"
                "4) 如果是“先查再对比”这类请求，应输出 analytics,comparison\n\n"
                f"用户问题：{query}"
            )
            response = self.llm.invoke([("user", prompt)])
            raw = (getattr(response, "content", "") or "").strip().lower()
            labels = re.findall(r"\b(recommendation|analytics|comparison|general)\b", raw)
            if not labels:
                return []

            deduped: list[str] = []
            for label in labels:
                if label not in deduped:
                    deduped.append(label)

            if "comparison" in deduped and len(deduped) > 1:
                deduped = [label for label in deduped if label != "comparison"] + ["comparison"]

            return deduped
        except Exception as e:
            logger.warning("LLM multi-intent routing failed: %s", e)
            return []

    def select_skill(self, query: str) -> SkillSpec:
        """三层路由：Embedding 主路由 -> LLM 低置信度裁决 -> Rule 兜底"""
        if not query:
            return SKILL_SPECS["general"]

        result, top1_score, top2_score = self.route_embedding(query)
        if result is not None:
            return result

        if top1_score >= EMBEDDING_LLM_THRESHOLD:
            logger.info(
                "🤖 Enter LLM decider: top1=%.3f, top2=%.3f",
                top1_score,
                top2_score,
            )
            result = self.route_llm(query)
            if result:
                return result
        else:
            logger.info(
                "📋 Skip LLM decider due to low embedding score: top1=%.3f",
                top1_score,
            )

        return self.route_rule(query)

    def select_skills(self, query: str) -> list[SkillSpec]:
        """
        多意图路由：
        1) Rule 多标签识别（优先，命中时可避免额外 LLM 路由耗时）
        2) LLM 多标签识别（补充）
        3) 单意图 select_skill
        """
        if not query:
            return [SKILL_SPECS["general"]]

        rule_labels = self.route_rule_multi(query)
        if len(rule_labels) >= 2 and "general" not in rule_labels:
            logger.info("📋 Multi-intent route by rule: %s", rule_labels)
            return [SKILL_SPECS[label] for label in rule_labels if label in SKILL_SPECS]

        llm_labels = self.route_llm_multi(query)
        if len(llm_labels) >= 2:
            logger.info("🤖 Multi-intent route by LLM: %s", llm_labels)
            return [SKILL_SPECS[label] for label in llm_labels if label in SKILL_SPECS]

        return [self.select_skill(query)]


_router_instance: Optional[SkillRouter] = None


def init_router(llm=None):
    """初始化全局 router（在 MovieAgent 初始化时调用）"""
    global _router_instance
    _router_instance = SkillRouter(llm)


def select_skill(query: str) -> SkillSpec:
    """便利函数：选择技能"""
    global _router_instance
    if _router_instance is None:
        _router_instance = SkillRouter()
    return _router_instance.select_skill(query)


def select_skills(query: str) -> list[SkillSpec]:
    """便利函数：多意图选择技能列表"""
    global _router_instance
    if _router_instance is None:
        _router_instance = SkillRouter()
    return _router_instance.select_skills(query)
