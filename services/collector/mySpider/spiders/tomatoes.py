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
                    "playwright_include_page": True,  # 必须保留 Page 对象
                    "playwright_page_goto_kwargs": {
                        "wait_until": "domcontentloaded",
                        "timeout": 60000
                    },
                    "download_timeout": 60,
                    "playwright_page_methods": [
                        PageMethod("wait_for_selector", "main", timeout=15000),
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
        except:
            pass

        seen_urls = set()
        current_loop = 1
        max_loops = 16  # 只抓 16 页

        try:
            while current_loop <= max_loops:
                self.logger.info(f"⚡️ 第 {current_loop}/{max_loops} 页处理中...")

                # ========================================================
                # 🚀 核心优化：在浏览器内部完成 [提取 + 清理]
                # 已应用【方案二】：自动剥除 Shadow DOM 内部自带的样式表文本
                # ========================================================
                # ========================================================
                # 🚀 核心优化：在浏览器内部完成 [提取 + 清理]
                # 修复版本：解决 ShadowRoot nodes are not clonable 报错
                # ========================================================
                items_data = await page.evaluate("""
                    () => {
                        const results = [];
                        // 1. 找到所有卡片
                        const cards = document.querySelectorAll('a[data-qa="discovery-media-list-item-caption"]');
    
                        cards.forEach(card => {
                            // 2. 定位到包含标题的 rt-text 标签
                            const titleEl = card.querySelector('rt-text[data-qa="discovery-media-list-item-title"]');
                            let title = "Unknown";
    
                            if (titleEl) {
                                if (titleEl.shadowRoot) {
                                    // 【优雅修复】：由于 ShadowRoot 无法直接克隆，我们新建一个普通 div 来承载内容
                                    const tempDiv = document.createElement('div');
                                    // 复制 shadowRoot 内部的所有 HTML 结构
                                    tempDiv.innerHTML = titleEl.shadowRoot.innerHTML;
    
                                    // 移除里面的所有 style 标签
                                    const styles = tempDiv.querySelectorAll('style');
                                    styles.forEach(s => s.remove());
    
                                    // 拿到的就是最干净的纯文本了
                                    title = tempDiv.textContent.trim();
                                } else {
                                    title = titleEl.innerText.trim();
                                }
                            }
    
                            // 3. 极速兜底：如果拿出来的标题仍然为空，或还是不小心夹带了 :host
                            if (!title || title === "Unknown" || title.includes(':host')) {
                                // 如果实在有顽固的 CSS 残留，用 JS 正则暴力抹杀样式块
                                const rawText = titleEl ? titleEl.textContent : "";
                                title = rawText.replace(/:host[\s\S]*?\}/g, '').trim() || "Unknown";
                            }
    
                            // 4. 提取干净的数据
                            results.push({
                                href: card.getAttribute('href'),
                                title: title
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
                                "playwright": True,  # 详情页依然用 Playwright
                                "playwright_page_goto_kwargs": {
                                    "wait_until": "domcontentloaded",
                                    "timeout": 60000
                                },
                                "download_timeout": 60,
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
        try:
            plot = response.xpath('//div[contains(@slot,"description")]//rt-text[@slot="content"]/text()').get()
            year_raw = response.xpath('//rt-text[@slot="metadata-prop"][2]//text()').getall()
            year_match = re.search(r'(\d{4})', "".join(year_raw))
            year = year_match.group(1) if year_match else "N/A"

            raw_rating = response.xpath(
                '//div[@class="media-scorecard no-border"]//rt-text[@slot="audience-score"]/text()').get()
            clean_rating = 0.0
            if raw_rating:
                match = re.search(r'(\d+)', raw_rating)
                if match:
                    clean_rating = round(float(match.group(1)) / 10, 1)

            director_nodes = response.xpath(
                '//div[@slot="inset-text" and .//p[@data-qa="person-name" and contains(text(), "Director")]]'
                '//p[@data-qa="person-name"]/text()'
            ).getall()
            director = [d.strip() for d in director_nodes if d.strip()]

            all_people = response.xpath('//a[@data-qa="person-item"]')
            actors = []
            for person in all_people:
                role = person.xpath('.//p[@data-qa="person-role"]/text()').get()
                name = person.xpath('.//p[@data-qa="person-name"]/text()').get()

                if role and name:
                    role_clean = role.strip()
                    if "Director" not in role_clean:
                        actors.append(name.strip())

            raw_cover_url = response.xpath('//media-scorecard//rt-img/@src').get()
            clean_cover = DataCleaner.clean_cover_url(raw_cover_url) if raw_cover_url else None

            rating_count = response.xpath('//rt-link[@slot="audience-reviews"]/text()').re_first(
                r'[\d,]+\+?\s*Ratings') or "No rating_count"

            # 提取传递过来的标题，以防万一在 Python 端再用正则清洗一下 CSS 的残留
            original_title = response.meta.get("original_title", "Unknown")
            if ":host" in original_title:
                # 极端兜底：如果依然含有 :host，用正则强行把 {} 及其中的内容全部抹去
                original_title = re.sub(r':host[\s\S]*?\}', '', original_title).strip()

            yield {
                "title": original_title,
                "year": year,
                "rating": clean_rating,
                "rating_count": rating_count,
                "plot": plot.strip() if plot else "Unknown plot",
                "director": director,
                "stars": actors[:5],
                "url": response.url,
                "cover_url": clean_cover
            }
        except Exception as e:
            self.logger.error(f"❌ 详情页解析出错 {response.url}: {e}")