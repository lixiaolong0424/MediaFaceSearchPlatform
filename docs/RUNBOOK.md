# 运维手册（docs/RUNBOOK.md）

> 目标：出问题时**按症状直接翻到对应小节**，不用读代码。
> 全部命令在项目根目录执行，Python 一律 `.venv\Scripts\python.exe`。

---

## 0. 一分钟健康检查

```powershell
$env:PYTHONUTF8='1'
.venv\Scripts\python.exe 00_bootstrap.py           # 自检：21 必过项应全过
.venv\Scripts\python.exe tools\status.py           # 任务状态 / 进度 / 速度
.venv\Scripts\python.exe 12_maintain.py status     # 库统计（含缺失/失败）
.venv\Scripts\python.exe tests\test_v53.py         # 核心回归
```

界面：<http://127.0.0.1:8501>（`.\start.bat` 启动；静态服务 18501）

---

## 1. 日常操作

| 想做什么 | 怎么做 |
|---|---|
| 启动界面 | 双击 `start.bat`（已在跑时它只会打开浏览器并退出，这是正常的） |
| 停界面 | 关掉 `start.bat` 黑窗口（⚠️ **会连带杀掉正在跑的任务**） |
| 新增/改动素材后入库 | 界面 → 开始 → 「扫描新文件」（勾「扫描后自动接着处理」= 跑完整 5 步） |
| 只看清单不抽帧 | 取消勾选「扫描后自动接着处理」 |
| 停止正在跑的任务 | 任务页「停止任务」（3 秒内进程退则立即停，否则 12 秒看门狗强杀） |
| 重新分组 | 照片墙 → 「重新分组」→ 确认（会产出新版本、自动切换、旧版保留） |
| 回退分组版本 | 照片墙 → 「分组版本与回退」→ 选旧版 |
| 清理「已删除（可恢复）」 | 维护 → 已删除 → 勾选/全选 → 「删除记录与向量」（磁盘文件不动） |
| 停用某目录（不显示/不搜索） | 设置 → 素材文件夹 → 选中 → 「停用（不显示·不搜索）」 |
| 从库中彻底删除某目录 | 设置 → 素材文件夹 → 「移除…」→ 确认（默认删记录与向量） |
| 备份 | 维护 → 「立即备份」（SQLite 在线备份）或见 §6 |

---

## 2. 症状 → 排查 → 处置

### 2.1 任务按钮点了没反应
1. 看侧边栏「当前任务」是否有任务在跑（只允许一个）
2. `.venv\Scripts\python.exe tools\status.py` 看 `活动任务` 与 `僵尸`
3. 若提示「上一次任务的进程其实还在跑」但 pid 明显是别的程序（PID 复用）：
   ```powershell
   .venv\Scripts\python.exe -c "import sys;sys.path.insert(0,'.');from src import tasks as T;from src.common import load_config;cfg=load_config();T.init(cfg);print(T.clear_reused_pids(cfg))"
   ```

### 2.2 任务卡在「正在停止…」
```powershell
.venv\Scripts\python.exe tools\status.py            # 看 pid 是否存活
# 存活但不动 → 手动收尾（会强制结束进程树）
.venv\Scripts\python.exe tools\recover_and_scan.py  # 停止旧任务并重跑扫描（谨慎）
```

### 2.3 扫描跑了但照片墙没变化
1. 确认勾了「扫描后自动接着处理」：不勾只更新清单，不抽帧/编码/检脸
2. 看 `tools\status.py` 是否在「第 4/6 步 人脸检测」之后停下
3. 新增的人脸**不会自动进组**：照片墙「按人浏览」只反映最近一次分组
   → 点「重新分组」，或先在「看看这些新脸」里确认新脸已入库

### 2.4 照片墙某个组点开报「文件不存在」
```powershell
.venv\Scripts\python.exe tools\check_disabled_hidden.py   # 组/封面是否指向已停用/缺失文件
```
必要时重算 meta：`cluster.refresh_meta_for_root(cfg, "<root>")`

### 2.5 停用目录后照片墙/搜索还能看到
已修（2026-10-26），自查：
```powershell
.venv\Scripts\python.exe tools\check_disabled_hidden.py
```

### 2.6 搜索搜不到东西
1. 看库统计：`.venv\Scripts\python.exe 12_maintain.py status | Select-String 已完成`
2. 视频只抽帧没编码 → 命中率低是正常的；跑「扫描新文件」把它编码
3. 阈值：搜索页「识别灵敏度」0.30 起（规格原值 0.55 在本库基本搜不到）
4. Qdrant 健康：`curl http://127.0.0.1:7333/collections`

### 2.7 自动分组崩溃（退出码 3221225725）
= 栈溢出。已修（`.venv\Scripts\python.exe tests\test_cluster_sampling.py` 可自检）。
若仍复现：把 `cluster.max_fit_faces` 调小（如 50000）再重新分组。

### 2.8 分组「没参与的人数」很多
两个不同门槛，别搞混：
* `faces.quality_min_show`：照片墙**显示**下限（不影响分组）
* `cluster.quality_gate`：**参与分组**的清晰度下限（默认 0.30）
改了后者必须**重新分组**；查当前被挡掉多少：
```powershell
.venv\Scripts\python.exe tools\check_grouping.py
```

### 2.9 界面白屏 / 打不开
1. `.\start.bat` 会先做端口守卫：若 8501 已有本系统在响应，它只开浏览器
2. 浏览器按 `F5`（界面重启后旧标签页会白屏）
3. 仍不行：关掉黑窗口重新双击；看 `logs\2026-xx-xx.log` 尾部

### 2.10 磁盘告急
```powershell
.venv\Scripts\python.exe 12_maintain.py quota            # 配额与占用
.venv\Scripts\python.exe 12_maintain.py clean-frames --days 30   # 清旧关键帧
.venv\Scripts\python.exe 12_maintain.py prune-orphans --confirm  # 清无主图
```

---

## 3. 命令行流水线（不经界面）

```powershell
# 单步执行（每步都是独立进程，可单独重跑）
.venv\Scripts\python.exe 01_manifest.py --root "E:/"
.venv\Scripts\python.exe 03_extract.py --kind all
.venv\Scripts\python.exe 04_encode.py
.venv\Scripts\python.exe 06_face_detect.py
.venv\Scripts\python.exe 07_face_encode.py

# 走任务队列（会写 tasks/事件，界面能看到）
.venv\Scripts\python.exe run_task.py --list
.venv\Scripts\python.exe run_task.py <task_id>

# 分组 / 标定 / 核对
.venv\Scripts\python.exe 16_cluster.py
.venv\Scripts\python.exe 17_gold.py --help
.venv\Scripts\python.exe 18_reconcile.py
```

> ⚠️ 命令行直接跑阶段脚本**不会**写 `task_events`（界面缓存最多陈旧 180 秒），
> 但 `01_manifest.py` 现在会写一条 `library_changed`（2026-10-22 补）。

---

## 4. 数据库运维

```powershell
.venv\Scripts\python.exe 12_maintain.py status          # 统计
.venv\Scripts\python.exe 12_maintain.py repair          # 修状态不一致
.venv\Scripts\python.exe 12_maintain.py reconcile       # 元数据 vs 向量核对
.venv\Scripts\python.exe 12_maintain.py purge-missing   # 报告「缺失」清理量
.venv\Scripts\python.exe 12_maintain.py purge-missing --apply   # 真删（磁盘不动）
.venv\Scripts\python.exe 12_maintain.py fix-status      # 重算 status
.venv\Scripts\python.exe 12_maintain.py rotate-logs --days 14
```

**大批量删记录**走 `purge-missing`：Qdrant 用点 ID 批量删 + SQLite 按 500 条一批 + 事务重试，
实测约 5,000 条/秒（1,500 条 0.28 秒）。

### 迁移
* 迁移文件在 `migrations/`，只增不改；`db.ensure_schema()` 在启动与测试时自动执行
* 当前 schema **v8**
* 手工查看版本：`sqlite3 data\library.db "SELECT * FROM db_version"`

---

## 5. 性能调优

界面：设置 → 性能 → 「全速模式」一键切换（照片预读 6 / 帧并行 8 / 视频并发 12 / 编码批 32）。

| 现象 | 该调什么 |
|---|---|
| 照片抽帧慢（< 10 张/秒） | `performance.photo_prefetch`（4~8） |
| 视频一个要几十秒 | `performance.frame_workers`（4~8）；超长视频靠 `video.scdet_max_duration_sec` |
| GPU 空转（编码慢） | `performance.encode_batch` 32/64、`encode_group` 12/16 |
| 界面卡顿 | 关掉全速模式；`performance.video_workers` 降回 4~6 |
| 分组太慢 | `cluster.max_fit_faces` 降到 6~8 万 |

实测基线（供回归对比）：
* 照片融合抽帧 3000×2000：预读 0 → 13.9 张/秒；预读 6 → **24.6 张/秒**
* 视频 7.2 GB / 148 镜头：帧并行 1 → 429 秒；并行 8 → **67 秒**
* 画面编码 batch=32：约 10~15 文件/秒（3090）
* 人脸检测约 80 文件/秒；人脸编码约 41 文件/秒
* 分组 31.5 万张脸：UMAP 146 秒 + 多轮 HDBSCAN 约 70 秒，总计约 6 分钟

---

## 6. 备份与恢复

```powershell
# 在线备份（不用停服务）
.venv\Scripts\python.exe 12_maintain.py backup
# 或直接拷（先确认没有任务在跑）
Copy-Item data\library.db D:\backup\library-$(Get-Date -f yyyyMMdd-HHmm).db
```

恢复：停界面 → 用备份覆盖 `data\library.db`（同时删 `-wal`/`-shm`）→ 启动 → 自检。
分组产物在 `data/clusters/<ver>/`，与 DB 一起备份才能完整回退版本。

---

## 7. 日志位置

| 文件 | 内容 |
|---|---|
| `logs/YYYY-MM-DD.log` | 全局日志（各阶段、界面渲染） |
| `logs/task_<task_id>.log` | 某个任务的完整输出（含异常栈） |
| `logs/task_<task_id>.launcher.log` | 执行器启动日志 |
| `logs/pipeline.log` | 任务队列相关 |

排查顺序：`tools\status.py` → `logs\task_<id>.log` 尾部 → `logs\YYYY-MM-DD.log`。

---

## 8. 危险操作清单（做之前先备份）

1. 设置 → 素材文件夹 → **移除…**（默认会把该目录的记录与向量从库里删掉）
2. 维护 → 已删除 → **删除记录与向量**（不可撤销）
3. 设置 → 路径变更向导（会 UPDATE 全库 path；历史上误用过一次，已加 20% 阈值拦截）
4. `git checkout -- .` / `git reset --hard`（会丢掉未提交的改动）
5. 强杀 `03_extract` 等进程（正常停止请用任务页按钮；强杀最多丢当前 1 个文件的进度）
