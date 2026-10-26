# tools/ · 诊断与基准脚本

> 这些脚本**不参与业务**，只用来"看清楚"。
> 约定：**默认只读**；需要写库或杀进程的，文件名与说明里都标了 ⚠️。
> 全部在项目根目录执行，Python 用 `.venv\Scripts\python.exe`。

```powershell
$env:PYTHONUTF8='1'; $env:PYTHONIOENCODING='utf-8'
.venv\Scripts\python.exe tools\status.py
```

---

## 日常三件套

| 脚本 | 用途 | 输出 |
|---|---|---|
| `status.py` | 当前任务 / 阶段 / 进度 / 速度 / pid 是否存活；没有任务时列最近 3 条 | 十几行文本 |
| `check_heartbeat.py` | 判断「被判心跳超时」的任务是**真死**还是**误判**（对比 pid 是否还是那条流水线） | 任务表 + 活进程 + 交叉比对 |
| `check_pages.py` | 用 AppTest 逐页确认 6 个页面都能渲染、路由正确 | `nav=wall → OK` 之类 |

---

## 数据一致性 / 可见性

| 脚本 | 检查什么 | 何时用 |
|---|---|---|
| `check_paths.py` | 路径体检：人脸/文件/向量/素材根/重复组 的 10 类问题（含磁盘抽样） | 怀疑"地址不对/文件不存在/移动没跟上" |
| `check_disabled_hidden.py` | 停用素材根后，照片墙分组、封面、弹窗成员、搜索候选集 是否都已隐藏 | 停用/启用目录后 |
| `check_disabled_search.py` | 走真实 UI 路径跑一次检索，断言命中不含停用目录（会加载模型，约 15 秒） | 同上，想验证搜索侧 |
| `check_grouping.py` | 分组门槛分析：quality 分布、被门槛挡掉多少、版本覆盖、可见组数 | "为什么这么多脸没归组" |

---

## 功能体检

| 脚本 | 检查什么 |
|---|---|
| `check_video_search.py` | 视频检索全链路：镜头/关键帧/向量/自检索 top1/静态服务/人脸向量/`enrich` 字段 |
| `check_perf_mode.py` | `performance.mode` 是否真的改变行为（prefetch/worker/批大小） |

---

## 基准（会在临时库上跑，不碰真实数据）

| 脚本 | 测什么 | 参考结果 |
|---|---|---|
| `bench_video_parallel.py` | 同一视频帧并行度 1 vs 8 | 7.2GB/148 镜头：429s → 67s |
| `bench_purge.py` | 批量清理 1,500 文件 / 7,500 向量（隔离 collection） | 0.28 秒（5,338 条/秒） |

> 两个基准都会自建临时 collection（`bench_faces` / `bench_semantic`），跑完自动删除。

---

## ⚠️ 会改状态的脚本

| 脚本 | 干什么 | 风险 |
|---|---|---|
| `restart_task.py` | 停掉当前任务（含 `taskkill /T /F`）→ 重置被 bug 打坏的文件 → 重新起一次扫描 | **会杀进程**。仅在没有别的任务在跑、且你确认要重来的时候用 |

---

## 写新诊断脚本的约定

1. 文件名 `check_*.py`（只读）或 `bench_*.py`（基准）或 `fix_*.py`（会改数据，必须支持 `--apply`）
2. 用 `ROOT = Path(__file__).resolve().parent.parent` + `sys.path.insert(0, str(ROOT))`
3. 优先用 `src/` 里的函数（`load_config()`、`db.connect()`、`tasks.*`），别自己拼 SQL 连接串
4. **不要**把临时脚本留在 `tmp/` 里当交付物 —— `tmp/` 不进 git；值得留的复制到 `tools/` 并在本文件登记
