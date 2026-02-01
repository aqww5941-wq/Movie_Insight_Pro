import os
from dotenv import load_dotenv

# 1. 导入最新的 LangGraph 预构建组件（这是目前最稳定的方式）
from langgraph.prebuilt import create_react_agent
from langchain_community.chat_models import ChatTongyi
from langchain_core.messages import HumanMessage, SystemMessage # 导入 SystemMessage

# 2. 导入你的工具
try:
    from .tools import get_movie_table_schema, query_movie_db, semantic_movie_search
except ImportError:
    import sys
    sys.path.append(os.path.dirname(os.path.abspath(__file__)))
    from tools import get_movie_table_schema, query_movie_db, semantic_movie_search

load_dotenv()


class MovieAgent:
    def __init__(self):
        # 初始化通义千问
        self.llm = ChatTongyi(model="qwen-plus", temperature=0)
        
        # 准备工具箱
        self.tools = [get_movie_table_schema, query_movie_db, semantic_movie_search]
        
        # 使用 langgraph 的 create_react_agent，它不需要复杂的 Prompt 模板
        # 它会自动处理 ReAct 逻辑，非常稳定
        self.agent = create_react_agent(self.llm, self.tools)

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
        result = self.agent.invoke(inputs)
        
        # 结果中包含所有的对话历史，我们只取最后一条回答
        final_message = result["messages"][-1].content
        return final_message

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