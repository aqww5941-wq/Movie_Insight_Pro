import scrapy
from utils.helpers import DataCleaner  # 导入刚才写的工具类
from scrapy_playwright.page import PageMethod  # 建议导入这个类，写法更清晰
from pathlib import Path
import json

class ImdbSpider(scrapy.Spider):
    name = "imdb"

    def start_requests(self):
        # 1. 读取配置文件
        config_path = Path("configs/spider_targets.json")
        if not config_path.exists():
            self.logger.error("配置文件未找到！")
            return

        with open(config_path, 'r', encoding='utf-8') as f:
            targets = json.load(f)

        # 2. 获取属于当前爬虫的任务
        my_targets = targets.get(self.name, [])

        for target in my_targets:
            playwright_meta = {
                "playwright": True,
                "playwright_include_page": True, # 如果需要执行自定义 JS，设为 True
                "playwright_page_methods": [
                    ("wait_for_selector", "main section"),    # 1. 使用元组形式，等待关键内容区域加载
                    ("evaluate", "window.scrollBy(0, 500)"),   # 2. 模拟稍微滚动一下，触发懒加载
                ]
            }
            # 获取 JSON 里的业务配置
            json_meta = target.get('mata',{})

            final_meta = {**playwright_meta, **json_meta}

            yield scrapy.Request(
                url=target['url'],
                meta=final_meta,  
                callback=self.parse
            )

    async def parse(self, response,**kwarge):
      
        nodes = response.xpath('//li[contains(@class, "ipc-metadata-list-summary-item")]')

        for node in nodes:

            title = node.xpath('.//h3/text()').get()

            Director = node.xpath('.//span[contains(text(), "Director")]/following-sibling::span//a/text()').getall()
            # cleaned_Director = [a.strip() for a in Director if a and a.strip()]

            actors = node.xpath('.//span[contains(text(), "Stars")]/following-sibling::span//a/text()').getall()
            
            rating = node.xpath('.//span[contains(@class, "ipc-rating-star--rating")]/text()').get()

            year = node.xpath('.//span[contains(@class, "li-title-metadata-item")][1]/text()').get()

            plot = node.xpath('.//div[contains(@class, "content-inner-div")]/text()').get()
            
            rating_count = node.xpath('.//span[contains(@class, "ipc-rating-star--voteCount")]/text()').re_first(r'[\w\.]+')

            # 抓取原始图片链接
            raw_cover_url = node.xpath('.//img[contains(@class, "ipc-image")]/@src').get()
            # 【调用工具类清洗】
            cover_url = DataCleaner.clean_cover_url(raw_cover_url)
            
            relative_url = node.xpath('.//a[contains(@class, "ipc-title-link-wrapper")]/@href').get()
            full_url = response.urljoin(relative_url)
            clean_url = full_url.split('?')[0]

            yield {
                "title": title.strip() if title else "Unknow title",
                "year": year.strip().rstrip('–') if year else "N/A",
                "rating": rating.strip() if rating else "No rating",
                "rating_count": rating_count.strip() if rating_count else "No rating_count",
                "plot": plot.strip() if plot else "Unknow plot",
                "Director": Director if Director else "Unknow Director",
                "stars": [name.strip() for name in actors if name.strip()],
                "url": clean_url,
                "cover_url": cover_url
            }