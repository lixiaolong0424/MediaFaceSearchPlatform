-- =============================================================================
-- migrations/005_media_roots.sql  ·  多素材目录（v5.3 §4 扩展）
-- 目标：用户可以在界面上**增 / 删 / 改**素材目录，多个目录共存。
--
-- 关键改动：
--   files.root       每个文件属于哪个素材根（绝对路径，带结尾分隔符）
--   path_index      主键改成 (root, path_hash)：两个根下出现同名相对路径
--                   不再互相覆盖（这是支持多根的前提）
--   libraries       变成「用户登记的素材目录列表」：label / enabled / added_at / sort_ix
--
-- files.root 的回填是**精确**的：由于 path 恒等于「根 + rel_path」，
-- 所以 root = substr(path, 1, length(path) - length(rel_path))。
-- =============================================================================

ALTER TABLE files ADD COLUMN root TEXT;
CREATE INDEX IF NOT EXISTS idx_files_root ON files(root);

UPDATE files
   SET root = substr(path, 1, length(path) - length(rel_path))
 WHERE root IS NULL
   AND rel_path IS NOT NULL AND rel_path != ''
   AND length(path) > length(rel_path);

ALTER TABLE libraries ADD COLUMN label TEXT;
ALTER TABLE libraries ADD COLUMN enabled INTEGER DEFAULT 1;
ALTER TABLE libraries ADD COLUMN added_at TEXT;
ALTER TABLE libraries ADD COLUMN sort_ix INTEGER DEFAULT 0;

-- ---- path_index 重建为 (root, path_hash) 复合主键 ----
ALTER TABLE path_index RENAME TO path_index_old;

CREATE TABLE path_index (
    root       TEXT NOT NULL DEFAULT '',
    path_hash  TEXT NOT NULL,
    file_id    INTEGER,
    inode      INTEGER,
    mtime      INTEGER,
    size       INTEGER,
    updated_at TEXT,
    PRIMARY KEY (root, path_hash)
);
CREATE INDEX IF NOT EXISTS idx_path_index_file ON path_index(file_id);

INSERT OR REPLACE INTO path_index(root, path_hash, file_id, inode, mtime, size, updated_at)
SELECT COALESCE(root, ''), path_hash, file_id, inode, mtime, size, updated_at
  FROM files
 WHERE path_hash IS NOT NULL;

DROP TABLE path_index_old;

-- down: 重建旧 path_index 需先（root 列直接忽略）：
--   CREATE TABLE p2(path_hash TEXT PRIMARY KEY, file_id INTEGER, inode INTEGER,
--                   mtime INTEGER, size INTEGER, updated_at TEXT);
--   INSERT OR REPLACE INTO p2 SELECT path_hash, file_id, inode, mtime, size, updated_at FROM path_index;
--   DROP TABLE path_index; ALTER TABLE p2 RENAME TO path_index;
