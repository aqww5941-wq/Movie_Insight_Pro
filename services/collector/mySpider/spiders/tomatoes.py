import scrapy
import json
import re
import asyncio
from pathlib import Path
from scrapy_playwright.page import PageMethod
from utils.helpers import DataCleaner

class TomatoesSpider(scrapy.Spider):
    name = "tomatoes"
    base_rt_url = "https://www.rottentomatoes.com" 

    def start_requests(self):
        config_path = Path("configs/spider_targets.json")
        if not config_path.exists(): return

        with open(config_path, 'r', encoding='utf-8') as f:
            targets = json.load(f)

        for target in targets.get(self.name, []):
            yield scrapy.Request(
                url=target['url'],
                meta={
                    "playwright": True,
                    "playwright_include_page": True, # 必须保留 Page 对象
                    "playwright_page_methods": [
                        PageMethod("wait_for_selector", "main"),
                    ],
                },
                callback=self.parse
            )

    async def parse(self, response, **kwargs):
        page = response.meta["playwright_page"]
        
        # 1. 关弹窗 (JS 方式，更快)
        try:
            await page.evaluate("""
                const btn = document.querySelector('button#onetrust-accept-btn-handler');
                if(btn) btn.click();
            """)
            await page.wait_for_timeout(1000)
        except: pass

        seen_urls = set()
        current_loop = 1
        max_loops = 16  # 只抓 16 页
        
        try:
            while current_loop <= max_loops:
                self.logger.info(f"⚡️ 第 {current_loop}/{max_loops} 页处理中...")
                
                # ========================================================
                # 🚀 核心优化：在浏览器内部完成 [提取 + 清理]
                # 这比把 HTML 传回 Python 再解析要快 10 倍！
                # ========================================================
                items_data = await page.evaluate("""
                    () => {
                        const results = [];
                        // 1. 找到所有卡片
                        const cards = document.querySelectorAll('a[data-qa="discovery-media-list-item-caption"]');
                        
                        cards.forEach(card => {
                            const titleNode = card.querySelector('span.p--small');
                            // 2. 提取数据
                            results.push({
                                href: card.getAttribute('href'),
                                title: titleNode ? titleNode.innerText.trim() : "Unknown"
                            });
                            card.setAttribute('data-processed', 'true');
                            card.style.display = 'none';
                        });
                        return results;
                    }
                """)
                
                new_count = 0
                for item in items_data:
                    url = item['href']
                    if not url: continue
                    
                    detail_url = url if url.startswith("http") else self.base_rt_url + url

                    if detail_url not in seen_urls:
                        seen_urls.add(detail_url)
                        new_count += 1
                        
                        # 发送详情页请求 (Priority=20 插队优先下)
                        yield scrapy.Request(
                            url=detail_url,
                            callback=self.parse_detail,
                            priority=20, 
                            meta={
                                "original_title": item['title'],
                                "playwright": True, # 详情页依然用 Playwright
                                "playwright_page_methods": [
                                     # 详情页只需等文字出来
                                    PageMethod("wait_for_selector", 'rt-text[slot="content"]', timeout=10000),
                                ]
                            }
                        )
                
                self.logger.info(f"✅ 第 {current_loop} 页提取 {new_count} 个 (DOM已清理)")

                # 4. 翻页逻辑
                if current_loop < max_loops:
                    # 检查按钮
                    is_btn_visible = await page.evaluate("""
                        () => {
                            const btn = document.querySelector('button[data-qa="dlp-load-more-button"]');
                            return btn && btn.offsetParent !== null;
                        }
                    """)
                    
                    if is_btn_visible:
                        try:
                            # 滚动
                            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                            
                            # 点击 (给30秒)
                            load_more_btn = page.locator('button[data-qa="dlp-load-more-button"]')
                            await load_more_btn.click(force=True, timeout=15000)
                            await load_more_btn.click(force=True)
                            
                            # 等待新数据 (JS 检测，比 Python 轮询更准)
                            await page.wait_for_function("""
                                () => document.querySelectorAll('a[data-qa="discovery-media-list-item-caption"]:not([data-processed])').length > 0
                            """, timeout=20000)
                            
                            current_loop += 1
                            # 稍微休息，防封
                            await asyncio.sleep(2)
                            
                        except Exception as e:
                            self.logger.warning(f"🛑 翻页受阻 (超时或到底): {e}")
                            break
                    else:
                        self.logger.info("🏁 按钮消失，抓取结束。")
                        break
                else:
                    self.logger.info("🎉 达到目标页数，主动停止。")
                    break

        finally:
            await page.close()

    async def parse_detail(self, response):
        # ... (详情页解析代码保持不变) ...
        # 建议加上 try-except 保护
        try:
            plot = response.xpath('//div[contains(@slot,"description")]//rt-text[@slot="content"]/text()').get()
            year_raw = response.xpath('//rt-text[@slot="metadata-prop"][2]//text()').getall()
            year_match = re.search(r'(\d{4})', "".join(year_raw))
            year = year_match.group(1) if year_match else "N/A"

            raw_rating = response.xpath('//div[@class="media-scorecard no-border"]//rt-text[@slot="audience-score"]/text()').get()
            clean_rating = 0.0
            if raw_rating:
                # 使用正则提取数字部分，防止带有特殊字符或空格
                match = re.search(r'(\d+)', raw_rating)
                if match:
                    # 将百分比转为 10 分制： 96 -> 9.6
                    clean_rating = round(float(match.group(1)) / 10, 1)
            # 使用 xpath 的 string() 方法可能更稳健
            director = response.xpath('//p[@data-qa="person-role" and contains(text(), "Director")]/preceding-sibling::p[@data-qa="person-name"]/text()').getall()
            actors = response.xpath('//p[@data-qa="person-role" and contains(text(), "Actor")]/preceding-sibling::p[@data-qa="person-name"]/text()').getall()
            
            raw_cover_url = response.xpath('//media-scorecard//rt-img/@src').get()
            clean_cover = DataCleaner.clean_cover_url(raw_cover_url) if raw_cover_url else None
            
            rating_count = response.xpath('//rt-link[@slot="audience-reviews"]/text()').re_first(r'[\d,]+\+?\s*Ratings') or "No rating_count"

            yield {
                "title": response.meta.get("original_title", "Unknown"),
                "year": year,
                "rating": clean_rating,
                "rating_count": rating_count,
                "plot": plot.strip() if plot else "Unknown plot",
                "Director": [d.strip() for d in director],
                "stars": [name.strip() for name in actors][:5],
                "url": response.url,
                "cover_url": clean_cover
            }
        except Exception as e:
            self.logger.error(f"❌ 详情页解析出错 {response.url}: {e}")