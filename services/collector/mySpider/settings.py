import os

# =============================================================
# 1. 核心 Reactor 配置 (⚠️ 必须放在文件最开头)
# =============================================================
# Scrapy-Playwright 必须使用 Asyncio Reactor
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
# 不遵守 robots.txt (Rotten Tomatoes 等大站通常会禁止爬虫)
ROBOTSTXT_OBEY = False

# 日志级别 (调试时用 INFO，稳定后可改为 WARNING)
LOG_LEVEL = "INFO"

# User Agent (建议设置一个较新的浏览器 UA)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

# =============================================================
# 4. 并发与延迟控制
# =============================================================
# 全局并发数：Playwright 比较吃内存，建议设为 CPU 核心数或稍低 (6-8 比较稳妥)
CONCURRENT_REQUESTS = 16

# 单域名并发：针对详情页抓取，保持与全局一致即可
CONCURRENT_REQUESTS_PER_DOMAIN = 16

# 下载延迟：防止请求过快被封 IP (单位：秒)
DOWNLOAD_DELAY = 0.5
# Playwright 专用配置
PLAYWRIGHT_MAX_CONTEXTS = 4  # 同时开启的浏览器窗口数
PLAYWRIGHT_MAX_PAGES_PER_CONTEXT = 4 # 每个窗口复用的标签页数

# 超时设置 (关键优化)
# 针对瀑布流列表页，翻页过程可能持续数分钟，必须设大
DOWNLOAD_TIMEOUT = 30  # 30s

# =============================================================
# 5. Playwright 深度配置
# =============================================================
# 指定 http/https 协议使用 Playwright 处理
DOWNLOAD_HANDLERS = {
    "http": "scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler",
    "https": "scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler",
}

# 浏览器类型
PLAYWRIGHT_BROWSER_TYPE = "chromium"

# 启动选项
PLAYWRIGHT_LAUNCH_OPTIONS = {
    # ⚠️ 调试时设为 False (看浏览器动作)，生产环境设为 True (后台运行)
    "headless": False, 
    "timeout": 30000,  # 浏览器启动超时
    "args": [
        "--no-sandbox",
        "--disable-gpu",
        "--disable-blink-features=AutomationControlled", # 核心反爬：隐藏自动化特征
        "--window-size=1920,1080", # 设大窗口，防止按钮被遮挡无法点击
        "--disable-images", # 禁用图片渲染
        "--blink-settings=imagesEnabled=false", # 双重保险禁用图片
    ],
}


# 资源加载过滤 (极速模式)
# 拦截不必要的资源以提升速度（尤其是详情页）
def should_abort_request(request):
    return request.resource_type in [
        "image", "font", "media", "beacon", "ad", "imageset"
    ]
PLAYWRIGHT_ABORT_REQUEST = should_abort_request

# =============================================================
# 6. Item Pipeline (数据管道)
# =============================================================
ITEM_PIPELINES = {
   # 'mySpider.pipelines.movie_pipeline.MoviePipeline': 300, # 如果有文件下载管道
   'mySpider.pipelines.mysql_pipeline.MysqlPipeline': 301,
}

# =============================================================
# 7. MySQL 数据库配置 (环境自适应)
# =============================================================
MYSQL_HOST = os.getenv('MYSQL_HOST', '127.0.0.1')
MYSQL_DBNAME = os.getenv('MYSQL_DBNAME', 'movie_db')
MYSQL_USER = os.getenv('MYSQL_USER', 'root')
MYSQL_PASSWORD = os.getenv('MYSQL_PASSWORD', '000000')

# 端口逻辑：
# 本地运行(127.0.0.1) -> 连 Docker 映射出的 3307
# Docker 容器内运行 -> 连容器间网络的 3306
if MYSQL_HOST == '127.0.0.1':
    MYSQL_PORT = 3307
else:
    MYSQL_PORT = 3306

# =============================================================
# 8. 其他设置
# =============================================================
# 导出编码
FEED_EXPORT_ENCODING = "utf-8"

# 禁用 Cookies (可选，设为 False 可减少被追踪风险，但在某些登录场景需开启)
COOKIES_ENABLED = False

# 请求指纹生成器 (推荐使用 2.7)
REQUEST_FINGERPRINTER_IMPLEMENTATION = "2.7"

# 扩展插件 (如果需要监控)
EXTENSIONS = {
   'mySpider.extensions.SpiderMonitorExtension': 500,
}