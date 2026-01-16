from utils.helpers import DataCleaner
from scrapy import signals
from datetime import datetime  
import logging

class SpiderMonitorExtension:
    def __init__(self, stats):
        self.stats = stats

    @classmethod
    def from_crawler(cls, crawler):
        # 实例化扩展
        ext = cls(crawler.stats)
        
        # 1. 连接状态类信号
        crawler.signals.connect(ext.spider_closed, signal=signals.spider_closed)
        crawler.signals.connect(ext.spider_error, signal=signals.spider_error)
        
        # 2. 连接数据质量类信号
        crawler.signals.connect(ext.item_dropped, signal=signals.item_dropped)
        crawler.signals.connect(ext.item_scraped, signal=signals.item_scraped)
        
        return ext

    def spider_error(self, failure, response, spider):
        """当爬虫运行报错（如 403, 500 或代码异常）时触发"""
        spider.logger.error(f"🚨 [严重报警] 爬虫 {spider.name} 出错! URL: {response.url}")

    def item_dropped(self, item, response, exception, spider):
        """当 Pipeline 抛出 DropItem 异常时触发（如重复数据、非法字段）"""
        spider.logger.warning(f"🗑️ 数据丢弃: {exception}")

    def item_scraped(self, item, response, spider):
        """每成功抓取一条数据，计数器加一（可选，Scrapy stats 默认也会统计）"""
        pass

    def spider_closed(self, spider, reason):
        """爬虫关闭时生成最终报告"""
        item_scraped = self.stats.get_value('item_scraped_count', 0)
        dropped_count = self.stats.get_value('item_dropped_count', 0)
        error_count = self.stats.get_value('log_count/ERROR', 0)
        
        # 修正这里：直接使用 datetime.now()
        report_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        report = f"""
        =========================================
        📊 采集全量报告: {spider.name}
        -----------------------------------------
        🛑 结束原因: {reason}的
        ✅ 成功存储: {item_scraped} 条
        🗑️ 质量丢弃: {dropped_count} 条
        ❌ 错误日志: {error_count} 条
        =========================================
        报告时间: {report_time}
        """
        print(report)

        # 逻辑：只要有数据抓取或者是报错结束，都发个邮件确认
       
        subject = f"【爬虫通知】{spider.name} 任务已结束 ({reason})"
        DataCleaner.send_email(subject, report)
        
        if reason != 'finished' or error_count > 0:
             # 这里未来可以接入 API 发送微信/钉钉提醒
             spider.logger.critical("⚠️ 发现异常：邮件已发送，请检查系统！！")