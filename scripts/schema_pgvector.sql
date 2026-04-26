-- pgvector 知识库：保存 MySQL → Redshift 成功翻译 + 结果对比通过的样本
-- 运行一次：psql "$PG_DSN" -f scripts/schema_pgvector.sql

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS sql_knowledge (
  id             BIGSERIAL PRIMARY KEY,
  mysql_sql      TEXT NOT NULL,
  redshift_sql   TEXT NOT NULL,
  embedding      vector(1024) NOT NULL,         -- cohere.embed-multilingual-v3 维度
  used_rules     TEXT[],                         -- 命中了哪些前置规则
  row_count      BIGINT,                         -- MySQL/Redshift 一致行数
  mysql_ms       INTEGER,                        -- MySQL 执行耗时
  redshift_ms    INTEGER,                        -- Redshift 执行耗时
  compare_mode   VARCHAR(16) DEFAULT 'strict',   -- strict / lenient / skipped / override
  source         VARCHAR(16) DEFAULT 'runtime',  -- runtime / import / seed
  hit_count      INTEGER DEFAULT 0,              -- 召回次数（淘汰用）
  created_at     TIMESTAMPTZ DEFAULT NOW(),
  last_used_at   TIMESTAMPTZ DEFAULT NOW()
);

-- HNSW 索引（cosine 距离）
CREATE INDEX IF NOT EXISTS idx_sql_knowledge_embedding
  ON sql_knowledge USING hnsw (embedding vector_cosine_ops)
  WITH (m=16, ef_construction=64);

-- 去重：相同 mysql_sql 只存一次（upsert 覆盖）
CREATE UNIQUE INDEX IF NOT EXISTS idx_sql_knowledge_mysql_md5
  ON sql_knowledge (md5(mysql_sql));

-- 检索时间索引（淘汰/观测用）
CREATE INDEX IF NOT EXISTS idx_sql_knowledge_last_used
  ON sql_knowledge (last_used_at DESC);
