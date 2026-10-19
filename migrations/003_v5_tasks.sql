-- =============================================================================
-- migrations/003_v5_tasks.sql  ·  v5.3 新增功能地基
-- 对应规格：§3.2 任务系统 / §3.4 缓存失效事件 / §7.2 content_hash /
--           §8.3 幂等唯一索引与 lease / §15.6 聚类版本指针
-- 规则：只增不改（新增可空列 + 新表），幂等；ALTER 只跑一次（由 db_version 保证）。
-- =============================================================================

-- ---------------------------------------------------------------------------
-- §3.2 任务队列：所有耗时 > 2 秒的操作都进这张表。
-- 为什么放数据库而不是前端内存：规格要求「页面关闭再打开，任务状态必须恢复显示」。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tasks (
    task_id      TEXT PRIMARY KEY,              -- 形如 20261019-201500-a1b2c3
    kind         TEXT NOT NULL,                 -- scan_new|scan_all|extract|encode|faces|cluster|migrate|reconcile|purge|gold
    title        TEXT NOT NULL,                 -- 给人看的标题（业务词，不含开发代号）
    plan         TEXT,                          -- JSON：实际执行的分步计划
    state        TEXT NOT NULL DEFAULT 'queued',-- queued|running|success|failed|cancelled
    stage        TEXT,                          -- 当前阶段（业务词）
    stage_ix     INTEGER DEFAULT 0,
    stage_total  INTEGER DEFAULT 0,
    done         INTEGER DEFAULT 0,             -- 当前阶段已处理数
    total        INTEGER,                       -- 当前阶段总数（未知为 NULL）
    message      TEXT,                          -- 最近一行人话状态
    error        TEXT,
    pid          INTEGER,
    cancel       INTEGER NOT NULL DEFAULT 0,    -- 1=请求取消
    created_at   TEXT NOT NULL,
    started_at   TEXT,
    finished_at  TEXT,
    updated_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_state   ON tasks(state, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at DESC);

-- ---------------------------------------------------------------------------
-- §3.4 变更事件：任务结束/文件增删改/路径变更/聚类完成/设置修改都写一行，
-- 前端轮询到新事件就失效缓存并局部刷新（规格 B 方案的服务端侧）。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS task_events (
    event_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    at        TEXT NOT NULL,
    task_id   TEXT,
    kind      TEXT NOT NULL,   -- library_changed|faces_changed|clusters_changed|settings_changed|task_state
    payload   TEXT
);
CREATE INDEX IF NOT EXISTS idx_task_events_id ON task_events(event_id DESC);

-- ---------------------------------------------------------------------------
-- 键值配置（聚类版本指针 §15.6、上次基线版本 §7.4 等运行时状态放这里，
-- 不塞进 config.yaml：config.yaml 只放「人写的设置」）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT
);

-- ---------------------------------------------------------------------------
-- §7.2 content_hash：搬家/改名去重的依据（路径会变，内容不变）。
-- 落库时机：03_extract 第一次真正读取内容时顺带算，不额外读盘。
-- ---------------------------------------------------------------------------
ALTER TABLE files ADD COLUMN content_hash TEXT;
CREATE INDEX IF NOT EXISTS idx_files_hash ON files(content_hash);

-- ---------------------------------------------------------------------------
-- §8.3 status=running 的 lease 超时：进程被 kill 后自动回收，避免永久卡死。
-- ---------------------------------------------------------------------------
ALTER TABLE files ADD COLUMN lease_until TEXT;

-- ---------------------------------------------------------------------------
-- §8.3 幂等唯一索引 uk_face_vector(file_id, shot_ix, face_ix)。
-- 本项目 face_records 原先没有 face_ix，这里补列并按 face_id 顺序回填。
-- ---------------------------------------------------------------------------
ALTER TABLE face_records ADD COLUMN face_ix INTEGER;

UPDATE face_records SET face_ix = (
    SELECT COUNT(*) FROM face_records f2
    WHERE f2.file_id = face_records.file_id
      AND f2.shot_ix = face_records.shot_ix
      AND f2.face_id < face_records.face_id
) WHERE face_ix IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uk_face_vector
    ON face_records(file_id, shot_ix, face_ix);

-- ---------------------------------------------------------------------------
-- §6 人脸聚类：簇归属 + 簇元信息。版本化，支持指针原子切换与一键回退。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS cluster_runs (
    version      TEXT PRIMARY KEY,      -- v20261019_2015
    algo         TEXT,                  -- umap+hdbscan
    params       TEXT,                  -- JSON（UMAP/HDBSCAN 参数）
    n_faces      INTEGER,               -- 输入人脸数（过质量门后）
    n_faces_all  INTEGER,               -- 检测到的脸总数（规格 §15.1 要求两个数都记）
    n_clusters   INTEGER,
    n_noise      INTEGER,
    quality      TEXT,                  -- JSON：平均相似度、低置信度簇数、同脸不同人误报率
    artifact_dir TEXT,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS face_clusters (
    version     TEXT NOT NULL,
    face_id     INTEGER NOT NULL,
    cluster_id  INTEGER NOT NULL,       -- -1 = 未分组
    confidence  REAL,                   -- 簇内平均相似度（低置信度簇 < 0.45 待审核）
    PRIMARY KEY (version, face_id)
);
CREATE INDEX IF NOT EXISTS idx_fc_version_cluster ON face_clusters(version, cluster_id);

CREATE TABLE IF NOT EXISTS cluster_meta (
    version        TEXT NOT NULL,
    cluster_id     INTEGER NOT NULL,
    size           INTEGER,
    confidence     REAL,
    cover_face_id  INTEGER,             -- 代表脸
    name           TEXT,                -- 用户命名后写入
    reviewed       INTEGER DEFAULT 0,   -- 1=低置信度已人工审核
    PRIMARY KEY (version, cluster_id)
);

-- 身份合并/拆分操作日志（§6.5 秒级响应 + 5 秒内可撤销 + 审计）
CREATE TABLE IF NOT EXISTS cluster_ops (
    op_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    at         TEXT NOT NULL,
    version    TEXT,
    action     TEXT,                    -- merge|split|rename|hide
    payload    TEXT,
    undone     INTEGER DEFAULT 0
);

-- down: SQLite 3.35+ 支持 DROP COLUMN；如需回退：
--   DROP INDEX uk_face_vector; ALTER TABLE face_records DROP COLUMN face_ix;
--   DROP TABLE IF EXISTS cluster_ops, cluster_meta, face_clusters, cluster_runs;
--   DROP TABLE IF EXISTS app_settings, task_events, tasks;
--   DROP INDEX idx_files_hash; ALTER TABLE files DROP COLUMN content_hash;
--   ALTER TABLE files DROP COLUMN lease_until;
