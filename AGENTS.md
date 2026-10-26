# AGENTS.md · 给 AI Agent / 新接手工程师的说明书

> 这份文件的目标：**让一个新的 agent 在 10 分钟内能安全地改这个项目**。
> 先读完「三条铁律」和「五条不能碰的不变量」，再动手。
> 人类用户的完整手册在 `README.md`（很长、含所有实测数据与历史偏离），
> 命令速查在 `script.md`，架构图在 `docs/ARCHITECTURE.md`，运维手册在 `docs/RUNBOOK.md`。

---

## 1. 这是什么

离线本地「素材人脸检索系统」：把整盘照片/视频建成索引，支持

* **照片墙**：按人浏览（人脸聚类分组）+ 全部人脸平铺
* **搜索**：文字描述找画面 / 传图找画面 / 传人脸找同款
* **任务队列**：扫描 → 抽帧 → 画面编码 → 人脸检测 → 人脸编码，带进度、可停止、可续跑
* **维护**：完整性检查、缺失文件清理、备份、灵敏度标定、分组版本回退

技术栈：Python 3.12（项目内 `.venv`）+ SQLite（`data/library.db`，WAL）+ Qdrant（`127.0.0.1:7333`）
+ Qwen3-VL-Embedding-2B（2048 维画面向量）+ insightface buffalo_l / ArcFace（512 维人脸向量）
+ Streamlit（`app.py`，单页 6 个导航）+ ffmpeg（外部 exe）。

---

## 2. 30 秒上手

```powershell
cd "D:\DATA\PROJECT\建设多媒体整理系统"

# 启动界面（自带启动前准备 + 自检 + Qdrant 检查）
.\start.bat                       # 浏览器打开 http://127.0.0.1:8501

# 一律用项目自带的解释器，不要用系统 python
.venv\Scripts\python.exe tests\test_v53.py     # 核心回归，应 PASS=75 FAIL=0
.venv\Scripts\python.exe tests\p0_smoke.py     # 端到端冒烟，应 PASS=43 FAIL=0
.venv\Scripts\python.exe tools\status.py       # 当前任务 / 进度速览

# 常用环境变量（中文输出必需）
$env:PYTHONUTF8='1'; $env:PYTHONIOENCODING='utf-8'
```

**改动后必须做**：跑 `tests\test_v53.py`；若改了 UI 还要跑
`.venv\Scripts\python.exe tmp\apptest_pages.py`（不存在时用 `tools/` 或 `tests/` 里的替代脚本），
并重启界面（`src/` 下的改动**不会**热重载，只有 `app.py` 会）。

---

## 3. 目录地图

```
根目录（薄壳入口，只解析 CLI，业务逻辑全在 src/）
  00_bootstrap.py   启动自检（21 必过 + 3 警告）
  01_manifest.py    L1 清单扫描（new/modified/moved/unchanged/missing 五类）
  02_dedup.py       L2a pHash 去重（可独立跑；照片侧默认融合进 03）
  03_extract.py     L2b 抽帧 + EXIF/容器时间 + （照片）pHash + 768px 缓存图
  04_encode.py      L2c 画面向量 → Qdrant materials_semantic
  05_rerank.py      L2d 重排序（查询期，不参与建库）
  06_face_detect.py F1+F2 人脸检测/对齐/track → face_records
  07_face_encode.py F3 ArcFace 512 → Qdrant materials_faces + 缩略图
  08_search.py      命令行检索
  11_export.py      TSV / 结构化 TXT 导出
  12_maintain.py    运维子命令（status/repair/reconcile/purge-missing/…）
  16_cluster.py     自动分组（人脸聚类，产出带版本的结果）
  17_gold.py        灵敏度标定
  18_reconcile.py   完整性核对
  app.py            Streamlit 界面（唯一入口）
  run_task.py       任务执行器：按 plan 逐步 spawn 上面的脚本
  start.bat         双击启动（必须 CRLF 行尾）

src/              业务模块
  common.py         Config / 日志双写 / CLI 骨架 / point_id / perf_value
  db.py             SQLite 连接(WAL) + 迁移执行器 + DAO + retry_on_lock
  manifest.py       L1 实现（classify / apply_classification）
  extract.py        L2b 实现（process_photo / process_video / scene_detect / PhotoPrefetcher）
  encode.py         编码批处理与落库
  encode_model.py   Qwen3-VL 模型加载与 encode_images
  rerank.py         L2d 实现
  search.py         检索门面（召回 → 重排 → 截断）
  cluster.py        人脸聚类（reduce_dims / cluster_with_sampling / meta / 命名合并拆分）
  face_pipeline.py  人脸管线实现（imread_bgr / 检测 / 编码 / 缩略图）
  ui_*.py           界面分页与组件（ui_groups / ui_pages / ui_actions / ui_wall / ui_common / ui_text）
  media_roots.py    多素材根登记（增删改、启用/停用、按根清理）
  migrate.py        路径变更、缺失处理、_purge_rows
  settings_store.py 全部可调设置的声明式注册表（L1 常用 / L2 高级）
  static_serve.py   本地静态服务（/nvme /media，文件级白名单）
  vector_store.py   VectorStore 抽象 + Qdrant 实现（含 delete_ids 批量删）

migrations/       001…008，只增不改（见「加迁移」）
tests/            回归与冒烟（见「测试地图」）
tools/            诊断/基准脚本（见 tools/README.md）
docs/             架构 / 运维 / 变更日志
data/             library.db + nvme 派生物（**不进 git**）
weight/           模型权重（**不进 git**）
logs/  tmp/       日志与临时脚本（**不进 git**）
```

---

## 4. 三条铁律（违反会静默毁数据）

1. **去重跳过的文件，两个模型各记一行 `skipped_dup`**
   `file_encoding` 里 `semantic` 与 `face` 必须各有一行，否则阶段计数对不上。
2. **删除只软删**：`files.status='missing'`，记录/向量/人脸全部保留；文件回来要能 `revived`。
   绝不物理删文件，也绝不因为「看不到」就删记录。
3. **两阶段入库**：`pending → 写 Qdrant → done + encoded_at`；检索只 join `status='done'`。

---

## 5. 五条不能碰的不变量

| # | 不变量 | 违反后果 |
|---|---|---|
| 1 | `scan.commit_batch_fused_photos` 必须 = **1** | 设成 100 时写锁窗口约 12 秒，编码进程被饿死（实测 0.06 文件/秒，比不并发慢 50 倍） |
| 2 | `db.busy_timeout` 60 秒 + `retry_on_lock()` 退避重试必须保留 | 一次锁竞争就让编码进程抛 `database is locked` 退出，丢整轮工作 |
| 3 | **同一时刻只允许一个任务**（`tasks.active()` 为空才能起新任务） | 两份流水线同时写库会互相拖死，历史上滚动出过 3 份并发 |
| 4 | `files.root` 必须带结尾分隔符（`E:\`），`point_id=(file_id<<20)\|shot_ix`（人脸 collection 用 `face_id`） | 分类/移动判定错乱；向量点 id 冲突 |
| 5 | 界面里的**素材根一旦新增/停用，必须让 `cluster.refresh_meta_for_root()` 重算** | 照片墙上留着已停用目录的人（2026-10-26 用户实测反馈过） |

---

## 6. 数据模型摘要

主要表（完整 DDL 见 `migrations/001_init.sql`…`008_stage_status.sql`，当前 schema **v8**）：

| 表 | 作用 | 关键字段 |
|---|---|---|
| `files` | 素材主表 | `file_id, path, rel_path, root, kind, size, mtime, inode, status, visible, captured_at, phash, width, height` |
| `libraries` | 素材根登记 | `root, enabled, last_scan, status` |
| `path_index` | 路径哈希索引（PK `(root,path_hash)`） | 用于增量判定 |
| `shots` | 镜头（照片也有 1 条 shot_ix=0） | `file_id, shot_ix, shot_start, shot_end, n_frames` |
| `face_records` | 人脸 | `face_id, file_id, shot_ix, bbox, quality, has_vector, status, thumb_path, raw_path` |
| `face_clusters` | 分组版本的人脸归属 | `version, face_id, cluster_id, confidence`（PK `(version,face_id)`） |
| `cluster_meta` | 组的展示信息 | `version, cluster_id, size, confidence, cover_face_id, name` |
| `cluster_runs` | 分组版本元数据 | `version, n_faces, n_clusters, n_noise, artifact_dir` |
| `file_encoding` | 两阶段状态 | `file_id, model(semantic/face), status, encoded_at, error` |
| `stage_status` | 各阶段进度 | `file_id, stage, status, shots, frames, error` |
| `tasks` / `task_events` | 任务与事件 | `task_id, kind, state, stage, done, total, cancel, pid, stage_started_at` |
| `app_settings` | 键值状态 | `cluster_version`, `nvme_usage` 等 |
| `dead_letter` | 反复失败的文件 | `file_id, stage, attempts, error` |

**语义**：
* `files.visible=0` = 该素材根被「停用（不显示·不搜索）」；读路径必须过滤它。
* `status='missing'` = 软删；`skipped_dup` = 去重让位（`visible=0`）。
* 分组是**快照**：改了门槛/新增文件，必须重新分组才会体现在「按人浏览」。

---

## 7. 运行架构

```
用户点按钮
  → ui_actions.start_task(kind)          # 建 tasks 行 + spawn run_task.py（分离进程）
  → run_task.py                          # 按 plan 逐步 spawn 01/03/04/06/07
      每一步：独立子进程、独立日志 logs/task_<id>.log、返回码 0/2/130
  → 阶段脚本写库（files/shots/faces/Qdrant）并用 tasks.tick() 上报进度
  → 阶段脚本结束写 task_events（library_changed / faces_changed / task_state）
  → 界面 fragment 每 1.5~2 秒轮询：进度条 + 缓存失效 + 整页刷新
```

* 停止：`tasks.request_cancel` 写 `cancel=1`；各阶段**每个文件开头**检查 `T.cancelled()`；
  `ui_actions.stop_task` 另有 12 秒看门狗（`taskkill /T /F`）兜底。
* 心跳：`tasks.updated_at`；`reap_stale()` 只在**心跳超时且 pid 不是该任务的进程**时判死
  （pid 复用会让「进程还活着」误判，见陷阱 #1）。
* 进度：`tasks.done/total/stage/stage_ix/stage_total/stage_started_at`；速度与 ETA 按**当前阶段**算。

---

## 8. 常见改动怎么做

### 加一个设置项
1. `src/settings_store.py` 的 `SETTINGS` 里加 `Setting(...)`（选 `L1` 常用 / `L2` 高级，`restart`/`rescan` 标记）
2. 若要默认值进 `config.yaml`，同步加（写注释说明实测依据）
3. `tests/test_v53.py` 第 2 节会校验「项数一致」，跑一遍
4. **不需要**改 UI：设置页按注册表自动渲染

### 加一条迁移
* 新文件 `migrations/00N_xxx.sql`，**只增不改**（`ALTER TABLE ... ADD COLUMN` / `CREATE INDEX IF NOT EXISTS` / `CREATE TABLE IF NOT EXISTS`）
* 头部写清「为什么加」，末尾写 `-- down:`
* 启动时 `db.ensure_schema()` 自动执行；测试里也用同一函数建临时库

### 加一个界面页
* 页面函数放 `src/ui_pages.py`；`app.py` 的 `NAV_PAGES` 加一项 + 路由 `elif page == "...":`
* 文案一律走 `src/ui_text.py::t()`，**不要**在 UI 里出现内部代号（有 `BANNED_IN_UI` 校验）

### 加一个任务类型
* `src/tasks.py::KINDS` 加种类；`src/taskplan.py::plan_for()` 给出步骤列表
* 步骤脚本的退出码约定：`0` 成功 / `2` 业务失败（打印原因）/ `130` 用户取消

### 改 `src/` 之后
* **必须重启界面**（Streamlit 只热重载 `app.py`；`src/*` 已 import 的模块不会刷新）
* 重启界面**不会**杀掉正在跑的任务（子进程是分离的）；但**关掉 `start.bat` 窗口会**

---

## 9. 已知陷阱（都是真实踩过的，别再踩）

| # | 陷阱 | 正确做法 |
|---|---|---|
| 1 | **PID 复用**：用「pid 还活着」判断任务未死 → 2 天前的失败任务 pid 被浏览器进程占用，于是**所有任务按钮都点不动** | 用 `tasks.pid_looks_like_task(pid, task_id)`（校验命令行含 `run_task.py` + task_id），并调 `clear_reused_pids()` |
| 2 | **侧边栏折叠时 fragment 不运行**：把「任务结束」状态机放侧边栏，折叠后完全失效（截图 30 秒无变化） | 状态机放**主区域**；侧边栏只做展示 |
| 3 | **按字符串替换插代码会插错函数**：`with Image.open(path) as im:` 在 `validate_frame()` 里先出现，导致视频帧全部报 `name 'prefetched' is not defined`，白跑 1 小时 38 分 | 用**行号/上下文（含函数名）**定位；改完**立刻**跑相关阶段的小样本 |
| 4 | **HDBSCAN 30 万点会栈溢出**（退出码 `3221225725` = 0xC00000FD），Python 抓不到 | 多轮抽样 + 128MB 栈线程（`cluster_with_sampling`）；拟合上限 `cluster.max_fit_faces` 默认 12 万 |
| 5 | **两个"清晰度下限"**：`faces.quality_min_show`（照片墙显示）vs `cluster.quality_gate`（参与分组） | 用户说「调低了下限还是没归组」时，先看后者；后者改了**必须重新分组** |
| 6 | **`config.local.yaml` 覆盖 `config.yaml`**：界面保存的设置都在覆盖层 | 调参不生效时先看覆盖层 |
| 7 | **照片预读线程必须共享游标**：每个线程各自遍历同一列表 → 每个文件读 N 遍，进度几乎停滞 | `PhotoPrefetcher._claim()` 加锁取下一项 |
| 8 | **视频写事务过长**：整片一个事务 → 界面/取消都 `database is locked` | 按 2 秒/每 N 镜头分片提交 |
| 9 | **静态服务白名单是启动快照**：移动文件后新路径 404 | 未命中时做一次 `files.path` 点查（`_indexed_now`） |
| 10 | **`Path.resolve()` 会把路径变成 8.3 短名**（`LIXIAO~1`），前缀匹配失败 | 停用/清理同时按「原样前缀 + resolve 后前缀」匹配（`_root_conditions`） |
| 11 | **换 `media_root` 走全局差集会把全库标 missing**（历史事故，已修为按根限定） | 多根用 `media_roots` 登记；扫子目录注意 `root` 字段 |
| 12 | **`start.bat` 必须 CRLF** | 用编辑器改过后确认行尾，或重跑生成脚本 |

---

## 10. 性能旋钮（全部可在「设置」里改）

| 键 | 默认 | 说明 |
|---|---|---|
| `performance.mode` | `balanced` | `turbo` 会让下面几项按档位自动取值 |
| `performance.photo_prefetch` | 0（turbo 6） | 照片预读线程（只并行读盘）。实测 3000×2000：13.9→24.6 张/秒 |
| `performance.frame_workers` | 0（turbo 8） | **同一视频内**并行抓帧。实测 7.2GB 视频 429s→67s（6.4×） |
| `performance.video_workers` | 0（turbo 12） | 视频并发上限（仍受可用内存约束） |
| `performance.encode_batch` / `encode_group` | 0（turbo 32/12） | 编码批大小/分组大小（OOM 会自动折半） |
| `video.scdet_max_duration_sec` | 1800 | 超长视频跳过全量镜头检测，按 `force_split_sec` 切段（省一次整片解码） |
| `cluster.max_fit_faces` | 120000 | 单次 HDBSCAN 拟合上限；超出走多轮抽样 |
| `cluster.assign_dist_factor` | 1.2 | 抽样外的人脸按「簇半径 95 分位 × 系数」归组 |
| `cluster.quality_gate` | 0.30 | 参与分组的清晰度门槛（**不是**照片墙下限） |

---

## 11. 测试地图

| 脚本 | 覆盖 | 期望 |
|---|---|---|
| `tests/test_v53.py` | 任务队列、设置、路径变更、聚类、术语 | `PASS=75 FAIL=0` |
| `tests/p0_smoke.py` | 端到端冒烟（合成素材） | `PASS=43 FAIL=0` |
| `tests/test_moved_detect.py` | 移动/改名识别（inode + 指纹） | `PASS=10 FAIL=0` |
| `tests/test_disable_remove.py` | 停用=隐藏、移除=删库 | `PASS=10 FAIL=0` |
| `tests/test_dedup_and_crossroot.py` | 视频去重、跨根移动 | `PASS=7 FAIL=0` |
| `tests/test_heic.py` | HEIC/HEIF 全链路 | `PASS=7 FAIL=0` |
| `tests/test_rerank.py` / `test_face_f2.py` / `test_dedup_*.py` | 重排、人脸 track、去重 | 见各自 `PASS=` |
| `tools/*` | 诊断与基准（只读或临时库） | 见 `tools/README.md` |

---

## 12. 当前状态与未完成项

**已完成**：6 页界面、任务队列、多素材根（停用=隐藏/移除=删库）、移动/改名零重算、
HEIC 支持、视频去重、人脸聚类（多轮抽样，2.5k+ 组）、照片墙新人一览、启动页可选、
方案 B 全速模式、缺失清理批量化（5,338 条/秒）。

**未完成 / 已知限制**：
1. **增量归组**：新脸要重新分组才能进组（可用每版 `centroid.npy` 做快速并入，未实现）
2. `content_hash` 已建列但未参与去重（搬家免重算目前靠 inode/指纹）
3. 视频并发是「帧级并行」，跨视频仍串行
4. 视频镜头已编码覆盖率受处理进度限制（抽帧 ≠ 编码）
5. P@20 人工标注评估未做（缺标注集）

---

## 13. 提交规范

```powershell
.venv\Scripts\python.exe tests\test_v53.py        # 必须 PASS
git add -A
git commit -m "fix(ui): 停用素材根后照片墙残留分组" -m "原因…；修法…；验证…"
```

* 提交信息用中文，`type(scope): 摘要` 格式（type: fix/feat/perf/docs/refactor/test）
* 正文写「**原因 / 修法 / 验证**」三段，便于回溯
* **不要**提交 `data/ weight/ logs/ tmp/ .venv/ config.local.yaml`
* 大改前先 `git status` 确认没有把 `data/library.db` 加进去
