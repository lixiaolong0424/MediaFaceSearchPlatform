-- =============================================================================
-- migrations/004_perf_indexes.sql  ·  v5.3 界面响应性
-- 目的：界面上那几个统计查询原来都是全表扫描，在大库上每次重跑要 4~5 秒，
--       Streamlit 在这几秒里会把整页置灰（用户感受就是「屏幕变白、点不动」）。
--       这几个复合索引让「按 kind+status 计数」「按 status+has_vector 计数」
--       变成走索引的区间扫描，不再扫 51 万 / 29 万行。
-- 规则：只增不改，幂等（IF NOT EXISTS）。
-- =============================================================================

-- 文件按类型+状态计数（侧边栏、关于页、12_maintain status 都用）
CREATE INDEX IF NOT EXISTS idx_files_kind_status ON files(kind, status);

-- 人脸按状态+是否已有向量计数（照片墙口径 faces_ready / pending）
CREATE INDEX IF NOT EXISTS idx_faces_status_vec ON face_records(status, has_vector);

-- 按 content_hash 找「同一内容换了路径」的记录（v5.3 §7.2，下一步入库去重要用）
CREATE INDEX IF NOT EXISTS idx_files_hash_status ON files(content_hash, status);

-- 任务队列：按状态取最新的一条（界面每 1.5 秒查一次）
CREATE INDEX IF NOT EXISTS idx_tasks_state_created ON tasks(state, created_at DESC);
