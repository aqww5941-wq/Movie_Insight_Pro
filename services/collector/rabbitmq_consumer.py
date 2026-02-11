#!/usr/bin/env python3
"""
RabbitMQ 消息消费者 (PostgreSQL + SQLAlchemy 同步版)
职责：消费 MQ → 校验/清洗 → PostgreSQL (Upsert)
"""
import json
import logging
import signal
import sys
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pika
from tenacity import retry, stop_after_attempt, wait_exponential

# SQLAlchemy 导入
from sqlalchemy import create_engine, Column, Integer, String, Float, Text, DateTime, func, text
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from sqlalchemy.dialects.postgresql import insert as pg_insert

# 配置引用
from configs.rabbitmq_config import (
    RABBITMQ_PARAMS,
    MOVIE_QUEUE_NAME,
    EXCHANGE_NAME,
    ROUTING_KEY,
    EXCHANGE_TYPE,
)

from models import Movie, Base
from utils.helpers import AIAgent, DataCleaner
# ---------------- logging ----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


# ==========================================
# 2. 数据库管理类 (SQLAlchemy Sync)
# ==========================================
class PgDatabaseManager:
    def __init__(self, settings):
        # 构造 PostgreSQL 连接字符串
        # 格式: postgresql+psycopg2://user:password@host:port/dbname
        db_url = f"postgresql+psycopg2://{settings['PG_USER']}:{settings['PG_PASSWORD']}@{settings['PG_HOST']}:{settings['PG_PORT']}/{settings['PG_DBNAME']}"
        
        self.engine = create_engine(
            db_url,
            pool_size=15,       # 稍微大于线程池数量(10)
            max_overflow=5,
            pool_pre_ping=True, # 自动检测断连并重连
            echo=False          # 设为 True 可打印 SQL 用于调试
        )
        self.SessionLocal = sessionmaker(bind=self.engine)
        
        # 自动建表 (如果表不存在)
        try:
            with self.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            logger.info("✅ 数据库连接校验完成，准备接收数据...")
        except Exception as e:
            logger.error(f"❌ 数据库连接失败，消费者无法启动: {e}")
           
    def upsert_movie(self, movie_data):
        """
        执行 Postgres 特有的 Upsert (Insert on Conflict Update)
        """
        session = self.SessionLocal()
        try:
            # 1. 构建插入语句
            stmt = pg_insert(Movie).values(**movie_data)
            
            # 2. 定义冲突时的更新策略
            # 当 url 冲突时，更新除 id, url, created_at 之外的字段
            update_dict = {
                "title": stmt.excluded.title, # 虽然标题一般不变，但也可能修正
                "year": stmt.excluded.year,
                "rating": stmt.excluded.rating,
                "rating_count": stmt.excluded.rating_count,
                "director": stmt.excluded.director,
                "stars": stmt.excluded.stars,
                "summary": stmt.excluded.summary,
                "cover_url": stmt.excluded.cover_url,
                "embedding": stmt.excluded.embedding, # <-- 新增：冲突时更新向量
                "updated_at": func.now() # 手动更新时间戳
            }
            
            # 3. 组装语句
            upsert_stmt = stmt.on_conflict_do_update(
                index_elements=['url'], # 冲突检测字段
                set_=update_dict
            )
            
            # 4. 执行
            session.execute(upsert_stmt)
            session.commit()
            
        except Exception as e:
            session.rollback()
            raise e
        finally:
            session.close()

# ==========================================
# 2. 消费者逻辑
# ==========================================
class RabbitMQConsumer:
    def __init__(self, db_settings):
        self.db_settings = db_settings
        self.connection = None
        self.channel = None
        self.db_manager = None 
        self.executor = ThreadPoolExecutor(max_workers=10)
        self._running = False

    # ---------- lifecycle ----------
    def setup_signal_handlers(self):
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _signal_handler(self, signum, frame):
        logger.info("🛑 收到退出信号，准备关闭 Consumer")
        self.stop()

    def init_db(self):
        try:
            self.db_manager = PgDatabaseManager(self.db_settings)
            logger.info("✅ PostgreSQL 连接池就绪")
        except Exception as e:
            logger.error(f"❌ 数据库初始化失败: {e}")
            sys.exit(1)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(1, 3, 10))
    def connect_rabbitmq(self):
        params = RABBITMQ_PARAMS.copy()
        user = params.pop('username','guest')
        pwd = params.pop('password','guest')
        credentials = pika.PlainCredentials(user,pwd)
        params['credentials'] = credentials

        self.connection = pika.BlockingConnection(
            pika.ConnectionParameters(**params)
        )
        self.channel = self.connection.channel()

        self.channel.exchange_declare(
            exchange=EXCHANGE_NAME,
            exchange_type=EXCHANGE_TYPE,
            durable=True,
        )
        self.channel.queue_declare(
            queue=MOVIE_QUEUE_NAME,
            durable=True,
        )
        self.channel.queue_bind(
            queue=MOVIE_QUEUE_NAME,
            exchange=EXCHANGE_NAME,
            routing_key=ROUTING_KEY,
        )
        self.channel.basic_qos(prefetch_count=10)

        logger.info("✅ RabbitMQ 连接成功")

    def start(self):
        logger.info("🚀 RabbitMQ Consumer 启动中...")
        self.init_db() # 初始化 PG
        self.connect_rabbitmq()
        self._running = True

        self.channel.basic_consume(
            queue=MOVIE_QUEUE_NAME,
            on_message_callback=self._on_message,
            auto_ack=False,
        )

        logger.info("🎧 等待消息中...")
        self.channel.start_consuming()

    def stop(self):
        self._running = False
        if self.channel and self.channel.is_open:
            self.channel.stop_consuming()
            self.channel.close()
        if self.connection and self.connection.is_open:
            self.connection.close()
        self.executor.shutdown(wait=True)
        logger.info("👋 Consumer 已退出")

    # ---------- message ----------
    def _on_message(self, ch, method, props, body):
        try:
            message = json.loads(body.decode("utf-8"))
            self.executor.submit(
                self._handle_message,
                message,
                method.delivery_tag,
            )
        except Exception as e:
            logger.error(f"❌ 解析消息失败: {e}")
            self._threadsafe_reject(method.delivery_tag)

    def _handle_message(self, message, delivery_tag):
        try:
            self._save_to_pg(message)
            self._threadsafe_ack(delivery_tag)
            logger.info(f"✅ 入库成功: {message.get('title')}")
        except Exception as e:
            logger.error(f"❌ 入库失败: {e}")
            # 如果是数据格式严重错误，建议不要 requeue，否则会死循环
            # 这里保守起见设为 True，生产环境可根据 Error 类型判断
            self._threadsafe_reject(delivery_tag)

    # ---------- ack safely ----------
    def _threadsafe_ack(self, tag):
        self.connection.add_callback_threadsafe(
            lambda: self.channel.basic_ack(tag)
        )

    def _threadsafe_reject(self, tag):
        self.connection.add_callback_threadsafe(
            lambda: self.channel.basic_reject(tag, requeue=True)
        )

    # ---------- db operations ----------
    def _save_to_pg(self, msg):
        # 1. 数据清洗 (保持原有逻辑)
        summary_text = msg.get("plot") or msg.get("summary", "")
        # 注意：Postgres 对类型要求比 MySQL 严格，务必确保类型正确
        clean_data = {
            "title": msg.get("title"),
            "year": str(msg.get("year", "")), # 转字符串
            "rating": self._to_float(msg.get("rating")),
            "rating_count": self._clean_count(msg.get("rating_count")),
            "source": msg.get("source", msg.get("spider")),
            "url": msg.get("url"),
            "director": self._join(msg.get("director")),
            "stars": self._join(msg.get("stars")),
            "summary": summary_text,
            "cover_url": msg.get("cover_url"),
        }
        # --- 新增：生成向量数据 ---
        if summary_text:
            logger.info(f"🧠 正在为《{clean_data['title']}》生成语义向量...")
            try:
                # 调用你 helpers.py 里的方法
                vector = AIAgent.generate_embedding(summary_text)
                if vector:
                    clean_data["embedding"] = vector
                    logger.info(f"✨ 向量生成成功")
            except Exception as e:
                logger.error(f"❌ 向量生成异常: {e}")
        # 2. 调用 Manager 执行 Upsert
        if not clean_data["url"]:
            logger.warning("⚠️ 跳过无 URL 的数据")
            return

        self.db_manager.upsert_movie(clean_data)

    # ---------- utils (内部工具函数) ----------
    def _join(self, v):
        if isinstance(v, list):
            return ",".join(str(x) for x in v) # 确保子元素也是 str
        return v or ""

    def _to_float(self, v):
        try:
            return float(v)
        except Exception:
            return 0.0

    def _clean_count(self, v):
        if not v:
            return 0
        s = str(v).lower().replace(",", "")
        if "k" in s:
            return int(float(s.replace("k", "")) * 1000)
        if "m" in s:
            return int(float(s.replace("m", "")) * 1_000_000)
        try:
            return int(float(s))
        except Exception:
            return 0

def main():
    from dotenv import load_dotenv
    load_dotenv()

    # 读取新的 PG 环境变量
    settings = {
        'PG_HOST': os.getenv('PG_HOST', 'db'),
        'PG_PORT': int(os.getenv('PG_PORT', 5432)),
        'PG_USER': os.getenv('PG_USER', 'root'),
        'PG_PASSWORD': os.getenv('PG_PASSWORD', '000000'),
        'PG_DBNAME': os.getenv('PG_DBNAME', 'movie_db')
    }

    consumer = RabbitMQConsumer(settings)
    consumer.setup_signal_handlers()

    try:
        consumer.start()
    except KeyboardInterrupt:
        consumer.stop()
        sys.exit(0)

if __name__ == "__main__":
    main()