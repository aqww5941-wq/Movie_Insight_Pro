# mySpider/utils/helpers.py
import smtplib
from dotenv import load_dotenv
from email.mime.text import MIMEText
from email.header import Header
import re
import os

from http import HTTPStatus

load_dotenv()

class DataCleaner:
    @staticmethod
    def clean_cover_url(url):
        """
        清洗图片URL，统一转换为高清原图链接
        """
        if not url:
            return ""
            
        # 1. 处理 IMDb 图片逻辑
        if "media-amazon.com" in url:
            # 去掉 @ 符号后的所有裁剪参数，直接拿原图
            return url.split('@')[0] + "@.jpg"
            
        # 2. 处理 豆瓣 图片逻辑
        if "doubanio.com" in url:
            # 将小图/中图标识替换为大图标识
            url = url.replace('/s_ratio_poster/', '/l_ratio_poster/') \
                     .replace('/m_ratio_poster/', '/l_ratio_poster/')
            # 兼容其他路径格式
            url = re.sub(r'/(s|m)_', '/l_', url)
            return url
            
        return url

    @staticmethod
    def clean_rating_count(count_str):
        """
        完美复刻 Pipeline 逻辑：将 '2.9M', '(150,000)', 'no rating' 等转换为纯数字
        """
        if not count_str:
            return 0
        s = str(count_str).strip().lower()
        if 'no' in s or not s:
            return 0
            
        s  = re.sub(r'[^\d\.mk]', '', s)
        
        try:
    
            if 'm' in s:
                return int(float(s.replace('m', '')) * 1000000)
            elif 'k' in s:
                return int(float(s.replace('k', '')) * 1000)
            else:
                # 使用正则表达式提取数字更稳健，防止有其他杂质字符
                match = re.search(r'\d+\.?\d*', s)
                if match:
                    return int(float(match.group()))
                return 0
        except (ValueError, TypeError):
            # 对应你原来的 except ValueError: count = 0
            return 0
    @staticmethod
    def send_email(subject, content):
        """
        发送邮件简报
        """
        # --- 配置信息 ---
        smtp_server = "smtp.qq.com"  # 如果是网易则为 smtp.163.com
        smtp_port = 465               # SSL 端口
        sender = os.getenv('EMAIL_SENDER')
        password = os.getenv('EMAIL_PASSWORD')
        receiver = os.getenv('EMAIL_RECEIVER')

        # --- 构造邮件 ---
        message = MIMEText(content, 'plain', 'utf-8')
        message['From'] = sender
        message['To'] = receiver
        message['Subject'] = Header(subject, 'utf-8')

        try:
            # 使用 SSL 安全连接
            server = smtplib.SMTP_SSL(smtp_server, smtp_port)
            server.login(sender, password)
            server.sendmail(sender, [receiver], message.as_string())
            server.quit()
            return True
        except Exception as e:
            print(f"邮件发送失败: {e}")
            return False


class AIAgent:
    @staticmethod
    def _get_client():
        """
        [内部方法] 初始化并返回硅基流动的 OpenAI 客户端
        """
        from openai import OpenAI
        api_key = os.getenv('SILICONFLOW_API_KEY')
        if not api_key:
            print("❌ 错误：未配置 SILICONFLOW_API_KEY 环境变量！")
            return None

        # 硅基流动的统一标准 API 入口
        return OpenAI(
            api_key=api_key,
            base_url="https://api.siliconflow.cn/v1"
        )

    @staticmethod
    def generate_embedding(text):
        """
        [已修改] 调用硅基流动接口将电影简介转换为向量
        使用推荐模型: BAAI/bge-m3 (1024维) 或 text-embedding-ada-002 兼容模型
        🚨 注意：如果你的 pgvector 数据库之前锁死了 1536 维，请使用 text-embedding-ada-002
        """
        if not text or not text.strip():
            return None

        client = AIAgent._get_client()
        if not client:
            return None

        try:
            # 硅基流动支持多种 Embedding 模型：
            # 1. "BAAI/bge-m3" (1024维，文本检索效果极佳，极度推荐)
            # 2. "BAAI/bge-large-zh-v1.5" (1024维)
            # 🚨 如果你的 pgvector 字段定义的是 vector(1536)，请联系我改数据库，或者确认他们是否支持 1536 维模型
            model_name = "Qwen/Qwen3-Embedding-4B"

            response = client.embeddings.create(
                model=model_name,
                input=text[:2000]  # 适当截取，防止单次 Token 超限
            )
            raw_embedding = response.data[0].embedding
            if len(raw_embedding) > 1536:
                return raw_embedding[:1536]

            # 从 OpenAI 标准响应结构中提取向量数组
            return raw_embedding

        except Exception as e:
            print(f"❌ 硅基流动 Embedding 失败: {e}")
            return None

    @staticmethod
    def analyze_movie_plot(plot_text):
        """
        [已修改] 调用硅基流动 API 对电影剧情进行分析
        使用性价比与速度极佳的: Qwen/Qwen2.5-7B-Instruct
        或者顶配全能大模型: Qwen/Qwen2.5-72B-Instruct
        """
        if not plot_text or not plot_text.strip():
            return "剧情简介为空，无法分析"

        client = AIAgent._get_client()
        if not client:
            return "未配置 API KEY"

        # 推荐使用硅基流动平台上性价比爆炸的 Qwen2.5 系列
        # 追求速度用: "Qwen/Qwen2.5-7B-Instruct"
        # 追求质量用: "Qwen/Qwen2.5-72B-Instruct"
        model_name = "Qwen/Qwen2.5-7B-Instruct"

        try:
            response = client.chat.completions.create(
                model=model_name,
                messages=[
                    {
                        "role": "user",
                        "content": f"请简要总结以下电影剧情的三个核心标签，用英文逗号隔开，不要包含任何其他废话：\n\n{plot_text}"
                    }
                ],
                temperature=0.3,  # 降低随机性，让标签更精准
                max_tokens=50
            )

            # 解析标准的 Chat 结构返回
            return response.choices[0].message.content.strip()

        except Exception as e:
            return f"Error: 硅基流动大模型请求失败: {e}"