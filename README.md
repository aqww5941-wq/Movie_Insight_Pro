# mySpider · Movie Insight Pro

一个围绕电影数据构建的开源项目：
- 多源爬虫采集（Douban / IMDb / Rotten Tomatoes）
- RabbitMQ 异步解耦
- PostgreSQL + pgvector 存储与向量检索
- FastAPI 提供检索与 AI 对话能力
- Nginx 统一入口（含 SSE 流式代理）

## 功能特性

- **多源采集**：Scrapy + Playwright 抓取电影数据。
- **异步入库**：采集端写入 RabbitMQ，消费者完成清洗、向量生成与 Upsert。
- **向量检索**：基于 `pgvector` 做语义检索与混合召回。
- **AI 助手**：支持普通问答与流式（SSE）电影推荐对话。
- **生产化部署**：Docker Compose 一键拉起全栈服务。

## 技术栈

- **采集层**：Scrapy, Playwright
- **消息队列**：RabbitMQ
- **数据层**：PostgreSQL 16 + pgvector, Redis
- **服务层**：FastAPI, SQLAlchemy, Alembic
- **网关层**：Nginx
- **AI 能力**：DashScope（向量与重排等）

## 项目结构

```text
.
├── docker-compose.yml
├── manage.sh                     # 一键管理脚本
├── db/
│   └── init_sql/init.sql         # 初始化扩展（vector/pg_trgm）
├── services/
│   ├── collector/                # Scrapy 爬虫 + RabbitMQ 生产/消费
│   ├── backend/                  # FastAPI API + AI Agent
│   └── nginx/                    # 反向代理与静态页
└── monitoring/                   # Prometheus 配置
```

## 环境要求

- Docker + Docker Compose（建议最新版）
- Linux/macOS（Windows 建议 WSL2）
- 可用的 DashScope Key（如需 AI 功能）

## 快速开始

## 1.  `.env` 配置如下：
```env
# 数据库配置
PG_PASSWORD=password
PG_DBNAME=movie_db
PG_USER=root

# 邮件配置
EMAIL_SENDER=your_qq_email@qq.com
EMAIL_PASSWORD=your_qq_smtp_auth_code   # 填写 QQ 邮箱生成的 SMTP 授权码
EMAIL_RECEIVER=your_receiver_email@qq.com

# 代理配置
PROXY_PORT=7897(用于 Scrapy/Playwright 科学上网)

#千问api
DASHSCOPE_API_KEY=xxxxxxxxx
# 硅基流动api
SILICONFLOW_API_KEY=xxxxxxxxxx
REDIS_HOST=redis
# AI 深度重排配置
AI_DEEP_RERANK_ENABLED=true
AI_DEEP_RERANK_TOPN=24
AI_DEEP_RERANK_WEIGHT=0.45
AI_DEEP_RERANK_TIMEOUT_SECONDS=12
AI_DEEP_RERANK_MODEL=gte-rerank-v2
```
---
## 2. alembic同步数据表结构、字段
```bash
# 进入alembic.init同一目录，执行 Alembic 升级命令，将数据库同步到最新版本
alembic upgrade head
```
或  进入容器直接操作
```bash
docker exec -it movie-api alembic upgrade head
```
---
## 3. 容器collector运行爬取数据
```bash
# 下载 Chromium 浏览器及依赖
docker exec -it collector bash -c "pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple && playwright install chromium --with-deps"

# 爬取数据
scrapy crawl douban
scrapy crawl imdb
scrapy crawl tomatoes

```
> 爬取的数据会自动进入队列，向量化，入库
## 运行机制（简版）

1. 爬虫抓取电影基础信息。
2. 数据通过 RabbitMQ 进入消费队列。
3. 消费端做字段清洗、生成 embedding、写入 PostgreSQL（Upsert）。
4. FastAPI 提供检索与 AI 推荐接口。
5. Nginx 对外提供统一入口并处理 SSE 长连接。

## 开源说明

欢迎提 Issue / PR 改进：
- 新增数据源
- 优化召回与重排策略
- 完善前端展示与可观测性

如果这个项目对你有帮助，欢迎 Star。
