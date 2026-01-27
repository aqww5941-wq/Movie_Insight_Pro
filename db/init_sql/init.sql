-- 如果不存在则创建数据库
CREATE DATABASE IF NOT EXISTS movie_db CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE movie_db;

-- 创建电影主表
CREATE TABLE IF NOT EXISTS movies (
    id INT AUTO_INCREMENT PRIMARY KEY,
    title VARCHAR(255) NOT NULL,
    year VARCHAR(50),
    rating DECIMAL(3,1),
    rating_count INT,
    source VARCHAR(50) COMMENT '来源：imdb 或 douban',
    url VARCHAR(255) UNIQUE, -- 唯一索引，防止重复入库
    director VARCHAR(255),
    stars TEXT,
    summary TEXT,
    -- cover_url VARCHAR(500),  
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;