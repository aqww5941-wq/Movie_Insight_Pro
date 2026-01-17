import scrapy
import os
import pathlib
from scrapy_playwright.page import PageMethod
import playwright_stealth

class QccSpider(scrapy.Spider):
    name = "qcc"
    allowed_domains = ["qcc.com", "qcckyc.com"]  # 加上跳转域名，防止报警
    
    # 模拟搜索华为，验证是否能拿到结果
    start_urls = ["https://www.qcc.com/web/search?key=华为"]

    def start_requests(self):
        for url in self.start_urls:
            yield scrapy.Request(
                url,
                meta={
                    "playwright": True,
                    "playwright_include_page": True,
                    "playwright_page_init_callback": self.init_page,
                    "playwright_page_methods": [
                        # 等待网络空闲（比单纯等时间更智能）
                        PageMethod("wait_for_load_state", "networkidle"),
                        # 模拟滚动，触发懒加载
                        PageMethod("evaluate", "window.scrollBy(0, 500)"),
                    ],
                },
                callback=self.parse,
                errback=self.handle_error
            )

    async def init_page(self, page, request):
        """
        【终极修复版】隐身逻辑
        不再调用不稳定的 Python 包装函数，直接找到 stealth.min.js 文件注入浏览器。
        """
        try:
            # 1. 动态定位 playwright_stealth 库里的 JS 文件路径
            # 通常路径是: site-packages/playwright_stealth/js/stealth.min.js
            stealth_path = pathlib.Path(playwright_stealth.__file__).parent / "js" / "stealth.min.js"
            
            # 2. 如果文件存在，直接注入
            if stealth_path.exists():
                await page.add_init_script(path=stealth_path)
                self.logger.info(f"🛡️ 隐身 JS 注入成功！路径: {stealth_path}")
            else:
                self.logger.error(f"⚠️ 找不到 stealth.min.js 文件，路径: {stealth_path}")
                
        except Exception as e:
            self.logger.error(f"⚠️ 隐身模式启动失败: {e}")

    async def parse(self, response):
        page = response.meta["playwright_page"]
        self.logger.info(f"🚀 当前 URL: {response.url}")

        # 1. 截图留证 (调试神器)
        # 只要截图里没有出现“滑块”或“405报错页”，就说明隐身成功了
        screenshot_path = "qcc_result.png"
        await page.screenshot(path=screenshot_path)
        self.logger.info(f"📸 页面截图已保存: {screenshot_path}")

        # 2. 尝试提取内容 (针对 QCC 搜索列表)
        # 注意：QCC 的类名经常变，这里用包含匹配，容错率更高
        titles = response.xpath('//a[contains(@class, "title")]//span/text() | //a[contains(@class, "title")]/text()').getall()
        
        # 清洗数据
        clean_titles = [t.strip() for t in titles if t.strip()]

        if not clean_titles:
            self.logger.warning("⚠️ 未抓取到公司名。可能是：1.被重定向 2.出验证码 3.选择器失效。请查看截图！")
        else:
            self.logger.info(f"✅ 成功抓取 {len(clean_titles)} 条数据")
            for name in clean_titles:
                yield {"company_name": name}

        # 3. 关闭页面
        await page.close()

    def handle_error(self, failure):
        self.logger.error(f"❌ 请求发生错误: {failure.value}")