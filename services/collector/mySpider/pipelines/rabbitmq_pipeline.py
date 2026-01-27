import json
import logging
import pika
from itemadapter import ItemAdapter
from scrapy.exceptions import DropItem
from tenacity import retry, stop_after_attempt, wait_exponential

from configs.rabbitmq_config import (
    RABBITMQ_PARAMS,
    MOVIE_QUEUE_NAME,
    EXCHANGE_NAME,
    ROUTING_KEY,
    EXCHANGE_TYPE,
)

logger = logging.getLogger(__name__)


class RabbitMQPipeline:
    def __init__(self, settings):
        self.settings = settings
        self.connection = None
        self.channel = None

    @classmethod
    def from_settings(cls, settings):
        return cls(settings)

    def open_spider(self, spider):
        params = RABBITMQ_PARAMS.copy()
        user = params.pop('username', 'guest')
        pwd = params.pop('password', 'guest')

        params['credentials'] = pika.PlainCredentials(user, pwd)

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

        logger.info("✅ RabbitMQ Producer 就绪")

    def close_spider(self, spider):
        if self.connection and self.connection.is_open:
            self.connection.close()

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(1, 3, 10))
    def _publish(self, data):
        self.channel.basic_publish(
            exchange=EXCHANGE_NAME,
            routing_key=ROUTING_KEY,
            body=json.dumps(data, ensure_ascii=False),
            properties=pika.BasicProperties(
                delivery_mode=2,
                content_type="application/json",
            ),
        )

    def process_item(self, item, spider):
        try:
            data = ItemAdapter(item).asdict()
            data["spider"] = spider.name
            self._publish(data)
            logger.info(f"📤 已发送: {data.get('title')}")
            return item

        except Exception as e:
            raise DropItem(f"MQ 发送失败: {e}")
