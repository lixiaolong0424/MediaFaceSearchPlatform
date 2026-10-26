# 架构说明（docs/ARCHITECTURE.md）

本文只讲「系统怎么搭起来的」。操作层面看 `docs/RUNBOOK.md`，agent 须知看根目录 `AGENTS.md`。

---

## 1. 总体形状

```
┌─────────────────────────── 界面层（Streamlit）───────────────────────────┐
│ app.py         6 个导航页（照片墙/搜索/任务/设置/维护/关于）              │
│ src/ui_pages   设置、维护、任务、关于 页实现                              │
│ src/ui_groups  「按人浏览」视图（分组卡片 + 组详情 + 命名/合并/拆分）    │
│ src/ui_wall    「全部人脸」平铺视图（keyset 分页 + 过滤）                 │
│ src/ui_actions 任务启停、进度 fragment、事件轮询、素材根向导              │
│ src/ui_text    全部文案字典（禁内部代号）                                 │
└──────────────────────────────┬──────────────────────────────────────────┘
                               │ 读：SQLite（只读连接） + Qdrant 检索
                               │ 写：只通过「任务」间接写（保证单写者）
┌──────────────────────────────┴──────────────────────────────────────────┐
│                      任务层（src/tasks.py + run_task.py）                 │
│ tasks：tasks/task_events 表、进度上报、取消、心跳、僵尸回收               │
│ taskplan：把 kind 翻译成步骤列表（Step: script/args/code/event）          │
│ run_task：逐步 spawn 子进程；每步独立日志；退出码 0/2/130                 │
└──────────────────────────────┬──────────────────────────────────────────┘
┌──────────────────────────────┴──────────────────────────────────────────┐
│                          流水线层（脚本）                                 │
│ L1 01_manifest → L2b 03_extract → L2c 04_encode → F1/F2 06_face_detect    │
│                    ↘ L2a 02_dedup（照片侧融合进 03）    → F3 07_face_encode│
│ 离线运维：16_cluster（分组） / 17_gold（标定） / 18_reconcile（核对）     │
│          11_export（导出） / 12_maintain（运维子命令）                    │
└──────────────────────────────┬──────────────────────────────────────────┘
┌──────────────────────────────┴──────────────────────────────────────────┐
│                        存储层                                             │
│ SQLite data/library.db（WAL，单库，含全部元数据）                         │
│ Qdrant 127.0.0.1:7333：materials_semantic（画面）/ materials_faces（人脸）│
│ NVMe data/nvme/：frames/<fid>/…（关键帧）、faces/raw|thumbs/<fid>.webp    │
│ 原始素材：只读，系统永不修改（三条铁律之一）                              │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## 2. 阶段职责与产物

| 阶段 | 脚本 | 输入 | 产物 | 幂等键 |
|---|---|---|---|---|
| L1 清单 | `01_manifest.py` | 素材根 | `files` / `path_index` / `libraries.last_scan`；五类判定 | `(root, path_hash)` |
| L2b 抽帧 | `03_extract.py` | `files.status='pending'` | `shots`、`files.captured_at/width/height`、`data/nvme/frames/**`、照片 pHash | `(file_id, shot_ix)` |
| L2a 去重 | `02_dedup.py`（照片侧已融合进 03） | 照片 pHash | `files.status='skipped_dup'` + `file_encoding` 两行 | `uk_face_vector` 等 |
| L2c 画面编码 | `04_encode.py` | `shots` + frames | `materials_semantic`（point_id=`(file_id<<20)|shot_ix`）、`file_encoding(semantic)` | point_id |
| F1/F2 人脸检测 | `06_face_detect.py` | frames | `face_records`（bbox/quality/track）、`faces/raw/*.webp` | `(file_id,shot_ix,face_ix)` |
| F3 人脸编码 | `07_face_encode.py` | `faces/raw` | `materials_faces`（point_id=`face_id`）、`faces/thumbs/*.webp`、`has_vector=1` | face_id |
| 分组 | `16_cluster.py` | `face_records`(alive+向量) | `cluster_runs` / `face_clusters` / `cluster_meta` + `data/clusters/<ver>/{assignments.parquet,centroid.npy,config.json}` | `version` |
| 核对 | `18_reconcile.py` | 全部 | 报告（A 孤儿点 / B 悬挂行 / C 漂移） | — |

**照片特例**：照片没有镜头概念，`03_extract` 会写一条 `shot_ix=0` 的虚拟镜头，使「语义点 id」与「人脸点 id」规则统一。

---

## 3. 向量与 id 规则

* 画面点：`point_id = (file_id << 20) | shot_ix`（file_id < 2^43、shot_ix < 2^20）
* 人脸点：`point_id = face_id`（payload 里另带 `file_id/shot_ix` 供反查）
* payload 必备字段：`file_id, kind, shot_ix, sec, thumb_path`（人脸另有 `bbox/quality`）
* 语义向量 2048 维（cosine，int8 量化可选），人脸 512 维（cosine）
* 检索只认 `files.status='done' AND visible=1`：Qdrant 召回后一律回 SQLite 过滤

---

## 4. 并发与写锁模型

* **SQLite 是唯一权威**，且**同时只有一个写者**（任务队列保证）。
* WAL + `busy_timeout=60000`；`db.retry_on_lock()` 对 `database is locked` 退避重试。
* 写锁窗口必须短：照片侧 `scan.commit_batch_fused_photos=1`（每文件提交），
  视频侧按「每 2 秒 / 每 N 镜头」分片提交。
* 读路径（界面）用只读连接，永不加写锁；界面缓存按 `task_events` 版本失效。
* 进度上报用**独立连接**，且必须在主连接 commit **之后**，否则两个连接互等（历史 bug）。

---

## 5. 任务与事件

```
tasks(id, kind, title, plan, state, stage, stage_ix/stage_total, done/total,
      cancel, pid, stage_started_at, started_at, finished_at, error)
task_events(event_id, at, kind, payload, task_id)
```

* `kind ∈ {scan_new, scan_all, extract, encode, faces, face_detect, face_encode,
  cluster, migrate, reconcile, purge, gold}`
* 事件 kind：`task_state`（开始/结束）、`library_changed`、`faces_changed`、
  `clusters_changed`、`settings_changed`、`media_root_changed`
* 界面 `watch_events` 只在**数据类事件**时整页刷新（`task_state` 不刷，避免白屏闪烁）

---

## 6. 界面结构

* 单页 + `st.segmented_control` 导航（6 项），只渲染当前页（不用 `st.tabs`，避免全跑一遍）
* 缓存键含 `data_version = 最新 event_id`：有新数据就自动换 key
* fragment（`run_every`）：侧边栏「当前任务」1.5s、事件轮询 2s、NVMe 占用 4s
  ⚠️ **侧边栏折叠后 fragment 不运行**，需要"必须跑"的逻辑请挂主区域
* 弹窗限制：Streamlit 1.66 的 `st.dialog` 无 `key` 参数，同一轮里只能弹一个；
  本项目「组详情」已改为**页面内面板**（`open_group` 持久状态）
* 静态服务（`src/static_serve.py`）：`/nvme/**`（关键帧/人脸图）、`/media/**`（原素材，
  文件级白名单）+ `/media/<path>?start=&end=` 支持 Range 播放

---

## 7. 失败语义

| 情况 | 表现 | 处理 |
|---|---|---|
| 单文件处理失败 | `files.status='error'` + `stage_status(stage).error` + `dead_letter` 计数 | 下次任务重试；达 `_max_extract_attempts` 后不再重试 |
| 整个阶段退出非 0 | `tasks.state='failed'`，`error="0X_xxx.py 退出码 N"` | 看 `logs/task_<id>.log` 尾部 |
| 用户停止 | `cancel=1` → 阶段脚本抛 `TaskCancelled` → 退出码 130 | `tasks.state='cancelled'` |
| 进程被强杀 | 心跳停更；`reap_stale()` 判死后置 `failed`/`cancelled` | 已提交进度全部保留，重跑续接 |
| 原生崩溃（如栈溢出） | 退出码 `3221225725`，无 Python 栈 | 见 `AGENTS.md` 陷阱 #4 |

---

## 8. 目录与磁盘预算

| 路径 | 内容 | 预算 |
|---|---|---|
| `data/library.db` | SQLite（含 WAL） | 当前约 1.0 GB / 561k 文件 |
| `data/nvme/frames` | 关键帧 JPEG（长边 384） | 主要占用，UI 有配额监控 |
| `data/nvme/faces/{raw,thumbs}` | 人脸对齐图 / 缩略图（webp） | 每脸约 10~20 KB |
| `data/clusters/<ver>` | 分组产物（parquet + centroid.npy） | 每版几十 MB，保留 `cluster.keep_versions=3` |
| `logs/` | 每日日志 + 每任务日志 | 轮转见 `12_maintain.py rotate-logs` |
