#!/usr/bin/env python3
"""
RabbitMQ 消息消费者 (独立同步版)
职责：消费 MQ → 校验/清洗 → MySQL
"""
import json
import logging
import signal
import sys
from concurrent.futures import ThreadPoolExecutor

import pika
import pymysql
from dbutils.pooled_db import PooledDB # 需要 pip install DBUtils
from tenacity import retry, stop_after_attempt, wait_exponential

# 只需要保留配置文件的引用
from configs.rabbitmq_config import (
    RABBITMQ_PARAMS,
    MOVIE_QUEUE_NAME,
    EXCHANGE_NAME,
    ROUTING_KEY,
    EXCHANGE_TYPE,
)

# ---------------- logging ----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# ==========================================
# 1. 定义一个独立的同步 MySQL 连接池类
# ==========================================
class SyncMysqlPool:
    def __init__(self, settings):
        self.pool = PooledDB(
            creator=pymysql,
            maxconnections=15,  # 稍微大于线程池数量(10)
            mincached=2,
            maxcached=5,
            blocking=True,      # 连接池满了等待
            host=settings.get('MYSQL_HOST'),
            port=int(settings.get('MYSQL_PORT', 3306)),
            user=settings.get('MYSQL_USER'),
            password=settings.get('MYSQL_PASSWORD'),
            database=settings.get('MYSQL_DBNAME'),
            charset='utf8mb4',
        )

    def execute_write(self, sql, params):
        """执行写操作（自动提交/回滚）"""
        conn = self.pool.connection()
        cursor = conn.cursor()
        try:
            cursor.execute(sql, params)
            conn.commit()
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            cursor.close()
            conn.close()

# ==========================================
# 2. 消费者逻辑
# ==========================================
class RabbitMQConsumer:
    def __init__(self, mysql_settings):
        self.mysql_settings = mysql_settings
        self.connection = None
        self.channel = None
        self.db = None # 这里将存放 SyncMysqlPool 实例
        self.executor = ThreadPoolExecutor(max_workers=10)
        self._running = False

    # ---------- lifecycle ----------
    def setup_signal_handlers(self):
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _signal_handler(self, signum, frame):
        logger.info("🛑 收到退出信号，准备关闭 Consumer")
        self.stop()

    def init_mysql(self):
        # ⚠️ 关键修改：直接使用上面定义的同步池，不再引用 Scrapy Pipeline
        try:
            self.db = SyncMysqlPool(self.mysql_settings)
            logger.info("✅ MySQL 同步连接池就绪")
        except Exception as e:
            logger.error(f"❌ MySQL 连接失败: {e}")
            sys.exit(1)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(1, 3, 10))
    def connect_rabbitmq(self):
        # 拷贝一份配置，避免修改全局变量
        params = RABBITMQ_PARAMS.copy()
        # 提取并移除 pika 不认识的 username/password 字段
        user = params.pop('username','guest')
        pwd = params.pop('password','guest')
        # 创建 pika 需要的凭证对象
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
        self.init_mysql() # 先连数据库
        self.connect_rabbitmq() # 再连 MQ
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
            # 提交给线程池处理
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
            self._save_to_mysql(message)
            self._threadsafe_ack(delivery_tag)
            logger.info(f"✅ 入库成功: {message.get('title')}")
        except Exception as e:
            logger.error(f"❌ 入库失败: {e}")
            # 这里可以选择是否 requeue，如果是因为数据脏了，requeue=False 避免死循环
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

    # ---------- db ----------
    def _save_to_mysql(self, msg):
        # 数据清洗逻辑（直接保留在这里，无需调用 helper 防止循环引用）
        rating = self._to_float(msg.get("rating"))
        count = self._clean_count(msg.get("rating_count"))
        director = self._join(msg.get("director"))
        stars = self._join(msg.get("stars"))
        summary = msg.get("plot") or msg.get("summary", "")

        sql = """
        INSERT INTO movies
        (title, year, rating, rating_count, source, url, director, stars, summary, cover_url)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE
            rating=VALUES(rating),
            rating_count=VALUES(rating_count),
            director=VALUES(director),
            stars=VALUES(stars),
            summary=VALUES(summary),
            cover_url=VALUES(cover_url)
        """

        params = (
            msg.get("title"),
            msg.get("year"),
            rating,
            count,
            msg.get("source", msg.get("spider")),
            msg.get("url"),
            director,
            stars,
            summary,
            msg.get("cover_url"),
        )

        # 调用我们自己的同步池
        self.db.execute_write(sql, params)

    # ---------- utils (内部工具函数，解耦依赖) ----------
    def _join(self, v):
        if isinstance(v, list):
            return ",".join(v)
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
    # 这里我们直接读取环境变量，不依赖 scrapy get_project_settings
    import os
    from dotenv import load_dotenv
    load_dotenv()

    # 构造简单的配置字典，解耦 Scrapy
    settings = {
        'MYSQL_HOST': os.getenv('MYSQL_HOST', 'db'),
        'MYSQL_PORT': int(os.getenv('MYSQL_PORT', 3306)),
        'MYSQL_USER': os.getenv('MYSQL_USER', 'root'),
        'MYSQL_PASSWORD': os.getenv('MYSQL_PASSWORD', '000000'),
        'MYSQL_DBNAME': os.getenv('MYSQL_DBNAME', 'movie_db')
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