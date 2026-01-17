# Scrapy settings for mySpider project
#
# For simplicity, this file contains only settings considered important or
# commonly used. You can find more settings consulting the documentation:
#
#     https://docs.scrapy.org/en/latest/topics/settings.html
#     https://docs.scrapy.org/en/latest/topics/downloader-middleware.html
#     https://docs.scrapy.org/en/latest/topics/spider-middleware.html
import os

BOT_NAME = "mySpider"

SPIDER_MODULES = ["mySpider.spiders"]
NEWSPIDER_MODULE = "mySpider.spiders"

ADDONS = {}

EXTENSIONS = {
    'mySpider.extensions.SpiderMonitorExtension': 500,
}

# Crawl responsibly by identifying yourself (and your website) on the user-agent
#USER_AGENT = "mySpider (+http://www.yourdomain.com)"

# Obey robots.txt rules
ROBOTSTXT_OBEY = False

# Concurrency and throttling settings
#CONCURRENT_REQUESTS = 16
CONCURRENT_REQUESTS_PER_DOMAIN = 1
DOWNLOAD_DELAY = 1

# Disable cookies (enabled by default)
#COOKIES_ENABLED = False

# Disable Telnet Console (enabled by default)
#TELNETCONSOLE_ENABLED = False

# Override the default request headers:
#DEFAULT_REQUEST_HEADERS = {
#    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
#    "Accept-Language": "en",
#}

# Enable or disable spider middlewares
# See https://docs.scrapy.org/en/latest/topics/spider-middleware.html
#SPIDER_MIDDLEWARES = {
#    "mySpider.middlewares.MyspiderSpiderMiddleware": 543,
#}

# Enable or disable downloader middlewares
# See https://docs.scrapy.org/en/latest/topics/downloader-middleware.html
#DOWNLOADER_MIDDLEWARES = {
#    "mySpider.middlewares.MyspiderDownloaderMiddleware": 543,
#}

# Enable or disable extensions
# See https://docs.scrapy.org/en/latest/topics/extensions.html
#EXTENSIONS = {
#    "scrapy.extensions.telnet.TelnetConsole": None,
#}

# Configure item pipelines
# See https://docs.scrapy.org/en/latest/topics/item-pipeline.html
ITEM_PIPELINES = {
#    "mySpider.pipelines.Movie_pipeline.MoviePipeline": 300,
   'mySpider.pipelines.mysql_pipeline.MysqlPipeline': 301,
}

MYSQL_HOST = os.getenv('MYSQL_HOST', '127.0.0.1')

# 2. 关键：本地调试用 3307，Docker 内部用 3306
if MYSQL_HOST == '127.0.0.1':
    MYSQL_PORT = 3307
else:
    MYSQL_PORT = 3306

MYSQL_DBNAME = os.getenv('MYSQL_DBNAME', 'movie_db')
MYSQL_USER = os.getenv('MYSQL_USER', 'root')

# 3. 确保密码与 docker-compose 中的一致
MYSQL_PASSWORD = os.getenv('MYSQL_PASSWORD', '000000')

# Enable and configure the AutoThrottle extension (disabled by default)
# See https://docs.scrapy.org/en/latest/topics/autothrottle.html
#AUTOTHROTTLE_ENABLED = True
# The initial download delay
#AUTOTHROTTLE_START_DELAY = 5
# The maximum download delay to be set in case of high latencies
#AUTOTHROTTLE_MAX_DELAY = 60
# The average number of requests Scrapy should be sending in parallel to
# each remote server
#AUTOTHROTTLE_TARGET_CONCURRENCY = 1.0
# Enable showing throttling stats for every response received:
#AUTOTHROTTLE_DEBUG = False

# Enable and configure HTTP caching (disabled by default)
# See https://docs.scrapy.org/en/latest/topics/downloader-middleware.html#httpcache-middleware-settings
#HTTPCACHE_ENABLED = True
#HTTPCACHE_EXPIRATION_SECS = 0
#HTTPCACHE_DIR = "httpcache"
#HTTPCACHE_IGNORE_HTTP_CODES = []
#HTTPCACHE_STORAGE = "scrapy.extensions.httpcache.FilesystemCacheStorage"

# Set settings whose default value is deprecated to a future-proof value
FEED_EXPORT_ENCODING = "utf-8"

# 添加的Playwright配置

# 1. 必须配置的异步 Reactor (必须放在最上面)
TWISTED_REACTOR = 'twisted.internet.asyncioreactor.AsyncioSelectorReactor'

# 2. 指定下载处理程序 (注意 https 和 http 都要指向 scrapy_playwright)
# 修改 settings.py 中的 DOWNLOAD_HANDLERS
DOWNLOAD_HANDLERS = {
    # 尝试改用 http11 路径（这是 Scrapy 2.14 内部期望的路径）
    "http": "scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler",
    "https": "scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler",
}


# 3. 浏览器配置
PLAYWRIGHT_LAUNCH_OPTIONS = {
    "headless": True,  # 调试时改成 False 可以看到浏览器弹出
    "timeout": 20 * 1000,  # 20秒超时
    "args": [
        "--disable-blink-features=AutomationControlled", # 配合 stealth 的双重保险
        "--no-sandbox",
    ]
}
# 4. 请求头 (伪装成 Win10 + Chrome)
DEFAULT_REQUEST_HEADERS = {
   "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
   "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
}
ROBOTSTXT_OBEY = False
DOWNLOAD_DELAY = 2  # 哪怕慢一点，也要稳
# # 3. 浏览器设置
# PLAYWRIGHT_BROWSER_TYPE = "chromium"
# PLAYWRIGHT_LAUNCH_OPTIONS = {
#     "headless": True, # 建议调试时先设为 False，看看浏览器到底怎么操作的
#     "timeout": 30000, 
# }

# REQUEST_FINGERPRINTER_IMPLEMENTATION = "2.7"

# 4. (建议添加) 伪装 User-Agent，否则 Playwright 的默认头很容易被识别
# USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
# PLAYWRIGHT_LAUNCH_OPTIONS = {"headless": False}