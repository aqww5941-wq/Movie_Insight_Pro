-- 1. 开启必要插件
-- pgvector: 用于 RAG 向量检索
-- pg_trgm: 用于高效中文模糊搜索 (替代无法直接使用的 chinese 分词)
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- 2. 设置时区一致性
SET TIME ZONE 'Asia/Shanghai';

-- 3. 创建三元组模糊检索索引 (Trigram Index)
-- 针对 title 和 summary 建立复合 GIN 索引，解决 ilike 全表扫描问题
CREATE INDEX IF NOT EXISTS idx_movie_search_trgm 
ON movies USING GIN (
    (coalesce(title,'') || ' ' || coalesce(summary,'')) gin_trgm_ops
);

-- 4. 增加向量字段
-- 1536 维度适配 OpenAI / 通义千问等主流 Embedding 模型
ALTER TABLE movies ADD COLUMN IF NOT EXISTS embedding vector(1536);

-- 5. 创建 HNSW 空间索引
-- 提高 AI 语义搜索的性能，将复杂度从 O(N) 降至 O(log N)
CREATE INDEX IF NOT EXISTS movies_embedding_hnsw_idx 
ON movies USING hnsw (embedding vector_cosine_ops) 
WITH (m = 16, ef_construction = 64);