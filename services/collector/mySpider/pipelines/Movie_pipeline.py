# Define your item pipelines here
#
# Don't forget to add your pipeline to the ITEM_PIPELINES setting
# See: https://docs.scrapy.org/en/latest/topics/item-pipeline.html


# useful for handling different item types with a single interface
from itemadapter import ItemAdapter
import json
from pathlib import Path


class MoviePipeline:

    def open_spider(self, spider):
        # 1. 动态确定路径和文件名
        if spider.name == "douban":
            folder = "data/douban"
        elif spider.name == "imdb":
            folder = "data/imdb"
        else:
            folder = f"data/{spider.name}"
            
        filename = "Lowest_rated_movies.jsonl"
        
        # 2. 这里的路径必须引用上面定义的变量 folder
        path = Path(folder)
        path.mkdir(parents=True, exist_ok=True)
        
        # 3. 使用变量拼接完整路径，不要写死
        file_path = path / filename
        self.file = open(file_path, "a", encoding="utf-8")

    def process_item(self, item, spider):
        line = json.dumps(dict(item), ensure_ascii=False)
        self.file.write(line + "\n")
        return item

    def close_spider(self, spider):
        if hasattr(self, 'file'):
            self.file.close()
