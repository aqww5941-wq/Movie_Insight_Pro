import os

# =============================================================
# 1. 核心 Reactor 配置 (⚠️ 必须放在文件最开头)
# =============================================================
TWISTED_REACTOR = "twisted.internet.asyncioreactor.AsyncioSelectorReactor"

# =============================================================
# 2. 基础项目信息
# =============================================================
BOT_NAME = "mySpider"
SPIDER_MODULES = ["mySpider.spiders"]
NEWSPIDER_MODULE = "mySpider.spiders"

# =============================================================
# 3. 基础爬虫行为设置
# =============================================================
ROBOTSTXT_OBEY = False
LOG_LEVEL = "INFO"

# 模拟一个真实的浏览器 UA
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

# =============================================================
# 4. 并发与延迟控制
# =============================================================
CONCURRENT_REQUESTS = 16
CONCURRENT_REQUESTS_PER_DOMAIN = 16
DOWNLOAD_DELAY = 0.5

# Playwright 专用并发配置
PLAYWRIGHT_MAX_CONTEXTS = 4
PLAYWRIGHT_MAX_PAGES_PER_CONTEXT = 4
DOWNLOAD_TIMEOUT = 30 

# =============================================================
# 5. Playwright 深度配置
# =============================================================
DOWNLOAD_HANDLERS = {
    "http": "scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler",
    "https": "scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler",
}

PLAYWRIGHT_BROWSER_TYPE = "chromium"

PLAYWRIGHT_LAUNCH_OPTIONS = {
    # ⚠️ 既然是“历史性改变”，生产环境务必设为 True，否则 Docker 容器里没显示器会报错
    "headless": True, 
    "timeout": 30000,
    "args": [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-gpu",
        "--disable-blink-features=AutomationControlled",
        "--window-size=1920,1080",
    ],
}

# 极速模式：拦截图片和无关资源
def should_abort_request(request):
    return request.resource_type in ["image", "font", "media", "beacon", "ad"]
PLAYWRIGHT_ABORT_REQUEST = should_abort_request

# =============================================================
# 6. Item Pipeline (数据管道)
# =============================================================
# 模式：通过 RabbitMQ 写入 PostgreSQL
ITEM_PIPELINES = {
   'mySpider.pipelines.rabbitmq_pipeline.RabbitMQPipeline': 301,
}

# =============================================================
# 7. PostgreSQL 数据库配置 (环境自适应)
# =============================================================
# 已经从 MYSQL 全面进化为 PG
PG_HOST = os.getenv('PG_HOST', '127.0.0.1')
PG_DBNAME = os.getenv('PG_DBNAME', 'movie_db')
PG_USER = os.getenv('PG_USER', 'root')
PG_PASSWORD = os.getenv('PG_PASSWORD', '000000')

# 端口逻辑：
# 本地运行(127.0.0.1) -> 连 Docker 映射出的 5432
# Docker 容器内运行(db) -> 连容器间网络的 5432
PG_PORT = int(os.getenv('PG_PORT', 5432))

# =============================================================
# 8. 其他设置
# =============================================================
FEED_EXPORT_ENCODING = "utf-8"
COOKIES_ENABLED = False
REQUEST_FINGERPRINTER_IMPLEMENTATION = "2.7"

EXTENSIONS = {
   'mySpider.extensions.SpiderMonitorExtension': 500,
}