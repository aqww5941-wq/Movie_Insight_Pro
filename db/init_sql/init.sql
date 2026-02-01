-- 开启 pgvector 拓展
CREATE EXTENSION IF NOT EXISTS vector;
-- 设置时区
SET TIME ZONE 'Asia/Shanghai';

-- 修改 movies 表，增加 embedding 字段
-- 1536 是 OpenAI (text-embedding-3-small) 或通义千问 (text-embedding-v2) 的标准维度
ALTER TABLE movies ADD COLUMN IF NOT EXISTS embedding vector(1536);

-- 创建 HNSW 索引（核心：让搜索从 O(N) 变成 O(log N)）
-- m=16, ef_construction=64 是平衡性能与精度的常用配置
CREATE INDEX IF NOT EXISTS movies_embedding_hnsw_idx 
ON movies USING hnsw (embedding vector_cosine_ops) 
WITH (m = 16, ef_construction = 64);