# 素材人脸检索系统 · 实施说明与验收手册

## 🤖 AI Agent 先读这里

| 想知道什么 | 看哪个文件 |
|---|---|
| **怎么安全地改这个项目**（铁律 / 不变量 / 已知陷阱 / 测试地图） | `AGENTS.md` |
| 系统搭起来的形状（阶段 / 表 / 向量 id / 并发模型） | `docs/ARCHITECTURE.md` |
| 出问题了怎么查（症状 → 排查 → 处置） | `docs/RUNBOOK.md` |
| 改了什么、为什么改 | `docs/CHANGELOG.md` |
| 我要敲什么命令 | `script.md` |
| 诊断/基准脚本 | `tools/README.md` |

```powershell
$env:PYTHONUTF8='1'
.venv\Scripts\python.exe tests\test_v53.py      # 核心回归，应 PASS=75 FAIL=0
.venv\Scripts\python.exe tools\status.py        # 当前任务/进度
```

> **交付物**：可安装、可扫描、可检索、可导出、可验收的本地工具（P0 阶段）
> **规格原文**：`素材人脸检索系统_完整实施规格v3_Windows版.docx`
> **需求讨论全文**：`多媒体数据查找方案-20261015085922 (2).txt`
> **P0 验收结果**：`43/43 通过`（`tests\p0_smoke.py`），`00_bootstrap.py` 必过项 `21/21` 通过

---

## ⚠️ 先读这两条

### 1) 必须用 venv 里的 python

你机器上的 `python` 指向**全局 Python 3.10**（`D:\PROGRAM\1_Python\Python310`），
它**没有**本项目需要的依赖。所有命令都必须走项目虚拟环境：

```powershell
cd "D:\DATA\PROJECT\建设多媒体整理系统"

# 推荐：显式写 venv 路径（任何终端、任何目录都不会跑错）
.venv\Scripts\python.exe tests\p0_smoke.py
.venv\Scripts\python.exe 00_bootstrap.py

# 或者：先激活 venv，之后就能直接用 python
.venv\Scripts\Activate.ps1
python tests\p0_smoke.py
```

**如果用裸 `python` 跑会怎样**：脚本会在开头做前置检查，
打印「缺失依赖 + 正确的命令」并**以退出码 2 立即中止**，
或者在不依赖 torch 的阶段跑完后明确告诉你「有 4 项被跳过、这不是完整验收」。

> 历史坑（已修）：早期版本在依赖不全时会静默回退到全局 python，
> 然后因为 Windows 控制台是 GBK 编码、无法打印 ffmpeg stderr 里的 U+FFFD 字符，
> 崩在 `UnicodeEncodeError: 'gbk' codec can't encode character '\ufffd'` ——
> **用一个与被测功能完全无关的异常掩盖了「跑错解释器」这个真正原因**。
> 现在父/子进程都强制 UTF-8（`errors="replace"`），并且开跑前就检查解释器。

### 2) `start.bat` 必须是 CRLF 行尾（已修复，勿用编辑器保存为 LF）

`cmd.exe` **只能解析 CRLF 行尾的批处理文件**。LF-only 的 `.bat` 会让 cmd 把多行粘成一行，
双击后表现为「窗口一闪而过」或刷出一串
`'XXX' is not recognized as an internal or external command`。

> 历史坑（已修）：`start.bat` 最初被写成 LF-only（CR=0, LF=98），
> 双击直接崩，报出 `'ROOT' is not recognized`、`'12' is not recognized` 之类的乱码错误。
> 现由 `tmp\write_start_bat.py` 以字节级 CRLF 写入并回读校验（CR 数 == LF 数、无 BOM）。
> **如果你用编辑器改过 `start.bat`，请确认行尾仍是 CRLF，或重跑该脚本。**

`start.bat` 的三段式流程（顺序有讲究）：

```
[1/3] 启动前准备  src\startup_prep.py
      · 幂等创建 ~\.streamlit\credentials.toml（否则首次运行会停在交互式 "Email:" 提示）
      · 端口守卫：若 8501 已被监听 → 判定「已在运行」，直接开浏览器后退出（不再重复启动）
[2/3] 启动自检    00_bootstrap.py（任一项 FAIL 中止）
[3/3] 检查 Qdrant 容器 → 启动 Streamlit（http://127.0.0.1:8501）
```

「启动前准备」**必须排在自检之前**：重复双击时端口当然被占，那是「已经在运行」而不是故障。
自检里的端口检查也已区分这两种情况——自己的服务在响应就报 **WARN + 界面已在运行**，
别的程序占用才报 **FAIL + netstat 排查命令**。

---

## v5.3 新增功能一览（2026-10-19）

对应规格：`素材人脸检索系统_新增功能实施规格v5_3.docx`（提取版见 `tmp/spec_v5_3.md`）。
**常用命令速查表在 `script.md`。**

### 已交付

| 规格条目 | 落地物 | 验证 |
|---|---|---|
| §2 术语清理（M0） | `src/ui_text.py`（唯一文案字典 + 禁用词表） | `tests/test_v53.py` 第 6 节 |
| §3.2 任务队列（M1） | `src/tasks.py` + `run_task.py` + `migrations/003`（tasks/task_events） | `tests/test_v53.py` 第 1 节 |
| §3.2 UI 反馈 / 防重复点击 / 状态恢复 / 任务历史 | 侧边栏「当前任务」+「任务」页；按钮在任务运行期间禁用 | `tmp/apptest_pages.py tasks` |
| §3.3 设置全部 UI 化（M2） | `src/settings_store.py`（L1/L2 共 31 项）+ `config.local.yaml` + 审计日志 | `tests/test_v53.py` 第 2 节 |
| §3.4 人脸墙实时更新（M3） | `task_events` 表 + `src/ui_actions.py::watch_events`（1.5s 轮询任务、2s 轮询事件） | 见下文「为什么不是 SSE」 |
| §4 路径变更五步向导（M5） | `src/migrate.py` + 设置页向导（策略①保留全部） | `tests/test_v53.py` 第 3 节 |
| §4.3 路径重映射 | 一次 `UPDATE files SET path = 前缀 || rel_path`（rel_path/path_hash 不变） | 同上，断言 rel_path 未变 |
| §5 文件增删改 | 缺失去留：维护页「恢复 / 清理报告 / 删除记录与向量」 | 同上 |
| §6 人脸聚类（M7） | `src/cluster.py` + `16_cluster.py` + 「自动分组」页（版本回退/低置信度筛选） | `tests/test_v53.py` 第 4 节 |
| §7.4 灵敏度标定（M6） | `src/gold.py` + `17_gold.py` | 需你自己的照片集，见 `script.md` |
| §8.2 完整性检查（M8） | `src/reconcile.py` + `18_reconcile.py` | 实测：A=0 B=0 C=0，点数账目 −93（见下） |
| §8.3 幂等唯一索引 + lease | `migrations/003`：`uk_face_vector(file_id,shot_ix,face_ix)`（含 29 万人脸回填）、`files.lease_until` | 迁移在副本上演练通过 |
| §15.5 日志/告警 | `monitor` 配置节 + 维护页错误角标与日志尾巴（webhook 预留） | — |
| §16 执行前检查清单 | `tmp/precheck_v5.py` | 见下「检查清单结果」 |

### 关键诊断：为什么「增量扫描 / 全新扫描没有作用」

规格 §3.1 要求按 A→B→C 顺序定位，实测结论是 **C 为主、B 次之**：

1. **C（任务真的没生效）**：旧界面的两个扫描按钮**都只调用了 `01_manifest.py`**，
   而它只更新文件清单（`files` 表），**不抽帧、不编码、不检脸**。
   `full` 参数甚至被忽略（两个分支传的是同一个命令）。
   所以扫描确实跑了、`files` 行数确实涨了，但照片墙一张都不会变 —— 用户看到的就是「没反应」。
2. **B（前端没刷新）**：扫描完成后没有任何事件通知，人脸墙数字要手动刷新页面才变。
3. A（没反馈）只有一部分：只有一个「扫描进行中…」文字，没有进度、没有阶段、没有剩余时间。

**修复**：扫描按钮现在启动的是**整条链**（清单 → 抽帧 → 画面编码 → 人脸检测 → 人脸编码），
每步一个子进程、进度写库、界面 1.5 秒自动刷新；不想要后续步骤可以取消勾选
「扫描后自动接着处理」（那时它只更新清单，界面会明确说明这一点）。

### 为什么用「事件 + 轮询」而不是 SSE

规格 §3.4 推荐 B 方案（任务完成写事件表 + 前端 SSE/长轮询订阅）。Streamlit 没有 SSE 通道，
所以这里实现的是 **B 的服务端部分（`task_events` 表）+ 客户端 2 秒轮询**，
事件到达后 `< 2s` 内自动失效缓存并整页刷新，满足 §13 验收项 UI-4「3 秒内自动刷新」。
换成自建前端时，`task_events` 表可以原样作为 SSE 的数据源，服务端不用改。

### 记录在案的偏离与取舍

1. **识别灵敏度三档改成 0.40 / 0.30 / 0.22**（规格 §2.2 写的是 0.55/0.40/0.30）。
   本机实测这套模型的相似度**整体挤在 0.31~0.50**，0.55 永远不可能命中 ——
   这正是之前「所有文字搜索都是 0 结果」的真实原因。照抄规格会把功能做死。
2. **`content_hash` 已建列但暂未参与去重**（M4 只完成了 DDL）。
   真正用它做「搬家免重算」需要改入库流程（每个文件多一次哈希 + 命中后复用旧 file_id），
   风险高于本轮范围；路径变更已经靠 `rel_path` 做到了零重算，`content_hash` 留给下一轮
   （届时用于「同一文件出现在两个目录」的去重）。
3. **配图（PR 曲线 PNG）没做**：本机没装绘图库，`17_gold.py` 输出曲线数据（JSON）与
   误报 CSV，不引入新依赖只为一张图。
4. **聚类降维允许 PCA 兜底**：装了 `umap-learn` 就用 UMAP，装不上自动降级 PCA 并在
   `cluster_runs.algo` 里写明用的是哪种。实测 2 万人脸 PCA 全程 10.3 秒。
5. **`merge_tracks` 相关的 `face_ix` 是新增列**：29 万人脸已按 face_id 顺序回填，
   `uk_face_vector` 才建得起来；F1 重跑同一个文件时仍是「先删后插」，语义不变。
6. **`clusters` 全量跑还没执行**：本轮只做了 2 万人脸的试跑（971 组、8157 未分组、
   10.3 秒）。全量 29 万要十几分钟到几小时，需要你确认后我再跑（会写 `cluster_runs`）。

### 检查清单结果（§16）

```
环境   Python 3.12.14 / SQLite 3.53.1 / Qdrant HTTP 200 / 库盘余量 48.3%
一致性 files 511,051 · semantic done 311,096 · faces 291,694
       僵尸 running: 无 ✓
       语义 Qdrant 334,129 = 镜头 334,222 − 93（无效画面镜头本来就不写向量）✓
       人脸 Qdrant 291,694 = face_records 291,694 ✓
仓库   files.lease_until ✓  content_hash ✓（已建列）  uk_face_vector ✓
聚类依赖 numpy/sklearn/pyarrow/torch ✓  umap-learn 0.5.12 ✓
```

结论：**A/B/C 三类异常全部为 0**，元数据与向量库一致。

---

## 0. 先读：与规格的偏离清单（供复核）

规格第〇章要求「遇到规格未覆盖的边界情况，先问我，不要自行决定」。以下是**实现前已与你确认**的偏离项，
以及**实现中发现、经实测证据修正**的问题。全部集中在此，便于逐条复核。

### 0.1 已确认的偏离（开工前）

| # | 规格原文 | 实际实现 | 原因 |
|---|---------|---------|------|
| 1 | 数据库 `state.db`（§4）与 `library.db`（§3/§14）并存 | 统一 **`library.db`** | 你拍板；§3 目录树与 §14 config.yaml 一致，§4 正文为遗留笔误 |
| 2 | `weights/`（复数）+ mklink 映射 | 直接读 **`weight/`**（单数），**不建 mklink** | 你给定的路径 `D:\DATA\PROJECT\建设多媒体整理系统\weight\` **无中文、无空格、不在 OneDrive 下**——mklink 的三个理由全部消失，多一层联接只是多一个故障点 |
| 3 | Qdrant `127.0.0.1:6333` | **`127.0.0.1:7333`** | Windows Hyper-V 保留了 TCP 6313–6412 段（实测 6333/6334 bind 失败：`An attempt was made to access a socket in a way forbidden by its access permissions`）。你拍板改用 7333。修好 winnat 后可原位改回 |
| 4 | `media_root` 默认 `E:\` | 保持 **`E:\`** | 你明确选择「严格 E:\\」。已内置目录黑名单（`$RECYCLE.BIN`、`迅雷云盘`、`BaiduNetdiskDownload`、`115网盘`、`QuarkDownload`、`System Volume Information`、`Recovery`），避免把回收站/网盘缓存当素材扫 |
| 5 | ffmpeg 放 `${project_root}\bin` | **`bin\` 为空，自动回落 PATH** | 本机已有 `D:\PROGRAM\1_ffmpeg-9.0.1-essentials_build\bin`。`00_bootstrap.py` 给出 WARN 并提示复制到 `bin\` |
| 6 | Python >= 3.10（§1.3）/ >= 3.11（对话） | 项目 `.venv` 用 **Python 3.12.14** | 全局 3.10 环境已损坏（实测 `import torch` 直接 AttributeError）。你选择项目内独立 `.venv` |
| 7 | §13 扁平模块 `00_bootstrap.py … 12_maintain.py` 与 §3 目录树 `src/` 并存 | **两层结构**：根目录放规格点名的入口文件（薄壳，只做 CLI），业务逻辑放 `src/` | 同时满足两处显式要求；也避免 Python 包内出现数字开头的模块名 |
| 8 | §4 `files` 表未含时长/分辨率 | 增加 `duration_sec / width / height / rel_path`；迁移 `002` 再增 `last_hit_at / hit_count` | 排序、展示与 frames LRU 需要。迁移纪律是「只增不改」，新增列合规 |
| 9 | 语义向量粒度未明说「帧级 / 镜头级」 | **镜头级**：一个 shot 的关键帧向量各自编码后**平均并重新归一化** | 与 L4「时序一致性聚类 / shot 级去重」颗粒度对齐，并把 60 万关键帧量级压到镜头量级；这是唯一与 `point_id=(file_id<<20)\|shot_ix` 命名空间自洽的选择 |
| 10 | `05_rerank.py` / `06_face_detect.py` / `07_face_encode.py` / `09_ui_wall.py` / `10_ui_search.py` | **已全部补齐**：`06_face_detect.py` / `07_face_encode.py`（F 系列人脸管线，2026-10-16，验收通过）、`05_rerank.py`（L2d 重排序，2026-10-16 权重到位后实现，验收通过）。`09/10` 的功能落在 `src/ui_wall.py` + `app.py` 的两个 Tab 里 | §15 要求 P0 通过后才写 P2；P0 已验收，故补齐 F 系列与 L2d |

### 0.2 全集跑起来后暴露的**硬件约束**（2026-10-16，改变了流水线结构）

这一节是本次改动最重要的一节：规格假设「各阶段读一遍素材」是廉价的，但本机不是。

| 事实 | 实测值 |
|------|--------|
| `E:` 的物理介质 | **14TB SATA 机械盘**（`ST14000NM005G-2KG133`，Disk 1），**不是 SSD** |
| 顺序读 | 161 MB/s（单个 29GB mp4 实测） |
| 随机读照片 | 18.6 个/秒，39 MB/s（300 张实测） |
| 库规模 | **503,057 个素材：478,634 照片 + 24,423 视频，9.34 TB**（照片 3.235 TB，视频 6.104 TB） |
| L1 清单扫描 | 46.2 秒（只 stat，不解码） |
| pHash 去重 | **11.4 张/秒 → 全集照片 11.6 小时**；近重复率**高度不均匀**（前 1 万张里 45.3%，另一批 4,000 张里只有 0.45%） |

后果：规格原本的四阶段划分（`02_dedup` → `03_extract` → `04_encode` → `06_face_detect`）
**每个阶段都要把 478,634 张照片重读一遍**，在机械盘上等于 **30 小时以上**。

因此照片侧做了**单趟读盘融合**（`config scan.fuse_photo_pipeline`）：

```
03_extract.py --kind photo    ← 唯一碰机械盘的一趟，同时做完 EXIF + pHash 去重 + 768px NVMe 缓存图
06_face_detect.py             ← 只读 NVMe 缓存图
07_face_encode.py             ← 只读 NVMe
04_encode.py                  ← 只读 NVMe 缓存图
```

实测该融合趟 **11.17 张/秒**（4,000 张：3,982 done / 18 dup / 0 error，358 秒），
即剩余照片约 **11.8 小时**，而不是 30 小时以上。

**由此产生的三处规格偏离（请复核）**：

| # | 规格原文 | 实际实现 | 原因 |
|---|---------|---------|------|
| G | L2a 去重（`02_dedup.py`）是独立阶段，先于 L2b 抽帧 | 照片侧默认**折进** `03_extract.py` 的同一趟读盘；`02_dedup.py` 仍完整保留为独立入口，两者共用 `src/dedup_run.py` 的同一套索引与代表项逻辑 | 机械盘上省掉两整趟读盘（约 24 小时）。判定结果与独立跑完全一致 |
| H | `process_photo` 只读 EXIF、不抽帧 | 照片会写一份**短边 768px 的 JPEG 缓存**到 `data/nvme/frames/<file_id>/0000_000000000.jpg`（与视频关键帧同一套目录约定） | 让 `04_encode`/`06_face_detect` 不再读机械盘。代价见下条 |
| I | F1 在**原图**上做 SCRFD 检测 | F1 在**缓存图**（默认短边 768）上检测 | 若读原图就要多一趟 11.8 小时。768 下原图里 300px 的人脸约 58~77px，SCRFD 可稳定检出；要满分辨率就把 `storage.photo_cache_short_edge` 调大（如 1600）重跑 `03_extract.py` + `06/07` |
| J | `point_id = (file_id << 20) \| shot_ix` 对两条管线都成立 | **人脸 collection 的 `point_id` 就是 `face_id`** | 一个 shot 里可能有多张人脸，而 `shot_ix` 已占满低 20 位，放不下人脸序号。`src/search.py` 的 `_fetch_face_vector` 与 `src/ui_wall.py` 的概览去重本来就按「point_id == face_id」实现，payload 里另带 `file_id`+`shot_ix`，「回退到同一文件+时间段」的能力没有丢失 |
| K | F0 稠密采样 `sample_fps: 1.0 / max_per_shot: 60` | 视频侧 F1 复用 L2b 已抽的关键帧（每镜头 ≤ `video.max_frames_per_shot` 张） | 满足规格「F1 直接读 L2b 产物，绝不重新解码视频」这条硬约束；2.4 万视频按 1fps 稠密抽帧要再解码数千小时。要真稠密就把 `video.max_frames_per_shot` / `long_shot_sample_per_sec` 调大重跑 `03_extract.py`，06/07 无需改动 |
| L | 质量分 `sharp = min(1, lap_var/500)` | `sharp = min(1, sqrt(lap_var / faces.quality_lap_norm))`，`quality_lap_norm` 默认 150 | 原式在真实 384px 关键帧上把 quality 中位数压到 **0.2179**，于是默认 `min_quality=0.3` 会把 **2/3 的人脸挡在照片墙外**。这是归一化尺度的标定错误，不是阈值错误 |
| M | 视频按 `file_id` 顺序处理 | 视频组内默认**体积升序**（`scan.video_size_order: asc`） | 视频共 24,423 个 / 6.104 TB，全集要跑好几天；先做小体积能最快把「已完成的视频数」堆起来，界面越早变多 |
| N | §7.2 自适应截断 `min_score: 0.55` | **`retrieval.min_score: 0.30`**，并新增**独立的** `retrieval.rerank_min_score: 0.25` | 见下面 §0.3①：0.55 **高于本模型能给出的最高分**，会让每个文字查询都返回 0 条 |
| O | §5「召回 → 自适应截断 → 重排序 k=400 → Top-50」 | 顺序改为**召回 → 重排序 → 截断**；默认只重排 **60** 条（规格写 400） | 见 §0.3①：召回余弦区分度太低，先截断会把候选砍空，重排就没东西可排。400 条实测约 80 秒/查询 |
| P | F3/重排序未提「精确匹配护航」 | 最高召回余弦 ≥ `retrieval.rerank_skip_cosine`(0.85) 时**跳过重排序**，保留召回顺序 | 见 §0.3③：交叉编码器给的是绝对 yes 概率（自我匹配实测仅 0.789），会把「库里就是这张图」压下去 |
| Q | 重排序直接吃视频查询 | 视频查询先抽一帧当**图像**查询再重排 | 见 §0.3④：vendored reranker 的 `return_video_metadata` 与本机 qwen-vl-utils 不兼容，且异常被吞掉后静默退化成 NULL 对 |

### 0.3 L2d 重排序（Qwen3-VL-Reranker-2B）：七个实测结论

补齐了 `05_rerank.py`（规格第十三章点名、此前因权重缺失未实现）。以下都是本机实测，
不是推测 —— 两条改了我原本的实现，一条否掉了一个看似显然的优化，
另外两条是**实现完之后才被抓出来的 bug**（⑥⑦，其中 ⑥ 已修）。

**① 规格的 `min_score: 0.55` 在本项目会让「搜什么都空」。**
18,506 个向量、9 个查询、k=400 实测：Qwen3-VL-Embedding-2B 的文字→图片余弦
**整体挤在 0.31~0.50**（合并 p99=0.451 p90=0.419 p50=0.366，**最大仅 0.497**）。
更关键的是，一个**故意编的无关查询词**（`完全无关的查询词xyzzy`）最高分也有 **0.405**。
⇒ 绝对余弦在这个模型上几乎不能区分相关与无关；用 0.55 当阈值必然返回 0 条。
⇒ 所以召回侧只保留一个宽松兜底（0.30，低于疑似相关的 p10=0.3125），
**真正的相关性判别交给交叉编码重排序**（自检里相关 0.547 / 无关 0.195，是能区分的）。
把 `05_rerank.py --text "人物" --no-rerank` 与不加 `--no-rerank` 各跑一次就能看到差别：
未重排的 top-5 完全由余弦决定，重排后 top-5 **换掉 4 条**。

**② 顺序必须是「召回 → 重排 → 截断」，而且两个分数空间要各自一套阈值。**
既然召回余弦区分度低，先按阈值截断就会把候选砍空、重排无从下手。
所以 `src/search.py` 拆出了不截断的 `semantic_recall()`，截断挪到重排之后
（`_final_cut()`），并按「是否重排」选用 `retrieval.rerank_min_score` 或 `retrieval.min_score`。
另外 `rerank_topk: 50` 是**硬上限**：这个模型分数区间太窄，自适应截断的阈值兜底那条
规则几乎不会触发，实测会原样返回 200 条。

**③ 「降像素预算换速度」被实测否掉了（我原本打算这么优化）。**
重排是交叉编码，每条候选约 0.2 秒；60 条 = 12 秒/查询，很慢，所以很自然想降像素预算。
同批 40 条真实候选上的对比（1280\*32\*32 为基准）：

| `rerank_max_pixels` | 秒/条 | 60 条 | 400 条 | Pearson | Spearman | **Top-10 重合** |
|---|---|---|---|---|---|---|
| `1280*32*32`（checkpoint 默认, 1.31MP） | 0.199 | 12.0s | 79.7s | 1.0 | 1.0 | 10/10 |
| `768*32*32`（0.60MP） | 0.145 | 8.7s | 58.0s | 0.829 | 0.802 | **6/10** |
| `512*32*32`（0.27MP） | 0.093 | 5.6s | 37.0s | 0.775 | 0.774 | **7/10** |
| `384*32*32`（0.15MP） | 0.073 | 4.4s | 29.4s | 0.753 | 0.722 | **5/10** |

Top-10 只有 5~7/10 与全分辨率一致 —— 这不是噪声，是**真实改变排序结果**。
（注意自检那组 0.5469/0.1953 在所有预算下都一样，因为它是**纯文本对**、
像素预算根本不参与，所以自检证明不了图片侧质量，必须像上表这样单独测。）
⇒ **保持 checkpoint 默认像素预算**，把「快/准」的取舍交给界面上的
「重排序候选数」滑块（`0 / 20 / 40 / 60 / 120 / 400`，选项上直接标出预估秒数）。

**④ 视频查询不能直接丢给重排序模型。**
vendored `qwen3_vl_reranker.py::tokenize` 会调
`process_vision_info(..., return_video_metadata=True)`，本机 qwen-vl-utils 报
`open() got an unexpected keyword argument 'metadata_errors'`；
**而且它把异常吞了**，内部退化成一条 `NULL` 文本对 —— 所有候选拿到无意义分数、
召回顺序被打乱（实测视频自身相似度从 0.991 掉到 0.459，被冒烟测试抓出来）。
⇒ 改成「视频先抽一帧，用图像对重排」（`src/rerank.py::build_query_media`），
并加了一道兜底：整批分数方差为 0 时判定为失败、退回召回顺序。

**⑤ 近重复护航。** 最高召回余弦 ≥ 0.85（远高于正常区间 0.50，只可能是「库里就是这张图」）时
直接跳过重排。因为交叉编码器给的是绝对 yes 概率，实测自我匹配只有 0.789，
重排反而会把精确匹配压下去 —— 以图搜图/以视频搜自身这两条冒烟断言就是这么被打破的。
跳过同时也省掉 8~12 秒。

**⑥ 尾部候选的分数必须与重排分隔离（2026-10-16 修的 bug）。**
重排只作用于前 `rerank_input_k`(60) 条，**之后的候选原样保留**（设计如此，绝不丢弃）；
但它们带的仍是**召回余弦**（实测区间 0.31~0.50），而重排分是 sigmoid(yes-no)
（实测 0.14~0.51）—— **两个区间重叠**。后果有两条，都用 `tmp/repro_rerank_score_space.py`
复现过（构造：重排段全 ≈0.39、尾部余弦 0.44）：

| 后果 | 修前实测 | 修后 |
|------|---------|------|
| 返回的分数序列在「重排段→尾部」处**上升**，破坏 `adaptive_cut` 的非递增前提 | index 29 处 0.39 → 0.44 | 单调不增（0.39 → 0.3896 → 0.3892） |
| 规格 8.2「重排后保留 Top-50」把**没重排过**的候选算进结果 | Top-50 里 20 条未重排（占 40%），挤掉已重排候选；界面角标出现「sim=0.44 排在 sim=0.39 后面」 | 未重排的一律降级到重排分之下，真实余弦存进 `recall_score` |

修法：未参与重排的候选（头部缺媒体的 + `input_k` 之外的尾部）统一压进
「严格低于已重排最低分」的单调递减带（`src/rerank.py` 的分数空间隔离段），
报告里用 `tail_demoted` 标出条数。回归测试：
`tests/test_rerank.py` 的 **[B5]**（10 条断言），修复前该用例会失败。

**⑦ 批大小基本不影响吞吐，但会改变分数（±0.02）。**
同批 24 条真实候选（GPU 同时被建库链的编码器占用）：

| `rerank_batch_size` | ms/对 | 60 条 | 400 条 | 与 batch=1 的最大分数差 |
|---|---|---|---|---|
| 1 | 616 | 37.0s | 246s | 0（基准） |
| 2 | 486 | 29.1s | 194s | 1.95e-02 |
| 4 | 410 | 24.6s | 164s | 1.95e-02 |
| 8（当前值） | 475 | 28.5s | 190s | 1.95e-02 |
| 16 | 424 | 25.5s | 170s | 1.17e-02 |

两点结论：
* **重排是逐对计算受限，不是延迟受限**：batch 从 2 加到 16，吞吐基本不变
  （所以「加大批大小提速」在这里是无效的；batch=1 反而只慢约 40%）。
* **批大小会改变分数**，因为 vendored `tokenize` 把一批 padding 到该批最长，
  而 `compute_scores` 取 `last_hidden_state[:, -1]` —— 左 padding 下位置编码随之偏移。
  最大差 0.02，而实测 top-10 里存在**完全相同的分数**（如三条都是 0.3887），
  也就是说这个量级足以翻动并列名次。
  ⇒ 含义：重排适合**粗排/换位**（实测 top-10 有 10 个位置被换掉），
  **不要**依赖 0.02 以内的分差；`(查询,文档)` 分数 LRU 缓存也因此是
  「近似缓存」而非「精确缓存」。要完全可复现就把 `rerank_batch_size` 设为 1。

> 操作提示：上表的 ms/对 是**建库链占着 GPU 时**的实测值。GPU 空闲时约 0.199 s/对
> （见 ③ 的表），即 60 条约 12 秒。**不要在跑建库的时候评估重排的交互延迟。**

### 0.4 实测证据修正规格的地方（**重要**）

| # | 规格原文 | 实测结论 | 证据 |
|---|---------|---------|------|
| A | §11 / §20.1：「`det_10g.onnx` > 100MB、`2d106det.onnx` > 10MB」 | **规格的体积阈值是错的**。官方 buffalo_l 真实构成：`det_10g.onnx` **16.1MB / 4.23M 参数**（SCRFD，9 个输出）、`w600k_r50.onnx` 166.3MB / 43.59M 参数（输出 [1,512]）、`2d106det.onnx` **4.8MB / 1.25M 参数**（输出 [1,212]）、`1k3d68.onnx` 137MB、`genderage.onnx` 1.26MB | `tests\verify_face_weights.py`：读 ONNX 图结构 + 真实推理，三者全部可用。**你现有的权重是完整的，无需重下** |
| B | §1.3：`onnxruntime-gpu`（未锁版本） | **必须锁 `1.20.2`**。PyPI 上 `>=1.21` 的 wheel 是 CUDA **12.8** 构建，`>=1.27` 默认 CUDA **13.0** 构建；本机 torch 是 cu126（CUDA 12.6），实测 1.31.0 报 `cublasLt64_13.dll which is missing` → 会话静默回退 CPU | [ONNX Runtime CUDA EP 兼容表](https://onnxruntime.ai/docs/execution-providers/CUDA-ExecutionProvider.html) |
| C | §20.1 必装清单未提 `torchvision` | **必须安装** `torchvision`（0.23.0+cu126）。`Qwen3VLProcessor` 需要它做图像预处理，缺失时 `AutoVideoProcessor requires the Torchvision library` | 首次跑 `04_encode.py` 的实际报错 |
| D | 权重完整性自检「体积正常」 | 改为**图结构 + 输出维度**判定（`expect_out_dim=512` / `expect_outputs_at_least=9`），体积下限只用于抓 LFS 指针 | 因为 A 项证明体积不是可靠信号 |
| E | §20.1 提到 sentence-transformers 加载方式 | 该 checkpoint 的 `modules.json` 声明了 `1_Pooling`/`2_Normalize` 子目录但**目录不存在**，且官方权重键名前缀是 `model.` 开头。用 `SentenceTransformer` 加载会 `Pooling.__init__() missing 1 required positional argument`，或出现 `Some weights ... were newly initialized`（**权重全部丢失，向量无意义**） | 实测。因此默认走权重目录自带的 `qwen3_vl_embedding.py`（`Qwen3VLEmbedder`），并内置**加载后自检**（norm≈1.0、不同文本相似度不是 1.0），一旦权重没加载上就报错而不是静默返回垃圾向量 |
| F | §11「权重离线可用（禁止无声下载 5GB）」 | 实现为 `HF_HUB_OFFLINE=1` + `TRANSFORMERS_OFFLINE=1` + `local_files_only=True` | `src\encode_model.py::apply_offline_env` |

---

## 1. 快速开始

> **本机（E: 是 14TB 机械盘）请直接看 §1.0「全集运行手册」**，不要按下面的朴素顺序跑
> ——朴素顺序会把 47.8 万张照片读四遍，等于 30 小时以上。

### 1.0 全集运行手册（2026-10-16 起）

一条命令，无人值守，分轮次推进（每轮约 1 小时，**第一轮结束照片墙就有内容**）：

```powershell
# 照片链 + 视频链，自动接力（照片跑完才开视频，避免两者抢同一块机械盘）
pwsh -NoProfile -File tmp\chain_rounds.ps1 -Stage all -RoundSize 25000

# 只想单独跑某条链
pwsh -NoProfile -File tmp\chain_rounds.ps1 -Stage photos
pwsh -NoProfile -File tmp\chain_rounds.ps1 -Stage videos
```

每一轮内部：

```
03_extract.py --kind photo --limit 25000   ← 唯一碰机械盘的一趟：EXIF + pHash 去重 + 768px 缓存
06_face_detect.py --limit 25000            ← 只读 NVMe 缓存图 → face_records（has_vector=0）
07_face_encode.py                          ← 只读 NVMe → ArcFace 512 + 128px 缩略图 + Qdrant
04_encode.py --limit 25000                 ← 只读 NVMe 缓存图 → 语义 2048 维 + Qdrant
```

**实测吞吐与 ETA**（2026-10-16，本机，均为**与抽帧并行时**的值）：

| 阶段 | 吞吐 | 全集 ETA |
|------|------|---------|
| `03_extract --kind photo`（融合） | 6.8~15.7 张/秒（随相册体积/重复率波动） | **约 17~20 小时**（428k 张，唯一碰机械盘的一趟） |
| `06_face_detect` | **79.6 张/秒**（只读 NVMe） | **约 70 分钟**（33 万张唯一照片） |
| `07_face_encode` | 16,278 文件 / 6.6 分钟 | 约 1 小时 |
| `04_encode`（语义，2 进程 + 成组） | 4.4 张/秒 | **约 27 小时 ← 当前瓶颈** |
| `03_extract --kind video` | 15~30 秒/个 | **数天**（24,423 个 / 6.104 TB） |

**关键结构：04 与抽帧并行跑。** 抽帧在等机械盘时 GPU 是空的，而语义编码是 GPU 密集。
不并行：17~20h(抽帧) + 27h(语义) + 1.5h(人脸) ≈ 45 小时；
并行后：max(抽帧, 语义) + 人脸 ≈ **约 28 小时**。链脚本每轮先**后台**起
`04_encode --shard i/N`，再前台跑本轮抽帧，最后等编码收尾并跑人脸链。

> ### 实测纠正：语义编码的并发与批大小（两次都测错/测反过，记录在此避免再走弯路）
>
> **① 多进程并发没有收益。** 早期我用 `--limit 400 --shard i/3` 测出「3 进程 9.55 张/秒
> = 2.65x」，**那个数字是错的**：`--limit` 在分片**之前**生效，每个分片实际只做
> `limit/3 ≈ 134` 个文件，我却按 `3 × limit` 算了分子。
> 重测（真实全集、同样并行条件）：1 进程 3.50 张/秒、3 进程 3.19 张/秒 —— **并发反而略慢**。
> 原因：照片是「一图一次前向」，batch=1 时 GPU 早已被单进程吃满（利用率 99%），
> 多进程只是在切 CUDA 上下文，还额外占了 19.7GB 显存（24GB 卡）。
> 现在的配置是 **2 进程 + 成组**，实测 4.4 张/秒。
>
> **② 真正的旋钮是「跨文件成组」，不是进程数。** `encode_file` 是「一个文件一次
> `encode_images`」，而照片只有 1 帧 → config 里的 `semantic.batch_size: 16`
> **对照片从未生效**。新增 `semantic.group_size: 8` 后（`src/encode.py::group_encode`），
> 8 个文件的帧合成一次调用：实测 3.5 → 4.4 张/秒。
>
> **③ 如果 27 小时太久**，最有效的一档是降 `semantic.max_pixels`
> （checkpoint 默认 `1800*28*28` ≈ 1.41MP，每图约 1000~1800 个视觉 token）。
> 实测（同一批 NVMe 缓存图，单进程 batch=16）：
>
> | max_pixels | 吞吐 | 33 万张 ETA | 与默认设置的向量 cosine |
> |---|---|---|---|
> | `1800*28*28`（默认，1.41MP） | 2.46 张/秒 | 37 h | — |
> | `768*28*28`（0.60MP） | 4.31 张/秒 | 21 h | 均值 **0.9842**，最小 0.9545 |
> | `512*28*28`（0.21MP） | 6.18 张/秒 | 15 h | 均值 0.9697，最小 0.9276 |
>
> **默认没有降**这个值：它直接改变入库向量，是检索质量的核心，不该由实现者擅自决定。
> 要换吞吐就在 config 里设 `semantic.max_pixels`，但**必须清库重编码**
> （`file_encoding` 里 semantic 行 + Qdrant 的 `materials_semantic`），
> 否则先后入库的向量分布不一致，检索会漂移。

> ⚠️ **并发写库的两个坑（都已修，改动别回退）**
> 1. `scan.commit_batch_fused_photos` 必须是 **1**。SQLite 只有一个写者；
>    抽帧每 `batch_commit_files` 个文件才提交一次，提交间隔就是写锁窗口。
>    设成 100（约 12 秒）时，编码进程实测 **CPU=0、磁盘 IO=0 完全饿死**，
>    只有 0.06 文件/秒（比单跑慢 50 倍）。设成 1 后正常。
> 2. `src/db.py` 的 `busy_timeout` 60 秒 + `retry_on_lock()` 退避重试必须保留，
>    否则一次锁竞争就会让编码进程抛 `database is locked` 退出、丢掉整轮工作。
> 3. PowerShell 变量名**不区分大小写**：链脚本里装进程的列表不能叫 `$encoders`
>    （会与 `[int]$Encoders` 参数撞名，报 `Cannot convert System.Object[] to Int32`）。

> ⚠️ **界面白屏的坑（2026-10-16 实际踩到，已修）**
> `app.py::_nvme_usage` 原实现用 `Path.rglob("*")` 遍历 `data/nvme`，
> **而且每次 rerun 都重算**。照片侧的 `frames/` 是「一张照片一个目录」的布局，
> 现在有 5 万多个目录、14 万个文件 —— Windows 上 rglob 要逐层开目录再逐项 stat，
> 实测要几十秒，表现为**打开界面长时间白屏（浏览器停在 Stop 按钮）**，
> 而且每次点击都会再卡一遍。
> 已改为 `os.scandir` 迭代遍历（`DirEntry.stat` 自带缓存，快一个数量级，实测 3.1s）
> 并加 `@st.cache_data(ttl=120)`。首屏现在实测 **5.7 秒**（含静态服务白名单、
> 库统计、照片墙 60 张卡片）。
> 另：`main()` 末尾加了一行 `界面渲染完成：...` 日志 ——
> 以后再遇到白屏，先看有没有这行，就能立刻区分「脚本没跑完」与「跑完了前端没显示」。
> 想自查可无头跑一遍：`.venv\Scripts\python.exe tmp\apptest_render.py`。

> 🩸 **换扫描根目录的坑（2026-10-16 实际踩到，差点丢数据）**
> `src/manifest.py::apply_classification()` 判定「消失」用的是**全局差集**：
> `SELECT file_id FROM files WHERE status != 'missing'` 减去本次扫到的路径集合。
> 它**不看本次 `--root` 是什么**。所以 `config.yaml` 里 `media_root` 改成另一个盘
> （或只扫某个子目录）再跑 `01_manifest.py`，会把**其余所有文件**（包括
> `skipped_dup`、包括 `done`）一次性标成 `missing` —— 界面上就是照片墙瞬间清空、
> 检索 0 结果。
>
> **数据没有丢**：`missing` 是软标记，抽帧/向量/人脸都还在 NVMe 和 Qdrant 里，
> 只是 `search.py` 只 join `done`，所以看起来「全没了」。
>
> **恢复办法（零重算，本项目实测 47 秒 + 5 秒）**：
> 1. `config.yaml` 的 `media_root` 改回原根；
> 2. `.venv\Scripts\python.exe 01_manifest.py --root "E:/"` → 打印 `revived N`；
> 3. `.venv\Scripts\python.exe 12_maintain.py fix-status` →
>    从 `file_encoding` 把 `skipped_dup` / `done` 还原回去（**不会碰 `missing`**）。
>
> **因此：本项目同时只支持一个根。** 想加第二个盘/子目录，先改 `media_root`
> 再**全程**用它跑 manifest，别混着扫。真需要多根，得先改
> `apply_classification` 把差集限定在 `path LIKE <root>%` 范围内（这是行为变更，
> 会让「扫子目录」不再检测子目录外的删除，动手前先确认需求）。

**写锁修复是可验证的，不是「看起来好了」。** 在抽帧正在跑的时候，从另一个连接反复
`BEGIN IMMEDIATE; ROLLBACK;`（申请真写锁后立刻回滚，零数据副作用），测**获取延迟**：

```powershell
.venv\Scripts\python.exe tmp\verify_write_lock_fix.py     # 含阳性对照，约 40 秒
.venv\Scripts\python.exe tmp\probe_write_lock2.py 20 2    # 只测生产环境，20 秒
```

本机实测（2026-10-16，照片链运行中）：

| 场景 | 采样 | 中位 | p99 | 最大 |
|------|------|------|-----|------|
| **无注入（生产真实情况）** | 6,145 | 0.02 ms | 4.17 ms | 535 ms |
| 注入一个**已知 4 秒**写锁（阳性对照） | 1,339 | 0.01 ms | 8.66 ms | **3,043 ms** |

第二行是关键：探针确实能抓到长锁（4 秒的锁报出 3,043 ms），所以第一行的「干净」
不是仪器不灵敏造成的假象。修复前 `commit_batch_fused_photos: 100` 时锁窗口约 12 秒，
编码进程实测 CPU=0、磁盘 IO=0 完全饿死（0.06 文件/秒）；修复后同一时段编码进程
稳定在 **4.05 文件/秒**，6,145 次抢锁 0 失败、日志里 0 条锁竞争告警。

> 生产环境那次 535 ms 的长尾来自 WAL checkpoint 之类的偶发事件（占比约 0.02%），
> 远小于 60 秒 busy_timeout，不构成饿死风险。

**可以随时 Ctrl+C 或直接关机**：所有阶段都以「文件」为单位记录在
`stage_status` 表里，重跑会从断点继续；融合去重的历史哈希从 `files.phash` 重新装载，
不会因为重启而重复劳动或漏判。

监控进度：

```powershell
# 各阶段跑到哪了
.venv\Scripts\python.exe 12_maintain.py status

# 待处理数量
.venv\Scripts\python.exe tmp\pending.py photo
.venv\Scripts\python.exe tmp\pending.py video

# 分轮次的起止与耗时
Get-Content logs\chain_state.tsv -Tail 20
Get-Content logs\chain_r1_extract_photo.log -Tail 5
Get-Content logs\chain_r1_encode_1.log -Tail 3     # 3 个语义编码分片

# 端到端接线自检（照片墙/检索/统计是否可用，随时可跑）
.venv\Scripts\python.exe tmp\check_e2e_wiring.py
```

### 1.1 朴素顺序（小库 / SSD 素材盘适用）

```powershell
cd "D:\DATA\PROJECT\建设多媒体整理系统"

# 1) 启动自检（任一项 FAIL 会打印修复命令并以退出码 1 结束）
.venv\Scripts\python.exe 00_bootstrap.py

# 2) 建立/刷新索引（L1 清单 → 只 stat，不解码）
.venv\Scripts\python.exe 01_manifest.py --root "E:/"

# 3) 图片感知哈希去重（L2a）—— 融合模式下可跳过，03 会顺带做掉
.venv\Scripts\python.exe 02_dedup.py

# 4) 镜头切分 + 抽帧 + EXIF/容器时间（L2b；照片侧默认融合去重 + 写缓存图）
.venv\Scripts\python.exe 03_extract.py

# 5) 语义编码 + 向量入库（L2c + L3）
.venv\Scripts\python.exe 04_encode.py

# 6) 人脸管线 F1–F3（照片墙的唯一数据源）
.venv\Scripts\python.exe 06_face_detect.py
.venv\Scripts\python.exe 07_face_encode.py

# 7) 重排序（L2d）自检 + 试跑：确认权重能加载、相关对高于无关对
.venv\Scripts\python.exe 05_rerank.py --self-test
.venv\Scripts\python.exe 05_rerank.py --text "人物" --k 100 --topk 10

# 8) 启动界面（双击也可以）
start.bat
```

**推荐顺序**：`00 → 01 → 03 → 06 → 07 → 04 → start.bat`
（把 06/07 排在 04 前面是刻意的：照片墙只依赖人脸管线，先跑就能更早看到可用界面）。
`05_rerank.py` 是**查询期**模块，不参与建库，任何时候都能跑；
界面里用「重排序候选数」滑块控制（0 = 关闭）。

每个阶段都可 `Ctrl+C` 中断后重跑，已完成的文件不会重复处理。
先小样验证再铺开：任何阶段都支持 `--limit N` 与 `--dry-run`。

---

## 2. 目录结构

```
D:\DATA\PROJECT\建设多媒体整理系统\
├─ 00_bootstrap.py          启动自检（§11，21 项必过 + 3 项警告）
├─ 01_manifest.py           L1 清单扫描（五类判定）
├─ 02_dedup.py              L2a pHash 去重
├─ 03_extract.py            L2b 镜头切分抽帧 + EXIF/容器时间
├─ 04_encode.py             L2c 语义编码 + L3 向量入库
├─ 05_rerank.py             L2d 重排序（Qwen3-VL-Reranker-2B，查询期）
├─ 06_face_detect.py        F1+F2 人脸检测与 track 合并
├─ 07_face_encode.py        F3 ArcFace 512 维入库 + 缩略图
├─ 08_search.py             命令行检索（界面走 app.py）
├─ 11_export.py             三入口 TSV / 结构化 TXT 导出
├─ 12_maintain.py           migrate/repair/reconcile/LRU/孤儿回收/配额/日志轮转
├─ app.py                   Streamlit 双 Tab 界面
├─ start.bat                双击启动
├─ config.yaml              唯一配置入口（禁止硬编码）
├─ requirements.txt         P0 实测通过的依赖版本
├─ migrations/
│  ├─ 001_init.sql          初始 schema（§4.1）
│  └─ 002_add_last_hit.sql  frames LRU 依据：last_hit_at / hit_count
├─ src/
│  ├─ common.py             Config / 日志双写 / CLI 骨架 / 子进程 / point_id / ORT 预加载
│  ├─ config_loader.py      --config / --config-overlay 统一加载
│  ├─ db.py                 SQLite WAL + 迁移执行器 + 两阶段状态机 + DAO
│  ├─ vector_store.py       VectorStore 抽象接口 + Qdrant 实现
│  ├─ manifest.py           L1 实现
│  ├─ exif.py               时间来源优先级解析
│  ├─ extract.py            L2b 实现
│  ├─ encode_model.py       Qwen3-VL-Embedding-2B 加载与编码（含加载后自检）
│  ├─ encode.py             L2c + L3 实现
│  ├─ query_rewrite.py      §7.4 文字检索轻量改写
│  ├─ search.py             三路路由 + adaptive_cut + RRF + L4 后处理
│  ├─ face_search.py        SCRFD/ArcFace 查询侧（精确找人的查询图检脸）
│  ├─ export.py             TSV / 结构化 TXT 导出
│  ├─ static_serve.py       本地静态服务（挂载白名单 + 防穿越 + 文件级白名单）
│  └─ ui_wall.py            Tab1 照片墙（keyset 分页）
├─ tests/
│  ├─ p0_smoke.py           P0 验收（40 项断言，自动生成/修复合成素材）
│  ├─ make_media.py         合成素材生成器
│  ├─ config_smoke.yaml     冒烟测试用的路径叠加配置
│  ├─ verify_face_weights.py 人脸权重结构+推理核验
│  └─ diag_*.py             故障定位脚本
├─ data/
│  ├─ library.db            SQLite 主库
│  └─ nvme/                 派生物（硬上限 300GB）
│     ├─ frames/<file_id>/  关键帧 JPEG（可再生，按 last_hit_at LRU 淘汰）
│     ├─ thumbs/            素材缩略图（永不淘汰）
│     ├─ faces/raw|thumbs/  人脸 112 / 人脸墙 128（永不淘汰）
│     ├─ embeddings/        镜头级编码缓存
│     └─ query_cache/       用户上传的检索图（24h 清理）
├─ logs/<date>.log          日志双写目标（每条带 session_id）
├─ tmp/                     临时文件（TMP/TEMP/TMPDIR 目标）
├─ weight/                  权重（离线，禁止无声下载）
│  ├─ Qwen3-VL-Embedding-2B/
│  └─ buffalo_l/
└─ README.md
```

---

## 3. 环境（本机实测值）

| 项 | 值 |
|---|---|
| 项目根 | `D:\DATA\PROJECT\建设多媒体整理系统` |
| 素材根 | `E:\`（实测素材集中在 `E:\晚上`：约 43 万 JPG + 2.4 万 MP4） |
| Python | `.venv` = 3.12.14 |
| GPU | RTX 3090 24GB，驱动 596.49 |
| torch / torchvision | 2.8.0+cu126 / 0.23.0+cu126（`torch.cuda.is_available()=True`） |
| onnxruntime-gpu | **1.20.2**（CUDA 12.x 构建，实际推理 provider = `CUDAExecutionProvider`） |
| transformers | 4.57.1 |
| Qdrant | Docker 容器 `qdrant`，`http://127.0.0.1:7333` |
| ffmpeg | `D:\PROGRAM\1_ffmpeg-9.0.1-essentials_build\bin`（回落到 PATH） |
| NVMe 预算 | 300 GB（`data/nvme`） |

### 3.1 从零重建虚拟环境

```powershell
cd "D:\DATA\PROJECT\建设多媒体整理系统"
uv venv --python 3.12 --seed .venv
pwsh -NoProfile -File tmp\setup_venv4.ps1     # 一键装齐（含断点续传下 torch）
```

`tmp\setup_venv4.ps1` 做的事情（手工等价）：
1. `uv venv --python 3.12 --seed .venv`
2. `curl -C - --retry` 断点续传下载 `torch-2.8.0+cu126-cp312-cp312-win_amd64.whl`（2.9GB），
   再 `pip install <本地whl>`（**国内镜像直连 pytorch 索引常挂死，必须走 curl**）
3. `pip install onnxruntime-gpu==1.20.2`、`transformers==4.57.1`、`sentence-transformers`、
   `qwen-vl-utils`、`qdrant-client`、`scenedetect`、`imagehash`、`streamlit`、`insightface`
4. **关键收尾**：`pip uninstall -y onnxruntime`（insightface 会把 CPU 版装回来）
5. 校验 `ort.get_available_providers()` 含 `CUDAExecutionProvider`

---

## 4. 验收（§15 P0 六条）

一键执行（**必须用 venv 的 python**，见文首说明）：
```powershell
.venv\Scripts\python.exe -u tests\p0_smoke.py
```
**当前结果：PASS=43 FAIL=0 SKIP=0**

如果误用全局 python，会得到 `PASS=36 FAIL=0 SKIP=4` 并提示「这不是完整验收」，
同时打印正确的命令——不会给出误导性的失败。

测试在 `tests\media_smoke\` 上自动生成合成素材（含近重复图、中文/空格/括号/& 路径、
12 秒测试视频、过小文件），不触碰真实素材库。若中途删过文件，脚本会自动重建素材。

| 规格验收项 | 自动化断言 | 结果 |
|---|---|---|
| ① 20 query judgment 基线 | **需你提供 20 条 query 草稿**。界面 Tab2 底部有 `🏷 标注` 面板，每个结果卡片点 ✅/❌ 写入 `judgments` 表（TREC 风格）。替代冒烟：`08_search.py --text "…"` 返回带时间段与绝对路径的命中 | 管线已验证；nDCG 基线待标注 |
| ② 路径含中文/空格/括号/& 时 📂 生效 | 断言中文路径被正确摄入 + 静态服务可访问该中文路径（HTTP 200）；`open_containing_folder` 用 `explorer /select,"path"` 封装，失败返回文案而非抛异常 | ✅ |
| ③ 视频 ▶ 预览从 shot_start 播 | 断言 `shots.shot_end > shot_start` 且时间段非零；界面用 `<video src="…mp4#t=start,end">` | ✅ |
| ④ TSV：tab 分隔 + UTF-8 BOM + DEAD 校验 | 断言 BOM=`[239,187,191]`、表头 7 个 tab/8 列且列名与规格一致、删 3 个源文件后 `# DEAD_PATHS` 恰好 3 条 | ✅ |
| ⑤ 同批导出两次 diff 为空 | 断言去掉注释行后两次导出行完全一致 | ✅ |
| ⑥ 扫描异步不假死 + Ctrl+C 可续跑 | 断言二次扫描 `new=0 / unchanged=7`（增量判定生效）、missing 软删与复活、`done/error` 计数符合预期 | ✅ |

### 4.1 各阶段独立验收标准

每个模块都支持 `--dry-run` / `--shard N/M` / `--root <path>`（§〇第 2 条）。

| 模块 | 验证命令 | 通过标准 | 失败判定 |
|---|---|---|---|
| 00_bootstrap | `python 00_bootstrap.py` | 必过项全 ✅，退出码 0 | 任一 ❌ 或退出码 1 |
| 01_manifest | `01_manifest.py --root E:/晚上 --dry-run` | 打印五类计数，不写业务数据 | 抛异常；或 `--dry-run` 后 files 表被改动 |
| 01_manifest | 连跑两次（非 dry-run） | 第二次 `unchanged` ≈ 全量、`new=0` | 第二次仍报大量 `new` |
| 02_dedup | `02_dedup.py --dry-run` | 列出近重复对，不写库 | 抛异常 |
| 02_dedup | 真跑后查库 | 被跳过文件在 `file_encoding` 有**两行** `skipped_dup` | 只有一行（违反铁律 1） |
| 03_extract | `03_extract.py --limit 20` | 每视频有 shots 行 + `frames/<id>/*.jpg` | 整批中断；关键帧 <2KB 或全黑 |
| 04_encode | `04_encode.py --limit 20` | 日志出现「编码自检 … norm=[1.0, 1.0]」；`file_encoding(semantic)=done`；Qdrant 点数增加 | 权重联网下载；日志出现 `newly initialized`；instruction 出现在入库侧 |
| 08_search | `08_search.py --text "…"` | 返回排序命中（含时间段+绝对路径）+ 截断提示 | 返回空但 collection 有点数 |
| 11_export | 见 §4④⑤ | BOM + tab + DEAD 正确 + 幂等 | 列错位 / 无 BOM / 两次 diff 非空 |
| 12_maintain | `12_maintain.py reconcile --json` | 正常状态 `verdict: 零差异`；有孤儿向量时给出具体数量 | 抛异常 |

---

## 5. 日常命令速查

### 5.1 索引建到哪了？怎么查？

**方式一：命令行（最快，推荐）**

```powershell
.venv\Scripts\python.exe 12_maintain.py status
```

`--json` 可得机器可读版本。

**方式二：界面侧边栏 → `📈 索引进度详情`**
显示索引根（实际值）、各阶段计数、shots/faces、下一步建议；
勾选「统计顶层目录分布」还会列出每个顶层目录的文件数
（该项要遍历全表，约 1~2 秒，所以默认不勾）。

**方式三：直接查库**

```powershell
.venv\Scripts\python.exe -c "import sqlite3;c=sqlite3.connect(r'data/library.db');print(c.execute('SELECT status, COUNT(*) FROM files GROUP BY status').fetchall())"
```

**方式四：日志**
每次扫描双写 `logs\<date>.log`（每条带 `session_id`）；控制台也打印汇总表
（candidates / new / modified / moved / unchanged / missing / dir_errors / scan_seconds）。

### 5.2 各阶段会让哪些数字发生变化

| 跑完的脚本 | `by_status` | `time_source` | `shots` | `nvme_used` | `faces` |
|---|---|---|---|---|---|
| `01_manifest.py` | 全部 `pending` | 全 `(未解析)` | 0 | 0 B | 0 |
| `02_dedup.py` | 近重复转 `skipped_dup` | 不变 | 0 | 0 B | 0 |
| `03_extract.py` | 转 `done`（失败转 `error`） | 出现 `filename_date`/`mtime`/`exif_*` | > 0 | 增长（关键帧） | 0 |
| `04_encode.py` | 不变（进度看 `encoding`） | 不变 | 不变 | 略增 | 0 |
| P2 `06/07`（未实现） | 不变 | 不变 | 不变 | 大幅增长 | > 0 |

> ⚠️ `照片墙` 与 `精确找人` 依赖 `faces > 0`，而 `faces` 只由 P2 的人脸管线产生。
> 只跑到 `04_encode.py` 时照片墙必然为空——这是预期行为，侧边栏会明确提示。

### 5.3 常用命令

```powershell
# 看索引状态 / 下一步建议
.venv\Scripts\python.exe 12_maintain.py status

# 增量扫描（新增/修改/移动/删除判定）
.venv\Scripts\python.exe 01_manifest.py --root "E:/晚上"

# 先看会发生什么（不写业务数据）
.venv\Scripts\python.exe 01_manifest.py --root "E:/晚上" --dry-run

# 分 8 片并行编码（开 8 个终端各跑一片）
.venv\Scripts\python.exe 04_encode.py --shard 1/8

# 一致性核对 / 配额
.venv\Scripts\python.exe 12_maintain.py reconcile --json
.venv\Scripts\python.exe 12_maintain.py quota --json

# 修复卡住的入库（<=3 次，超限入死信表）
# 2026-10-16 修：「重置为 pending」现在**同时清掉该阶段的 stage_status 行**。
#   各阶段的取数条件是 `ss.status IS NULL OR ss.status IN ('pending','error')`，
#   所以 stage_status='done' 的文件永不入选 —— 早期只改 file_encoding，
#   那些「Qdrant 已有向量、file_encoding 停在 pending」的文件（进程被中断留下的）
#   永远修不回来，reconcile 每次都要报一批孤儿向量。
.venv\Scripts\python.exe 12_maintain.py repair --dry-run
.venv\Scripts\python.exe 12_maintain.py repair

# 补齐铁律 1 的记账：为每个 status='skipped_dup' 的文件补齐缺失的 skipped_dup 行
# （幂等，只 INSERT 缺的那些）。什么时候会缺：维护时按 model 单独清理 file_encoding，
#   例如换语义编码像素预算后 `DELETE ... WHERE model='semantic'`，
#   会把 semantic 侧的 skipped_dup 一起删掉，两个模型的计数就对不上了。
.venv\Scripts\python.exe 12_maintain.py fix-skipped-dup

# 关键帧 LRU 清理（依据 files.last_hit_at；thumbs 与 faces 永不淘汰）
.venv\Scripts\python.exe 12_maintain.py clean-frames --days 30 --dry-run

# 检索临时图清理（24h）
.venv\Scripts\python.exe 12_maintain.py clean-query-cache

# 孤儿回收（必须手动 + 二次确认；先看 dry-run 报告）
.venv\Scripts\python.exe 12_maintain.py prune-orphans
.venv\Scripts\python.exe 12_maintain.py prune-orphans --confirm

# 三入口导出
.venv\Scripts\python.exe 11_export.py --scope library
.venv\Scripts\python.exe 11_export.py --scope wall_filtered --min-quality 0.3 --relative
.venv\Scripts\python.exe 11_export.py --scope search_hits --text "会议室里三个人"

# 结构化 TXT（供下游 agent 剪辑归档；含可直接复制的 explorer 命令）
.venv\Scripts\python.exe 11_export.py --scope library --format txt
```

---

## 6. 故障归因速查（§16 + 本项目实测补充）

| 现象 | 先查 |
|---|---|
| 搜什么都空 | `files.status` 是否 `done`；`materials_semantic` 是否有点（`12_maintain.py reconcile`） |
| 搜得到但时间码错 | `shots.shot_start/end` 与 ffmpeg 实际切帧是否一致 |
| 换模型后精度暴跌 | 是否忘清 `file_encoding` 致新旧向量混；instruction 是否误拼到入库侧 |
| 人脸检索全是陌生人 | 对齐逻辑被改过 / 阈值过高 / F2 未生效致库被重复帧污染 |
| 人脸检索全是同一个人重复 | F2 track 去重没生效；检索侧 shot 级去重没挂 |
| 扫描巨慢 | 素材在 NAS；或未走增量把全库重解码；或未用反向比对索引表 |
| NVMe 爆满 | frames LRU 没配定时；或 orphan 从未回收 |
| 照片墙缺图 | `faces/thumbs/` 未在 F3 同步写出，或 `min_quality` 过高 |
| 照片墙巨慢 | 误用 Qdrant scroll；应走 sqlite 分页（`ui_wall.wall_page`） |
| 打开文件夹无反应 | subprocess 传了 shell 字符串 / 路径特殊字符 / 网络盘 |
| 导出 TSV 列错位 | 用了逗号分隔 → 改 tab + 加 BOM |
| 时间排序错乱 | `time_source=mtime` 占比高 → EXIF 读取失败 |
| 首次启动卡住 | 权重在无声下载 5GB；或 NVMe 属主为 root |
| **onnxruntime 走 CPU** | ①装了 CPU 版 onnxruntime（insightface 会装回来）→ `pip uninstall -y onnxruntime`；②`onnxruntime-gpu` 版本与本机 CUDA 不匹配 → 锁 `1.20.2`；③未在创建 session 前 `import torch` 预加载 DLL。`00_bootstrap.py` 三项都会检查 |
| **`pip install` 卡在 0% 不动** | 国内直连 `download.pytorch.org` 与多数镜像会挂死。用 `curl -C - --retry --speed-time` 断点续传下 wheel 后本地安装 |
| **`ort.get_device()` 抛 AttributeError** | onnxruntime >= 1.23 已移除该 API，一律用 `ort.get_available_providers()` |
| **`cv2.imread` 读中文路径返回 None** | 改用 `np.fromfile` + `cv2.imdecode`（已在 `face_search._imread_unicode` 实现） |
| **Qdrant 容器起不来，报 `bind: access forbidden`** | Windows 保留端口段。`netsh interface ipv4 show excludedportrange protocol=tcp` 查；本项目已改用 7333 |
| **Qwen 加载后向量全是垃圾** | 日志若出现 `Some weights … were newly initialized`，说明权重键名前缀没对上。本项目自带加载后自检会直接报错 |

---

## 7. 隐私与安全（§17 + 加固项）

* 人脸库只存本机 `data/nvme/faces`，不进云不上报。
* 静态服务绑 `127.0.0.1`（禁止 `0.0.0.0`），拒绝 `..` 穿越，禁用目录列表。
* **加固**：`/media` 启用**文件级白名单**——只允许库中 `status != 'missing'` 的文件。
  这条把「目录白名单」收紧为「文件白名单」：即使 `media_root` 被设成整个盘根（如 `E:\`），
  也不会把整块磁盘暴露给浏览器。（开关：`ui.restrict_static_to_indexed`）
* 导出 TSV 默认排除人脸裁剪图路径（`privacy.exclude_face_crop_from_export`）。
* 日志只记 `face_id`，不记人脸向量与原图；日志中的 Windows 绝对路径自动加引号。
* 照片墙支持 `face_records.hidden` 软标记；远程访问时 `is_local_request()` 判定关停。

---

## 8. 数据模型与三条铁律

* `migrations/001_init.sql` 建全部表；`002_add_last_hit.sql` 增 `last_hit_at / hit_count`。
* 迁移执行器按 `db_version` 顺序执行大于当前版本的脚本，**每个脚本包在一个事务里，失败回滚**。
* 铁律 1：去重跳过的文件在 `file_encoding` 为 `semantic` 与 `face` **各记一行 `skipped_dup`**。
* 铁律 2：删除只标 `status='missing'`，绝不物理删；改名/移动经 inode 判定后只更新 path；
  文件重新出现时自动**撤销 missing**（`revived` 计数）。
* 铁律 3：两阶段入库 `pending → 写 Qdrant → done + encoded_at`；检索只 join `done`。
* `point_id = (file_id << 20) | shot_ix`，语义与人脸两条管线回退到同一「文件+时间段」。

---

## 9. 下一步（P1 → P3，未开工）

按 §15，P0 验收通过后才进入下一阶段：

* ~~**P1** Reranker（`src/rerank.py`）+ 阈值分档校准 + adaptive_cut 前端提示。~~
  **已完成（2026-10-16）**：`src/rerank.py` + `05_rerank.py` + 界面「重排序候选数」滑块
  （`app.py`：0/20/40/60/120/400，选项上标出预估秒数）+ 两个分数空间各自一套阈值。
  验收口径说明：P@20 提升需要人工标注集，本项目还没有标注数据，所以改用可复现的替代证据 ——
  自检「相关 0.547 / 无关 0.195」能区分；同一查询下重排把 top-10 的 10 个位置全换过
  （`overlap_in_topk` 只剩 4 条）；返回条数受 `rerank_topk` 硬上限约束、不抖动；
  阈值标定有回归测试（`tests/test_rerank.py` 的 [B1]，防止改回 0.55 又把结果砍空）。
  **P@20 的人工标注评估仍是未完成项**，见 §9 末尾。
* ~~**P2** `06_face_detect.py` / `07_face_encode.py` + 照片墙概览/平铺/分组 + [找同款] 闭环 + 海报过滤三条。~~
  **已完成（2026-10-16）**：F1+F2（`06_face_detect.py`，实测 **79.6 张/秒**）+ F3
  （`07_face_encode.py`，1.63 万文件 6.6 分钟）+ 概览/平铺两模式 + [找同款] 闭环。
  照片墙已实测出脸（17,130 张，时间跨 2013–2026）。
  F2 的验收已用**合成观测序列**做成确定性断言（`tests/test_face_f2.py`，PASS=25）：
  同人 30 帧合成 1 条 track、双人同框成 2 条、代表帧两两间隔 ≥1s、
  模拟 5 分钟 1fps 访谈输出 ≤ 期望×1.5。
  **规格原文要求的那份「10 段含人访谈视频 + 人工标注期望代表帧数」的验收素材本项目没有**，
  这是未完成项；海报过滤（>15 张脸 → `group_poster`、墙上默认隐藏）已实现（实测 4 个文件命中）。
* **P3** 增量 timer + moved/missing 处理 + repair/reconcile + LRU + 孤儿回收 + 配额告警 + 故障手册。
  验收：无人值守跑一周不崩；NVMe ≤ 预算；reconcile 零差异；删 1 万张后 reconcile dry-run 报告正确。

**注意**：任务计划程序定时增量扫描属于 P3，尚未创建。

---

## 10. 已具备的能力（对照你的原始需求）

> 2026-10-16 更新：P0 之后补齐了 F 系列人脸管线与 L2d 重排序，下表里原先标「待 P2」的
> 几项已经实际可用（照片墙已出 17,130 张脸、精确找人/找同款已通）。

| 你的需求 | 状态 |
|---|---|
| 指定本地绝对路径，递归扫描照片与视频，建立离线索引 | ✅ `01_manifest.py`（增量/五类判定），全集 503,057 个 / 9.34 TB / 46 秒 |
| 三种检索输入：文字 / 图片 / 视频 | ✅ `08_search.py` 与界面 Tab2 |
| 图片/视频输入时可勾选「精确找人」 | ✅ 06/07 已建人脸库并实测可用（17,130 张脸、`materials_faces` 18,868 个点） |
| 照片墙（浏览模式，不依赖 query） | ✅ keyset 分页 / 筛选 / 概览去重 / 预览 / 键盘导航，已出脸 |
| 命中清单：时间码 + 完整绝对路径 + 相似度 badge | ✅ |
| 卡片操作：▶ 预览（视频跳时间段）/ 📂 打开所在文件夹 / 找同款 | ✅ 三项全通（找同款走 `face_id` 反查） |
| 重排序提升相关性（L2d） | ✅ `05_rerank.py` + 界面「重排序候选数」滑块（0 = 关闭） |
| 导出 TSV（三入口、tab、DEAD 校验、幂等） | ✅ |
| 输出结构化 TXT 供下游 agent / 快捷操作 | ✅ `--format txt`（含可复制的 explorer 命令） |
| 增量扫描：只处理新增/修改/移动，异步可见进度 | ✅ |
| 相似度分数不混排（语义 sim= / 人脸 face=） | ✅ 不同颜色 badge |
