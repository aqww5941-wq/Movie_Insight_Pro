-- 1. 开启必要插件 (Alembic 无法自动开启 Extension，必须手动)
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- 2. 设置时区 (影响数据库底层时间存储)
SET TIME ZONE 'Asia/Shanghai';

-- 后面关于 CREATE INDEX, ALTER TABLE 的全部删掉！