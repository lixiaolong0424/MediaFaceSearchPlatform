-- =============================================================================
-- migrations/007_size_index.sql · 视频去重 / 跨根移动识别需要按 size 取候选
--
-- 没有这个索引时：`WHERE size=?` 是全表扫描（56 万行 ≈ 0.3s/次）。
-- 视频去重是**每个视频一次**，不建索引等于把抽帧速度砍一半。
-- =============================================================================

CREATE INDEX IF NOT EXISTS idx_files_size ON files(size);

-- down: DROP INDEX IF EXISTS idx_files_size;
