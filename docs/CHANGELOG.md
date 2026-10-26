# 变更日志（docs/CHANGELOG.md）

> 只记录**影响行为**的变更（新增能力 / 修的 bug / 性能数量级变化），
> 每条写「原因 → 修法 → 验证」。更早的历史见 `README.md` 的 v5.3 一节。

---

## 2026-10-26 · 自动分组崩溃、停用不彻底、UI 说明

### 修复：自动分组在 31.5 万张脸上崩溃（退出码 3221225725）
* **原因**：`3221225725` = `0xC00000FD` = STATUS_STACK_OVERFLOW。sklearn HDBSCAN 在
  31.5 万点上建最小生成树，Windows 默认 1MB 线程栈不够，进程直接死（Python 抓不到）。
* **修法**：① HDBSCAN 放进 `threading.stack_size(128MB)` 的线程；
  ② `cluster_with_sampling()` 多轮抽样：每轮抽 `cluster.max_fit_faces`（默认 12 万）拟合，
  其余按「最近簇心 + 簇半径 95 分位 × 系数」归组，直到剩余 ≤ 上限再整体拟合；
  ③ 归组阈值从固定 0.35 改为按数据尺度自适应（原来在 UMAP 空间里几乎归不进任何点）。
* **验证**：`tests/test_cluster_sampling.py` PASS=5；真实库 31.5 万张脸 6 分钟完成，
  噪声从首版 **143,611（45%）降到 1,218（0.4%）**。

### 修复：停用素材根后照片墙仍显示它的人
* **原因**：停用只把 `files.visible` 置 0，**没有重算 `cluster_meta`**（人数/封面是分组快照）。
  实测 2,484 组里 2,155 组全部成员已不可见、2,432 组封面已失效，卡片照旧。
* **修法**：`media_roots.set_enabled()` → `cluster.refresh_meta_for_root()` 重算受影响组；
  可见成员为 0 的组从 `cluster_meta` 删除（重新启用时 `ON CONFLICT` 会恢复）；
  `_members()` 加 `f.visible=1`；照片墙版本行改用 `cluster.live_meta_stats()` 实时值。
* **验证**：`tools/check_disabled_hidden.py`；停用/启用闭环测试 4/4；
  UI 路径检索只返回可见目录文件。

### 修复：分组/自动分组按钮点了没反应
* **原因**：`start_task()` 的僵尸守卫用「pid 还活着」判断，而 Windows 复用了 PID
  （2 天前失败任务的 pid 被 `msedgewebview2.exe` 占用）→ 守卫每次命中，所有任务都起不来。
* **修法**：`tasks.pid_looks_like_task()` 校验命令行含 `run_task.py` + task_id；
  `clear_reused_pids()` 清理被复用的历史 pid；界面在按钮禁用/启动失败时显示原因。
* **验证**：`start_task(cfg,"cluster")` 返回 task_id 并真正跑完。

### 改进：照片墙「未参与分组」的说明
* 明确区分两个"清晰度下限"（`faces.quality_min_show` vs `cluster.quality_gate`），
  并说明分组是快照、改门槛必须重新分组。

---

## 2026-10-24 · 方案 B（全速模式）与画质相关修复

### 性能：视频帧抽取并行 + 超长视频跳过全量镜头检测
* **原因**：视频严格串行（`compute_workers()` 算出的并发数从未被使用），
  单视频 scdet 要把整片解码一遍；且「无切点」时还会换后端**再解码一遍**。
* **修法**：① 同一视频内所有 `(镜头, 时间点)` 帧用线程池并发抓（`performance.frame_workers`）；
  ② 空切点 = 整段单镜头，不再换后端（`None` 表示失败、`[]` 表示成功无切点）；
  ③ 时长 > `video.scdet_max_duration_sec`（1800s）直接按 `force_split_sec` 切段。
* **验证**：7.2 GB / 148 镜头视频 **429s → 67s（6.4×）**；同类扫描进度 1.1 → 6.0 文件/分钟。

### 性能：照片预读
* **原因**：照片一张一张「读盘→解码→pHash→写缓存」，机械盘与 CPU 互相空等。
* **修法**：`PhotoPrefetcher` 后台线程只并行读字节（去重顺序与写库顺序不变）；
  线程间共享游标（曾因各自遍历同一列表导致每个文件读 6 遍、进度几乎停滞）。
* **验证**：3000×2000 照片 13.9 → **24.6 张/秒**。

### 修复：`performance.mode` 只是标签，切全速无效
* **原因**：代码只读具体数值（默认 0），从不读 `mode`。
* **修法**：`common.perf_value(cfg,key)`：具体值优先，否则按档位取默认（turbo→预读6/帧8/视频12/批32）。
  另修 UI toggle 的 key 绑定（配置变了控件才重建，避免一点「立即应用」把全速撤销）。

### 修复：视频 `prefetched` 作用域 bug（致命）
* **原因**：字符串替换把 `Image.open(path)` 改在了 `validate_frame()`（视频帧校验）里，
  所有视频帧报 `name 'prefetched' is not defined` → 整片抽帧失败 → 任务失败（白跑 1h38m）。
* **修法**：`validate_frame()` 还原；`process_photo()` 正确接入预读字节。

### 修复：视频抽帧写事务过长
* **原因**：整片一个事务（200+ 镜头）→ 界面/取消都 `database is locked`。
* **修法**：按 2 秒分片提交。

---

## 2026-10-23 · 清理、HEIC、启动页

### 新增：HEIC / HEIF 支持
* **原因**：扩展名白名单没有 heic、没装 `pillow-heif`、`imread_bgr` 用 cv2（不支持 HEIC）
  → iPhone 原片被完全忽略（库里 1,479 条旧 HEIC 全是 `error`）。
* **修法**：装 `pillow-heif`；`common.register_heif_opener()` 全局注册；
  `imread_bgr` 回退 PIL；白名单加 `heic/heif`；旧错误记录重排为 pending。
* **验证**：`tests/test_heic.py` PASS=7。

### 性能：清理「已删除（可恢复）」批量化
* **原因**：每文件 4 次 HTTP（`delete_by_file` 内含 count）+ Qdrant 按 file_id 过滤删
  → 1,702 条要 17 分钟（0.6 条/秒）。
* **修法**：`vector_store.delete_ids()` 按点 ID 批量删；`_purge_rows` 每 500 条一批，
  每批一个事务 + 锁冲突退避重试（不再"记个 warning 就当删掉了"）。
* **验证**：1,500 文件 / 7,500 向量 **0.28 秒（5,338 条/秒）**。

### 修复：任务永远显示「正在停止…」
* **原因**：用户点停止后子进程自己退了，没有代码收尾；`reap_stale` 只处理「心跳超时」。
* **修法**：新增「cancel=1 且 pid 已死 → 立刻判 cancelled（15 秒宽限）」；
  `stop_task` 改为非阻塞 + 12 秒看门狗强杀。

### 新增：启动页可选
* `ui.start_page`（默认 search）+ 设置页可选 6 个页面。

---

## 2026-10-22 · 可见性、路径、移动识别

### 修复：移动文件后库里仍是旧地址（根因：inode 从未落库）
* **原因**：Windows 上 `DirEntry.stat().st_ino` 恒为 0（实测），而移动识别依赖 inode
  → 56 万行 `inode=0`，所有移动/改名都被当成「新增 + 缺失」。
* **修法**：`walk_media` 补 `os.stat()` 取真 inode；`classify` 增加「size+mtime+同名
  且旧路径不存在」的指纹兜底；`unchanged` 行回填 inode。
* **验证**：`tests/test_moved_detect.py` PASS=10（移动 → moved、复制 → 不误判）。

### 修复：跨素材根移动变成「新增+缺失」
* **修法**：`classify` 对每个 new 再做一次跨根指纹匹配（旧路径必须已不存在），命中即 moved 并改 root。
* **验证**：`tests/test_dedup_and_crossroot.py` PASS=7。

### 新增：视频去重
* **原因**：照片有 pHash，视频完全没去重（实测 906 组「同 size+mtime+文件名」被完整处理两遍）。
* **修法**：抽帧前按 `(size, mtime, 文件名)` 判重 → `skipped_dup`；新增 `idx_files_size` 索引。

### 改进：停用=隐藏 / 移除=删库
* 停用 → `files.visible=0`（不显示不搜索，数据全留）；移除 → 默认从库中彻底删除（磁盘不动）。

### 修复：静态服务白名单是启动快照
* 移动后新路径 404 → 未命中时对 `files.path` 做一次点查（`_indexed_now`）。

---

## 2026-10-20 ~ 10-21 · 并发与稳定性

* **修复**：`heartbeat timeout` 误判把活着的任务判死（阈值 2 分钟 < 单视频 10 分钟）
  → 阈值按 `video.single_video_timeout_sec` 自适应（≥15 分钟），且**必须 pid 已死**才判死。
* **修复**：心跳判定不再只看时间戳，新增 `_pid_alive()`。
* **修复**：并发多份流水线（误判 → 解锁按钮 → 再点一次）→ `start_task` 加僵尸拦截。
* **性能**：`reap_stale` 先只读探测再决定写（原来每次页面渲染都抢写锁 → 切页卡数秒）；
  `tasks._connect` 去掉 `PRAGMA journal_mode=WAL`。
* **性能**：`关于` 页从 103.9 秒降到 2.7 秒（schema 用 `MAX(rowid)`、磁盘占用读缓存值）。

---

## 2026-10-19 · v5.3 主体交付

任务队列 + 事件刷新、设置全部 UI 化、路径变更五步向导、人脸聚类与版本回退、
灵敏度标定、完整性检查、幂等唯一索引 + lease、日志与告警预留。
详见 `README.md`「v5.3 新增功能一览」与 `tests/test_v53.py`。
