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
    def analyze_movie_plot(plot_text):
        """
        调用 Qwen API 对电影剧情进行分析
        """
        import dashscope
        api_key = os.getenv('DASHSCOPE_API_KEY')
        if not api_key:
            return "未配置 API KEY"

        # 调用通义千问模型 (以 qwen-plus 为例)
        response = dashscope.Generation.call(
            model='qwen-plus',
            prompt=f"请简要总结以下电影剧情的三个核心标签，用逗号隔开：{plot_text}",
            api_key=api_key
        )

        if response.status_code == HTTPStatus.OK:
            return response.output.text
        else:
            return f"Error: {response.message}"