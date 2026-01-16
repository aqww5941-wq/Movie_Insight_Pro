import pymysql
from twisted.enterprise import adbapi
from utils.helpers import DataCleaner

class MysqlPipeline:
    def __init__(self, dbpool):
        self.dbpool = dbpool

    @classmethod
    def from_settings(cls, settings):
        # 从 settings.py 读取配置，实现解耦
        dbparams = dict(
            host=settings.get('MYSQL_HOST'),
            port=settings.getint('MYSQL_PORT'),
            db=settings.get('MYSQL_DBNAME'),
            user=settings.get('MYSQL_USER'),
            password=settings.get('MYSQL_PASSWORD'),
            charset='utf8mb4',
            cursorclass=pymysql.cursors.DictCursor,
            use_unicode=True,
        )
        # 创建连接池
        dbpool = adbapi.ConnectionPool('pymysql', **dbparams)
        return cls(dbpool)

    def process_item(self, item, spider):
        # 把插入操作放入异步池
        query = self.dbpool.runInteraction(self.do_insert, item, spider)
        query.addErrback(self.handle_error, item, spider) # 错误处理
        return item
    def do_insert(self, cursor, item, spider):
            # 1. 确保 SQL 结构正确
            insert_sql = """
            INSERT INTO movies (title, year, rating, rating_count, source, url, director, stars, summary,cover_url)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE 
                rating = VALUES(rating),
                rating_count = VALUES(rating_count),
                director = VALUES(director),
                stars = VALUES(stars),
                summary = VALUES(summary),
                cover_url = VALUES(cover_url)
            """
            
            # 2. 评分转换 (保留你的逻辑)
            raw_rating = item.get('rating')
            rating = 0.0
            if raw_rating and str(raw_rating).strip().lower() != 'no rating':
                try:
                    rating = float(raw_rating)
                except ValueError:
                    rating = 0.0

            # 3. 人数转换 (保留你的逻辑)
            count = DataCleaner.clean_rating_count(item.get('rating_count'))

            # 注意：日志显示 key 是 'Director' (大写) 和 'plot'
            raw_dir = item.get('Director') or item.get('director', '')
            director = ",".join(raw_dir) if isinstance(raw_dir, list) else str(raw_dir)
            
            raw_stars = item.get('stars', '')
            stars = ",".join(raw_stars) if isinstance(raw_stars, list) else str(raw_stars)
            
            # 注意：日志显示你的简介 key 是 'plot'
            summary = str(item.get('plot') or item.get('summary', ''))

            # 5. 构造参数 (顺序必须严格对应上面的 INSERT 语句)
            params = (
                item.get('title'),
                item.get('year'),
                rating,
                count,
                spider.name,
                item.get('url', ''),
                director,
                stars,
                summary,
                item.get('cover_url')
            )
            
            cursor.execute(insert_sql, params)

    def handle_error(self, failure, item, spider):
        # 这里对应你的“异常监控”初步：记录数据库错误
        print(f"❌ 数据库写入失败: {failure}")