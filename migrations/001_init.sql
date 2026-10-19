-- =============================================================================
-- migrations/001_init.sql  ·  初始 schema（规格 4.1）
-- 规则：幂等、只增不改（不改列类型、不删列）、带 down 注释。
-- 迁移执行器：src/db.py::migrate()，按 db_version 顺序执行，整体包在一个事务，失败回滚。
-- =============================================================================

CREATE TABLE IF NOT EXISTS libraries (
    root          TEXT PRIMARY KEY,
    last_scan     TEXT,
    status        TEXT,
    indexed_faces INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS files (
    file_id      INTEGER PRIMARY KEY,
    path         TEXT UNIQUE NOT NULL,      -- 绝对路径
    rel_path     TEXT,                      -- 相对 media_root，用于 path_hash
    path_hash    TEXT,
    inode        INTEGER,
    size         INTEGER,
    mtime        INTEGER,                   -- 取整到秒，便于 FAT/exFAT 降级比对
    kind         TEXT CHECK (kind IN ('photo','video','unknown')),
    status       TEXT DEFAULT 'pending',
    phash        TEXT,
    captured_at  TEXT,
    time_source  TEXT,
    visible      INTEGER DEFAULT 1,
    kind_hint    TEXT,
    duration_sec REAL,
    width        INTEGER,
    height       INTEGER,
    indexed_at   TEXT,
    updated_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_files_status    ON files(status);
CREATE INDEX IF NOT EXISTS idx_files_inode     ON files(inode);
CREATE INDEX IF NOT EXISTS idx_files_path_hash ON files(path_hash);
CREATE INDEX IF NOT EXISTS idx_files_captured  ON files(captured_at DESC);

-- 镜头表：L2b 产出，人脸管线 F1 直接复用（绝不重新解码视频）
CREATE TABLE IF NOT EXISTS shots (
    file_id    INTEGER NOT NULL,
    shot_ix    INTEGER NOT NULL,
    shot_start REAL,
    shot_end   REAL,
    n_frames   INTEGER,
    PRIMARY KEY (file_id, shot_ix)
);

-- 两阶段入库的一致性锚点：先 pending → 写 Qdrant → 最后 done
CREATE TABLE IF NOT EXISTS file_encoding (
    file_id    INTEGER NOT NULL,
    model      TEXT    NOT NULL,
    status     TEXT,                       -- pending|done|error|skipped_dup
    n_frames   INTEGER,
    error      TEXT,
    encoded_at TEXT,
    PRIMARY KEY (file_id, model)
);
CREATE INDEX IF NOT EXISTS idx_file_encoding_status ON file_encoding(status, model);

CREATE TABLE IF NOT EXISTS face_records (
    face_id     INTEGER PRIMARY KEY,
    file_id     INTEGER,
    shot_ix     INTEGER,
    sec         REAL,
    bbox        TEXT,                      -- JSON: [x1,y1,x2,y2]
    det_score   REAL,
    quality     REAL,
    track_id    INTEGER,
    thumb_path  TEXT,
    raw_path    TEXT,
    status      TEXT DEFAULT 'alive',      -- alive|missing
    captured_at TEXT,
    hidden      INTEGER DEFAULT 0,
    has_vector  INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_face_browse ON face_records(status, captured_at DESC);
CREATE INDEX IF NOT EXISTS idx_face_file   ON face_records(file_id, shot_ix);
CREATE INDEX IF NOT EXISTS idx_face_thumb  ON face_records(thumb_path);

CREATE TABLE IF NOT EXISTS person_labels (
    face_group_id INTEGER,
    name          TEXT,
    created_at    TEXT
);

-- 标注工具（第十二章）：TREC 风格，调阈值/换模型的唯一依据
CREATE TABLE IF NOT EXISTS judgments (
    query_id TEXT,
    doc_id   TEXT,
    rank     INTEGER,
    score    REAL,
    label    INTEGER,
    at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_judgments_query ON judgments(query_id, rank);

CREATE TABLE IF NOT EXISTS dead_letter (
    file_id INTEGER,
    stage   TEXT,
    attempt INTEGER,
    error   TEXT,
    at      TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    who    TEXT,
    at     TEXT,
    what   TEXT,
    before TEXT,
    after  TEXT
);

CREATE TABLE IF NOT EXISTS db_version (
    v INTEGER PRIMARY KEY
);

-- 增量比对用的轻量索引表（反向比对，使增量比对秒级完成）
CREATE TABLE IF NOT EXISTS path_index (
    path_hash  TEXT PRIMARY KEY,
    file_id    INTEGER,
    inode      INTEGER,
    mtime      INTEGER,
    size       INTEGER,
    updated_at TEXT
);

-- 检索会话缓存（Tab2 用；也支撑「仅看本次新增」）
CREATE TABLE IF NOT EXISTS query_sessions (
    query_id   TEXT PRIMARY KEY,
    mode       TEXT,
    payload    TEXT,
    exact      INTEGER,
    thresh     REAL,
    warning    TEXT,
    n_hits     INTEGER,
    created_at TEXT
);

-- down: DROP TABLE query_sessions; DROP TABLE path_index; DROP TABLE audit_log;
--       DROP TABLE dead_letter; DROP TABLE judgments; DROP TABLE person_labels;
--       DROP TABLE face_records; DROP TABLE file_encoding; DROP TABLE shots;
--       DROP TABLE files; DROP TABLE libraries; DROP TABLE db_version;
