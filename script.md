# 常用功能与命令

> 本文件是「我该敲什么」的速查表。所有命令都在项目根目录执行：
> `D:\DATA\PROJECT\建设多媒体整理系统`
> Python 一律用项目自带的：`.venv\Scripts\python.exe`（不要用系统 python）。

---

## 0. 一分钟上手

```powershell
# 启动界面（会自动拉起缩略图服务）
.\start.bat
# 浏览器打开 http://127.0.0.1:8501
```

界面里已经能做完整流程：**扫描 → 抽帧 → 编码 → 人脸 → 照片墙 → 搜索 → 分组**。
下面的命令行只是同一个东西的另一种入口，以及界面装不下的运维动作。

---

## 1. 界面功能地图（7 个页面）

| 页面 | 干什么 | 对应命令 |
|---|---|---|
| 🖼 照片墙 | 按人看人脸；概览/平铺、排序、质量与日期筛选、导出 | — |
| 👥 自动分组 | 按人归类；低置信度待审核；版本回退 | `16_cluster.py` |
| 🔎 搜索 | 文字找画面、传图找画面、按人脸找同款；可调重排序 | — |
| 📋 任务 | 当前任务进度 + 历史记录（保留 30 天） | `run_task.py` |
| ⚙️ 设置 | 常用（L1）+ 高级（L2）全部可视化，写 `config.local.yaml` | — |
| 🛠 维护 | 完整性检查、缺失文件恢复/清理、异常清单、日志、备份、灵敏度标定 | `18_reconcile.py` `17_gold.py` |
| ℹ️ 关于 | 版本、库统计、磁盘占用、自动探测的数据结构、模型清单 | — |

**左侧「开始」里的三个按钮就是主流程：**

1. **扫描新文件** —— 只处理上次扫描后新增/变化的文件（勾选「扫描后自动接着处理」= 一路跑到人脸，界面才会真的出现新照片）
2. **重新扫描全部** —— 把已完成的全重做一遍（抽帧+编码+人脸，数小时起，有二次确认）
3. **人脸检测与编码** —— 只补人脸这一步

---

## 2. 命令行常用命令

### 2.1 主流程

```powershell
# 扫描文件清单（只更新清单，不抽帧）
.venv\Scripts\python.exe 01_manifest.py --root "E:/"
.venv\Scripts\python.exe 01_manifest.py --root "E:/" --dry-run      # 只看会发生什么
.venv\Scripts\python.exe 01_manifest.py --root "E:/" --reprocess-all # = 界面「重新扫描全部」

# 抽帧（照片侧一趟读盘同时做 EXIF + 去重 + 缓存图）
.venv\Scripts\python.exe 03_extract.py --kind photo
.venv\Scripts\python.exe 03_extract.py --kind video
.venv\Scripts\python.exe 03_extract.py --kind all

# 画面向量编码（写 Qdrant）+ 人脸
.venv\Scripts\python.exe 04_encode.py
.venv\Scripts\python.exe 06_face_detect.py
.venv\Scripts\python.exe 07_face_encode.py

# 一条命令跑完整条链（每步一个独立进程，显存随步骤释放）
.venv\Scripts\python.exe run_task.py --list                    # 看最近任务
.venv\Scripts\python.exe run_task.py <task_id>                 # 重新执行某条任务
```

冒烟测试（任何阶段都支持）：

```powershell
.venv\Scripts\python.exe 03_extract.py --limit 50 --dry-run
.venv\Scripts\python.exe 06_face_detect.py --limit 20 --json
```

### 2.2 分组 / 标定 / 检查

```powershell
# 自动分组：先试跑量内存与耗时，再全量
.venv\Scripts\python.exe 16_cluster.py --trial 100000
.venv\Scripts\python.exe 16_cluster.py

# 用你自己的照片集标定识别灵敏度（一个子文件夹一个人）
.venv\Scripts\python.exe 17_gold.py --root "D:/金标准集"

# 库完整性检查（只出报告，不删东西）
.venv\Scripts\python.exe 18_reconcile.py
```

报告位置：

- `data\reports\reconcile_<时间>.json` —— 孤儿向量 / 悬空引用 / 内容漂移
- `data\gold\result_<时间>.json` + `false_positives.csv` —— PR 曲线数据与建议阈值
- `data\clusters\v<时间>\` —— `assignments.parquet` / `centroid.npy` / `config.json`

### 2.3 搜索（命令行，不经过界面）

```powershell
.venv\Scripts\python.exe 05_rerank.py --self-test          # 环境自检
.venv\Scripts\python.exe 05_rerank.py --text "健身房深蹲的人"
.venv\Scripts\python.exe 05_rerank.py --image "D:/q.jpg"
.venv\Scripts\python.exe 05_rerank.py --benchmark 20       # 测耗时
.venv\Scripts\python.exe 08_search.py --help
```

### 2.4 状态与诊断

```powershell
# 全库状态（按类型/阶段/视频进度/人脸/编码）
.venv\Scripts\python.exe 12_maintain.py status
.venv\Scripts\python.exe 12_maintain.py status --no-disk   # 跳过磁盘统计，秒出

# 还有多少活没干（与流水线完全同一口径）
.venv\Scripts\python.exe tmp\pending.py
.venv\Scripts\python.exe tmp\pending.py --verbose

# 界面能不能渲染（白屏排查第一步）
.venv\Scripts\python.exe tmp\apptest_pages.py
.venv\Scripts\python.exe tmp\apptest_pages.py settings     # 只测某一页

# 规格 §16 执行前检查清单
.venv\Scripts\python.exe tmp\precheck_v5.py

# 数据库 schema / 向量点数核对
.venv\Scripts\python.exe tmp\probe_schema.py
```

### 2.5 维护与修复

```powershell
# 把 status 与 file_encoding 对齐（误标 missing / 状态错乱后必跑）
.venv\Scripts\python.exe 12_maintain.py fix-status

# 只修阶段状态（不清数据）
.venv\Scripts\python.exe 12_maintain.py repair

# 清理 30 天前的任务历史与事件
.venv\Scripts\python.exe 12_maintain.py purge-history

# 备份数据库（等价于界面「维护 → 立即备份」）
Copy-Item data\library.db "data\backup\library_$(Get-Date -f yyyyMMdd_HHmmss).db"
```

---

## 3. 换硬盘 / 改素材目录

**不要直接改 config.yaml 里的 media_root 就重扫** —— 那会把另一边的整个库标成「缺失」。

正确做法（界面）：

1. `设置 → 更改素材文件夹`
2. 填新路径 → **① 预览差异**（会逐个核对新位置上的文件）
3. 看清「新位置找不到 N 个」是不是 0
4. 勾选确认 → **④ 执行更新**
5. 完事后点「扫描新文件」，把新盘上多出来的文件也入库

命令行等价物（脚本方式，慎用）：先 `tmp\precheck_v5.py` 记录现状，再用界面向导。

> 原理：`files.rel_path` 是相对素材根的路径，根一换只需换 `path` 前缀，
> 向量、人脸、分组关系一条都不用重算 —— 所以这个过程是分钟级，不是小时级。

---

## 4. 设置写在哪

| 文件 | 作用 |
|---|---|
| `config.yaml` | 出厂默认值 + 全部注释。**不要手工改它来调功能** |
| `config.local.yaml` | 界面「设置」写回的覆盖层（程序生成，可删） |
| `data\library.db` | 权威数据：文件、镜头、人脸、任务、审计 |
| `data\nvme\` | 中间文件：关键帧、缩略图、人脸小图、照片缓存 |
| `logs\` | 每天一个日志文件 + 每个任务的独立日志 `task_<id>.log` |

改坏了想恢复默认：界面上「恢复默认」（= 删掉 `config.local.yaml` 里的键）。

---

## 5. 常见问题

| 现象 | 先做什么 |
|---|---|
| 界面白屏 / 一直转圈 | 看 `logs\<今天>.log` 有没有 `界面渲染完成` 那行；没有就 `tmp\apptest_pages.py` |
| 点了扫描没反应 | 看左侧「当前任务」是否有进度；`12_maintain.py status` 看 pending 有没有变 |
| 照片墙空 | 是否只跑了扫描没跑「人脸检测与编码」；或「清晰度 ≥」调太高 |
| `database is locked` | 有另一个写进程在跑；等它结束。界面已自动排队，不会再撞 |
| 搜索结果为空 | 「识别灵敏度」是不是太高；这套模型相似度只在 0.31~0.50 之间 |
| 某个视频老是失败 | 已达重试上限进入待处理异常（维护页可看原因）；`--limit` 单独复现 |
| 换了 root 之后全库变「缺失」 | 改回原 root 重扫 → `12_maintain.py fix-status`（详见 README「换扫描根目录的坑」） |

**结构性约束（记住两条就够）：**

1. **可以登记多个素材文件夹**（设置 → 素材文件夹：增 / 删 / 改 / 停用 / 单独扫描）。
   删目录默认只移除登记，不删任何数据；「改路径」是把已有记录整体搬家，和「加一个目录」是两件事。
2. **扫描只更新清单**。抽帧/编码/人脸是另外三步；界面上的「扫描后自动接着处理」就是帮你连着跑。

---

## 6. 目录结构

```
建设多媒体整理系统/
  00_bootstrap.py        环境自检 / 依赖安装
  01_manifest.py         扫描文件清单
  02_dedup.py            pHash 去重（照片融合模式已内置，通常不用单独跑）
  03_extract.py          抽帧 + EXIF + 缓存图
  04_encode.py           画面向量编码
  05_rerank.py           重排序 / 检索 A-B 工具
  06_face_detect.py      人脸检测
  07_face_encode.py      人脸向量 + 缩略图
  08_search.py           命令行检索
  11_export.py           导出
  12_maintain.py         状态 / 修复 / 清理
  16_cluster.py          人脸自动分组
  17_gold.py             识别灵敏度标定
  18_reconcile.py        库完整性检查
  run_task.py            任务执行器（界面按钮背后的东西）
  app.py                 界面（Streamlit）
  start.bat              一键启动
  src/                   实现（manifest/extract/encode/face_pipeline/cluster/search/…）
  tmp/                   诊断脚本与一次性工具（可随时删，不影响主流程）
  tests/                 回归测试
```

---

## 7. 直接操作数据库（SQLite）

库文件：`data\library.db`（WAL 模式）。**所有写操作都要经过脚本**，手工只做「查」和「明确的修复」。

### 7.0 三条硬规矩

1. **查：只用只读连接。** 流水线在跑时库上有写事务，只读连接不会被阻塞；反过来，一个手工的写事务可能把抽帧/编码堵死（`database is locked`）。
2. **改：先备份。** 821 MB 的库复制一份只要 1~2 秒，出错了能立刻退回。
3. **绝不手工 `DELETE` / 改 `status` 绕过流程。** 状态不是给人事后编辑的标签，它决定「下一步取哪些文件」：
   - `missing` = 软件标记（原文件不在磁盘上），**不是**删除；恢复要重新扫描或 `12_maintain.py fix-status`
   - `skipped_dup` = 去重结论，两条模型记录各一行（铁律 1）
   - `done` = 该阶段已完成，手工改成 `pending` 会让它整段重做（这是「重新扫描全部」做的事，不要手改）

### 7.1 怎么连（不要用 sqlite3 命令行做长事务）

```powershell
# 交互式查询（推荐：只读打开，语法高亮无所谓，安全第一）
.venv\Scripts\python.exe -c "import sqlite3;c=sqlite3.connect('file:data/library.db?mode=ro',uri=True);[print(r) for r in c.execute('SELECT status,COUNT(*) FROM files GROUP BY status')]"
```

长一点的查询建议写成临时脚本，别在命令行里拼引号（Windows 的引号转义很容易出错）。项目里已有可直接用的：

```powershell
.venv\Scripts\python.exe tmp\pending.py            # 还剩多少活（与流水线同口径）
.venv\Scripts\python.exe 12_maintain.py status      # 全库状态
.venv\Scripts\python.exe tmp\probe_schema.py        # 表结构 / 行数 / 向量点数核对
.venv\Scripts\python.exe tmp\show_roots.py          # 素材目录登记表 + 各根文件数
.venv\Scripts\python.exe tmp\diag_stuck_task.py     # 任务卡住时查根因
```

### 7.2 常用查询（复制即用）

```sql
-- 文件状态总览
SELECT status, COUNT(*) FROM files GROUP BY status ORDER BY 2 DESC;

-- 按类型 × 状态（照片/视频各处理到哪了）
SELECT kind, status, COUNT(*) FROM files GROUP BY kind, status ORDER BY 1,2;

-- 还剩多少待抽帧（与 03_extract 的取数条件一致：排除死信）
SELECT f.kind, COUNT(*) FROM files f
  LEFT JOIN stage_status ss ON ss.file_id=f.file_id AND ss.stage='extract'
 WHERE f.status IN ('pending','error')
   AND (ss.status IS NULL OR ss.status IN ('pending','error'))
   AND COALESCE((SELECT COUNT(*) FROM dead_letter dl
                  WHERE dl.file_id=f.file_id AND dl.stage='extract'),0) < 3
 GROUP BY f.kind;

-- 各阶段完成情况
SELECT stage, status, COUNT(*) FROM stage_status GROUP BY stage, status;

-- 编码情况（两个模型分开看）
SELECT model, status, COUNT(*) FROM file_encoding GROUP BY model, status;

-- 某个文件的全貌（换成要查的 file_id）
SELECT * FROM files WHERE file_id=12345;
SELECT * FROM shots WHERE file_id=12345 ORDER BY shot_ix;
SELECT shot_ix, face_id, quality, status FROM face_records WHERE file_id=12345;
SELECT * FROM stage_status WHERE file_id=12345;
SELECT * FROM file_encoding WHERE file_id=12345;

-- 按路径找（中文路径直接用，SQLite 里是 UTF-8）
SELECT file_id, status, kind FROM files WHERE path LIKE '%关键字%' LIMIT 20;

-- 每个素材根有多少文件（多目录）
SELECT root, COUNT(*) FROM files GROUP BY root;

-- 失败清单（含原因）
SELECT f.file_id, f.path, e.model, e.error FROM file_encoding e
  JOIN files f ON f.file_id=e.file_id
 WHERE e.status='error' LIMIT 50;

-- 反复失败、已放弃重试的文件
SELECT f.file_id, f.path, dl.stage, dl.attempt, dl.error FROM dead_letter dl
  JOIN files f ON f.file_id=dl.file_id ORDER BY dl.at DESC LIMIT 50;

-- 人脸：可检索 / 待编码 / 已隐藏
SELECT status, has_vector, COUNT(*) FROM face_records GROUP BY status, has_vector;

-- 任务队列（界面看到的进度就是这张表）
SELECT task_id, title, state, stage, done, total, updated_at FROM tasks
 ORDER BY created_at DESC LIMIT 10;

-- 最近的数据变更事件（界面缓存就靠它失效）
SELECT event_id, at, kind FROM task_events ORDER BY event_id DESC LIMIT 20;

-- 设置修改审计（旧值→新值）
SELECT at, who, what, before, after FROM audit_log
 WHERE what LIKE 'settings%' ORDER BY at DESC LIMIT 20;
```

### 7.3 修复类操作（都有现成脚本，别手写 SQL）

| 症状 | 命令 | 说明 |
|---|---|---|
| 状态与 `file_encoding` 对不上 | `12_maintain.py fix-status` | 从编码结论还原 `done`/`skipped_dup`，**不碰 `missing`** |
| 抽帧卡在某个阶段没进展 | `12_maintain.py repair` | 按阶段状态恢复 `files.status` |
| 换过扫描根，库被标成大量缺失 | 先改回原根扫一遍 → `fix-status` | 见 README「换扫描根目录的坑」 |
| `files.root` 前缀不一致（界面显示 0 文件） | `tmp\fix_root_key2.py --apply` | 统一成带结尾分隔符的写法 |
| 只有路径变了、内容没变 | 设置 → 素材文件夹 → 修改路径… | 走向导，`rel_path` 不动，向量零重算 |
| 缺失文件的记录想彻底清掉 | 维护 → 已删除（可恢复）→ 生成清理报告 → 确认删除 | 只删记录与向量，**磁盘文件不动** |
| 任务永远停在「进行中」 | `tmp\diag_stuck_task.py` 看进程；`tmp\finish_restore.py --apply` 或在界面等 5 分钟自动回收 | 进程被强杀后心跳会停，5 分钟判定失败 |

### 7.4 备份与恢复

```powershell
# 备份（等价于界面「维护 → 立即备份」）
Copy-Item data\library.db "data\backup\library_$(Get-Date -f yyyyMMdd_HHmmss).db"

# 恢复：先停界面和所有流水线进程，再覆盖回去
.\start.bat  # 关掉
Copy-Item data\backup\library_20261019_202359.db data\library.db -Force
Remove-Item data\library.db-wal, data\library.db-shm -ErrorAction SilentlyContinue
```

> **备份只包含元数据。** 向量在 Qdrant（`127.0.0.1:7333`），中间图在 `data\nvme\`。
> 换机器要一起搬的还有：`data\nvme\`（可选，能重建）、`config.local.yaml`、`weight\`。

### 7.5 一致性自查（和规格 §16 口径一致）

```powershell
.venv\Scripts\python.exe 18_reconcile.py     # 孤儿向量 / 悬空引用 / 内容漂移，只出报告
```

判读要点（本项目实测基线）：
- 语义向量点数 ≈ 镜头数（**一个镜头一个点**，不是「一组镜头一个点」）
- 人脸向量点数 == `face_records` 里 `status='alive'` 的行数
- 差值应为 0；负数几十条属正常（全黑/极小帧的镜头本来就不写向量）
