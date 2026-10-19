-- =============================================================================
-- migrations/002_add_last_hit.sql
-- 目的：为 frames/ 的 LRU 清理提供真实的「最近是否被命中」依据。
--   规格第三章要求「frames/ 每日 LRU 清理（淘汰最近 N 天未被命中的关键帧）」，
--   没有命中时间戳就无法正确判断「未被命中」。
-- 规则：只增不改（新增可空列），幂等。
-- =============================================================================

ALTER TABLE files ADD COLUMN last_hit_at TEXT;
ALTER TABLE files ADD COLUMN hit_count INTEGER DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_files_last_hit ON files(last_hit_at);

-- down: SQLite 不支持 DROP COLUMN（3.35 以下）；如需回退请重建表。
--       ALTER TABLE files DROP COLUMN last_hit_at;
--       ALTER TABLE files DROP COLUMN hit_count;
