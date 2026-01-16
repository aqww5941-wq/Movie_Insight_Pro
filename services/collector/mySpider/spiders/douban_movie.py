import scrapy
import json
import re
from pathlib import Path
from utils.helpers import DataCleaner  # 确保路径正确

class MovieSpider(scrapy.Spider):
    name = "douban"
    
    # 你想遍历的分类列表
    categories = ["热门", "最新", "豆瓣高分", "冷门佳片"]

    def start_requests(self):
        # 1. 加载配置文件
        config_path = Path("configs/spider_targets.json")
        if not config_path.exists():
            self.logger.error(f"❌ 配置文件 {config_path} 未找到！")
            return

        with open(config_path, 'r', encoding='utf-8') as f:
            targets = json.load(f)

        my_targets = targets.get(self.name, [])

        # 标准豆瓣 API 请求头
        headers = {
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 14_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0.3 Mobile/15E148 Safari/604.1",
            "Referer": "https://m.douban.com/movie/",
        }

        for target in my_targets:
            base_url = target['url']
            json_meta = target.get('mata', {}) # 保持你的 mata 拼写

            # --- 高手逻辑：双重循环构造请求池 ---
            for cat in self.categories:
                # 构造初始 URL，从 start=0 开始
                final_url = f"{base_url}?start=0&limit=20&category={cat}&type=全部"
                
                # 将当前分类信息存入 meta，传递给 parse
                request_meta = json_meta.copy()
                request_meta['current_category'] = cat

                yield scrapy.Request(
                    url=final_url,
                    headers=headers,
                    meta=request_meta,
                    callback=self.parse,
                    dont_filter=True
                )

    def parse(self, response):
        # 1. 解析 JSON 数据
        try:
            data = json.loads(response.text)
        except Exception as e:
            self.logger.error(f"解析JSON失败: {e}")
            return

        items = data.get("subject_collection_items") or data.get("items") or []
        
        if not items:
            self.logger.info(f"🚩 分类 [{response.meta.get('current_category')}] 抓取完毕或无数据")
            return

        # 2. 遍历处理数据
        for m in items:
            # 豆瓣 API 的结构解析
            title = m.get("title")
            subtitle = m.get("card_subtitle") or "" # 或者是 m.get("info")
            
            # 使用正则拆解 subtitle (1994 / 美国 / 剧情 / 弗兰克·德拉邦特 / 蒂姆·罗宾斯)
            year, director, stars, genres = "", "", "", ""
            pattern = r"^(.*?) / (.*?) / (.*?) / (.*?) / (.*)$"
            match = re.match(pattern, subtitle)

            if match:
                year = match.group(1).strip()        
                genres = match.group(3).strip()      
                director = match.group(4).strip()    
                stars = match.group(5).strip()  
            else:
                parts = subtitle.split(" / ")
                if len(parts) >= 1: year = parts[0]
                if len(parts) >= 4: director = parts[3]

            # --- 调用你的 DataCleaner 强力清洗 ---
            raw_cover = m.get("pic", {}).get("large") or m.get("cover", {}).get("url")
            clean_cover = DataCleaner.clean_cover_url(raw_cover)
            
            raw_count = m.get("rating", {}).get("count") or 0
            clean_count = DataCleaner.clean_rating_count(raw_count)

            yield {
                "title": title,
                "year": year,
                "rating": m.get("rating", {}).get("value") or 0,
                "rating_count": clean_count,
                "url": m.get("uri", "").replace("douban://douban.com/movie/", "https://movie.douban.com/subject/"),
                "cover_url": clean_cover,
                "director": director, 
                "stars": stars,
                "summary": f"[{response.meta.get('current_category')}] {genres}", # 把分类标在简介里
                "source": "douban"
            }

        # --- 3. 自动翻页逻辑 ---
        # 检查当前 URL 里的 start 参数
        current_start_match = re.search(r'start=(\d+)', response.url)
        if current_start_match:
            current_start = int(current_start_match.group(1))
            next_start = current_start + 20
            
            # 获取接口返回的总数（如有）
            total = data.get("total", 0)
            
            # 安全阈值：start 小于总数，且为了防止封号设置最大爬取页数（如前 100 条）
            if next_start < total :
                next_url = re.sub(r'start=\d+', f'start={next_start}', response.url)
                
                self.logger.info(f"⏭️ 正在翻页: {response.meta.get('current_category')} -> start={next_start}")
                
                yield scrapy.Request(
                    url=next_url,
                    headers=response.request.headers,
                    meta=response.meta,
                    callback=self.parse
                )