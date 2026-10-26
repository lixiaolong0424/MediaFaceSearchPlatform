-- =============================================================================
-- migrations/008_stage_status.sql · 补齐 stage_status 表
--
-- 为什么现在才补：这张表一直存在于**现有库**里（1,745,107 行），但它从来没有
-- 出现在 migrations/*.sql 中 —— 说明当年是手工/代码里建的。后果：
--   1. 全新安装（或测试用的临时库）根本没有这张表；
--   2. `migrate._purge_rows()` 删记录时会 `DELETE FROM stage_status WHERE file_id=?`，
--      在全新库上直接抛 OperationalError: no such table。
--      2026-10-22 复现：tests/test_disable_remove.py 的「移除素材根」一步就崩在这里。
-- 用 IF NOT EXISTS，对现有库无影响。
-- =============================================================================

CREATE TABLE IF NOT EXISTS stage_status (
    file_id    INTEGER NOT NULL,
    stage      TEXT    NOT NULL,
    status     TEXT,                -- pending|done|error|skipped
    shots      INTEGER DEFAULT 0,
    frames     INTEGER DEFAULT 0,
    error      TEXT,
    updated_at TEXT,
    PRIMARY KEY (file_id, stage)
);

CREATE INDEX IF NOT EXISTS idx_stage_status ON stage_status(stage, status);

-- down: DROP TABLE IF EXISTS stage_status;
