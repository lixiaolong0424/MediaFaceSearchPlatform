# -*- coding: utf-8 -*-
"""补丁：把 T.tick() 移到 conn.commit() 之后。

原因：tick 用独立连接写 tasks 表；如果主连接当时还握着未提交的写事务，
两个连接互等 → 进度写全部被跳过（界面永远 0/N），且每个文件白等 1 秒超时。
"""
import io

# ---- extract.py ----
p = "src/extract.py"
s = io.open(p, encoding="utf-8").read()
old = """        processed += 1
        # 进度上报 + 取消检查（没绑定任务时是空操作）。
        # 用户点「停止任务」后最多再处理 1 个文件就退出，已提交的进度全部保留。
        T.tick()
        # 写锁窗口必须有时间上限：只看「每 N 个文件提交一次」是不够的，
        # 视频单个文件要 15~30 秒，N=100 就是几十分钟不提交，
        # 这段时间里任何别的写库动作（进度上报、界面、并行编码）都会被堵死。
        if processed % commit_every == 0 or (time.monotonic() - last_commit) >= 2.0:
            D.retry_on_lock(conn.commit)
            last_commit = time.monotonic()
"""
new = """        processed += 1
        # 写锁窗口必须有时间上限：只看「每 N 个文件提交一次」是不够的，
        # 视频单个文件要 15~30 秒，N=100 就是几十分钟不提交，
        # 这段时间里任何别的写库动作（进度上报、界面、并行编码）都会被堵死。
        if processed % commit_every == 0 or (time.monotonic() - last_commit) >= 2.0:
            D.retry_on_lock(conn.commit)
            last_commit = time.monotonic()
        # 进度上报必须在 commit **之后**：tick 用自己的连接写 tasks 表，
        # 主连接若还握着未提交的写事务，两个连接互等 → 进度全被跳过
        # （2026-10-19 实测：文件在一个个处理，界面永远是 0/N，还白等 1 秒超时）。
        T.tick()
"""
assert old in s, "extract pattern not found"
io.open(p, "w", encoding="utf-8", newline="\n").write(s.replace(old, new, 1))
print("extract patched")

# ---- encode.py ----
p2 = "src/encode.py"
s2 = io.open(p2, encoding="utf-8").read()
old2 = """        done_n = min(i + group_size, len(rows))
        T.tick(len(group), total=len(rows))
"""
new2 = """        done_n = min(i + group_size, len(rows))
"""
assert old2 in s2, "encode tick anchor not found"
s2 = s2.replace(old2, new2, 1)
old3 = """            D.retry_on_lock(conn.commit)
            last_commit = time.monotonic()
            LOG.info("  进度 %d/%d"""
new3 = """            D.retry_on_lock(conn.commit)
            last_commit = time.monotonic()
        T.tick(len(group), total=len(rows))
        if done_n % 100 < group_size or done_n == len(rows):
            LOG.info("  进度 %d/%d"""
assert old3 in s2, "encode commit anchor not found"
s2 = s2.replace(old3, new3, 1)
io.open(p2, "w", encoding="utf-8", newline="\n").write(s2)
print("encode patched")
