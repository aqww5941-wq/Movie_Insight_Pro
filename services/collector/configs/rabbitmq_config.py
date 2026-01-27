"""
RabbitMQ 配置文件
支持环境变量配置
"""
import os

# RabbitMQ 连接配置
RABBITMQ_HOST = os.getenv('RABBITMQ_HOST', '127.0.0.1')
RABBITMQ_PORT = os.getenv('RABBITMQ_PORT', '5672')
RABBITMQ_USERNAME = os.getenv('RABBITMQ_USERNAME', 'guest')
RABBITMQ_PASSWORD = os.getenv('RABBITMQ_PASSWORD', 'guest')
RABBITMQ_VIRTUAL_HOST = os.getenv('RABBITMQ_VIRTUAL_HOST', '/')

# RabbitMQ Queue 配置
MOVIE_QUEUE_NAME = os.getenv('MOVIE_QUEUE_NAME', 'movie_queue')
EXCHANGE_NAME = os.getenv('RABBITMQ_EXCHANGE_NAME', 'movie_exchange')
ROUTING_KEY = os.getenv('RABBITMQ_ROUTING_KEY', 'movie.data')

# 连接参数配置
RABBITMQ_PARAMS = {
    'host': RABBITMQ_HOST,
    'port': int(RABBITMQ_PORT),
    'username': RABBITMQ_USERNAME,
    'password': RABBITMQ_PASSWORD,
    'virtual_host': RABBITMQ_VIRTUAL_HOST,
    'heartbeat': 60,
    'connection_attempts': 3,
    'retry_delay': 5,
}

# Queue 参数配置
QUEUE_ARGS = {
    'x-message-ttl': 300000,  # 消息过期时间 5 分钟
    'x-max-priority': 10,     # 最大优先级
}

# Exchange 参数配置
EXCHANGE_TYPE = 'direct'  # exchange 类型