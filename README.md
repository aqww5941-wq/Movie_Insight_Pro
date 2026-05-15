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

### 1) 配置环境变量

项目依赖根目录 `.env`。至少需要下面这些变量：

```bash
PG_USER=root
PG_PASSWORD=000000
PG_DBNAME=movie_db
RABBITMQ_USERNAME=guest
RABBITMQ_PASSWORD=guest
DASHSCOPE_API_KEY=your_api_key
```

> `manage.sh` 会检查 `.env` 是否存在。

### 2) 启动服务

```bash
chmod +x manage.sh
./manage.sh start
```

启动后默认端口：
- `http://localhost`：Nginx 入口
- `http://localhost/api/docs`：Swagger 文档
- `http://localhost:15672`：RabbitMQ 管理台

### 3) 查看状态与日志

```bash
./manage.sh status
./manage.sh logs backend
./manage.sh logs consumer
```

### 4) 健康检查

```bash
./manage.sh health
```

## 常用管理命令

```bash
./manage.sh start                  # 启动
./manage.sh stop                   # 停止
./manage.sh restart                # 重启
./manage.sh status                 # 服务状态
./manage.sh logs [service]         # 查看日志
./manage.sh enter backend          # 进入容器
./manage.sh backup                 # 备份 PostgreSQL
./manage.sh restore backups/xxx.gz # 恢复 PostgreSQL
```

## 数据采集

采集服务在 `collector` 容器中运行。你可以进入容器手动触发爬虫任务，例如：

```bash
./manage.sh enter collector
scrapy list
scrapy crawl douban_movie
scrapy crawl imdb
scrapy crawl tomatoes
```

采集数据会先进入 RabbitMQ，再由 `consumer` 服务清洗与入库。

## 核心 API

后端主要接口（由 Nginx 统一挂在 `/api` 下）：

- `GET /api/health`：系统健康状态
- `GET /api/movies`：电影检索/分页查询
- `GET /api/movies/{movie_id}`：电影详情
- `POST /api/movies/rag-search`：RAG 检索
- `POST /api/agent/chat`：AI 对话（非流式）
- `POST /api/agent/chat/stream`：AI 对话（SSE 流式）
- `GET /api/stats/platforms`：数据来源统计
- `GET /api/surprise-me`：随机推荐

在线调试：`http://localhost/api/docs`

## 运行机制（简版）

1. 爬虫抓取电影基础信息。
2. 数据通过 RabbitMQ 进入消费队列。
3. 消费端做字段清洗、生成 embedding、写入 PostgreSQL（Upsert）。
4. FastAPI 提供检索与 AI 推荐接口。
5. Nginx 对外提供统一入口并处理 SSE 长连接。

## 故障排查

- `api/docs` 打不开：先执行 `./manage.sh status`，确认 `backend` 与 `nginx` 已启动。
- AI 无响应或超时：检查 `DASHSCOPE_API_KEY` 是否正确，以及配额/网络状态。
- 无数据可查：确认爬虫已执行，且 `consumer` 日志无持续报错。
- PostgreSQL 异常：检查 `db/init_sql/init.sql` 是否成功启用了 `vector` 扩展。

## 开源说明

欢迎提 Issue / PR 改进：
- 新增数据源
- 优化召回与重排策略
- 完善前端展示与可观测性

如果这个项目对你有帮助，欢迎 Star。
