"""
通用工具：AI Embedding、异常解析、噪声过滤
"""
import os
from typing import Optional

from dotenv import load_dotenv

load_dotenv()


def is_noise_message(text: str) -> bool:
    """过滤 LLM 返回的无意义噪声消息"""
    normalized = text.strip().strip('"\'').lower()
    if normalized == "request":
        return True
    if normalized.startswith("keyerror") and "request" in normalized:
        return True
    return False


def extract_root_error_message(exc: Exception) -> str:
    """递归提取异常链中最底层的错误消息"""
    messages = []
    current = exc
    visited = set()

    while current and id(current) not in visited:
        visited.add(id(current))
        text = str(current).strip()
        if text and not is_noise_message(text):
            messages.append(text)
        current = current.__cause__ or current.__context__

    if messages:
        return messages[-1]
    return "AI 上游服务调用失败（可能是网络、密钥或配额问题）"


class AIAgent:
    @staticmethod
    def _get_client():
        from openai import OpenAI

        api_key = os.getenv("SILICONFLOW_API_KEY")
        if not api_key:
            print("❌ 错误：未配置 SILICONFLOW_API_KEY 环境变量！")
            return None

        return OpenAI(
            api_key=api_key,
            base_url="https://api.siliconflow.cn/v1",
        )

    @staticmethod
    def generate_embedding(text: str) -> Optional[list[float]]:
        """调用硅基流动接口生成文本向量"""
        if not text or not text.strip():
            return None

        client = AIAgent._get_client()
        if not client:
            return None

        try:
            response = client.embeddings.create(
                model="Qwen/Qwen3-Embedding-4B",
                input=text[:2000],
            )
            raw_embedding = response.data[0].embedding
            if len(raw_embedding) > 1536:
                return raw_embedding[:1536]
            return raw_embedding
        except Exception as e:
            print(f"❌ 硅基流动 Embedding 失败: {e}")
            return None

    @staticmethod
    def analyze_movie_plot(plot_text: str) -> str:
        """调用硅基流动大模型分析电影剧情标签"""
        if not plot_text or not plot_text.strip():
            return "剧情简介为空，无法分析"

        client = AIAgent._get_client()
        if not client:
            return "未配置 API KEY"

        try:
            response = client.chat.completions.create(
                model="Qwen/Qwen2.5-7B-Instruct",
                messages=[
                    {
                        "role": "user",
                        "content": (
                            "请简要总结以下电影剧情的三个核心标签，"
                            "用英文逗号隔开，不要包含任何其他废话：\n\n"
                            f"{plot_text}"
                        ),
                    }
                ],
                temperature=0.3,
                max_tokens=50,
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            return f"Error: 硅基流动大模型请求失败: {e}"
