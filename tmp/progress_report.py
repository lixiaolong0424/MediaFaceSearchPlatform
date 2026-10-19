"""当前执行程度 + 基于**实际轮次耗时**的 ETA（不再靠外推猜）。

数据来源：
  * data/library.db      —— 权威状态（files / stage_status / file_encoding / face_records）
  * logs/chain_state.tsv —— 每轮的 START/END 时间戳（链条自己写的）
  * logs/chain_r*_extract_photo.log —— 每轮实际处理了多少文件、多少重复
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime
from pathlib import Path

ROOT = Path(r'D:\DATA\PROJECT\建设多媒体整理系统')
LOGS = ROOT / "logs"


def fmt_secs(s: float) -> str:
    if s < 3600:
        return f"{s/60:.0f} 分钟"
    return f"{s/3600:.1f} 小时"


print("=" * 74)
print(f"执行程度报告   {datetime.now():%Y-%m-%d %H:%M:%S}")
print("=" * 74)

conn = sqlite3.connect(str(ROOT / "data" / "library.db"))
conn.row_factory = sqlite3.Row

# ---------------------------------------------------------------- 1. 素材总览
print("\n【1】素材总览")
by = {}
for r in conn.execute("SELECT kind, status, COUNT(*) n FROM files GROUP BY kind,status"):
    by[(r["kind"], r["status"])] = r["n"]
photo = {k[1]: v for k, v in by.items() if k[0] == "photo"}
video = {k[1]: v for k, v in by.items() if k[0] == "video"}
p_tot, v_tot = sum(photo.values()), sum(video.values())
print(f"  照片 {p_tot:,}：done {photo.get('done',0):,} · 判重跳过 {photo.get('skipped_dup',0):,}"
      f" · 待处理 {photo.get('pending',0):,} · 失败 {photo.get('error',0):,}")
print(f"  视频 {v_tot:,}：done {video.get('done',0):,} · 判重跳过 {video.get('skipped_dup',0):,}"
      f" · 待处理 {video.get('pending',0):,} · 失败 {video.get('error',0):,}")
processed = photo.get("done", 0) + photo.get("skipped_dup", 0) + photo.get("error", 0)
print(f"  → 照片已处理 {processed:,} / {p_tot:,} = {100*processed/p_tot:.1f}%")
done_pct = 100 * photo.get("done", 0) / p_tot
print(f"     其中真正入库（done）{100*done_pct:.1f}%（其余是近重复，按设计不重复入库）")

# ---------------------------------------------------------------- 2. 各阶段
print("\n【2】各阶段完成数")
st = {}
for r in conn.execute("SELECT stage, status, COUNT(*) n FROM stage_status GROUP BY stage,status"):
    st[(r["stage"], r["status"])] = r["n"]
for stage in ("dedup", "extract", "face_detect", "face_encode", "semantic_encode"):
    d = st.get((stage, "done"), 0)
    e = st.get((stage, "error"), 0)
    print(f"  {stage:<16} done {d:>8,}   error {e}")

# ---------------------------------------------------------------- 3. 人脸
print("\n【3】人脸库（照片墙数据源）")
alive = conn.execute("SELECT COUNT(*) FROM face_records WHERE status='alive'").fetchone()[0]
vec = conn.execute(
    "SELECT COUNT(*) FROM face_records WHERE status='alive' AND has_vector=1").fetchone()[0]
files_with_face = conn.execute(
    "SELECT COUNT(DISTINCT file_id) FROM face_records WHERE status='alive'").fetchone()[0]
print(f"  人脸总数 {alive:,}（带向量 {vec:,}，可在墙上显示）")
print(f"  来源文件 {files_with_face:,} 个 → 平均每文件 {alive/max(1,files_with_face):.1f} 张脸")

# ---------------------------------------------------------------- 4. 轮次历史
print("\n【4】轮次历史（链条自己写的 chain_state.tsv + 每轮日志）")
state = LOGS / "chain_state.tsv"
rounds = {}
if state.exists():
    for line in state.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        ts, msg = parts[0], parts[1]
        m = re.search(r"第 (\d+) 轮开始", msg)
        if m:
            rounds.setdefault(int(m.group(1)), {})["start"] = ts
        m = re.search(r"END\s+r(\d+)_face_encode", msg)
        if m:
            rounds.setdefault(int(m.group(1)), {})["face_end"] = ts

rows = []
for ix in sorted(rounds):
    r = rounds[ix]
    log = LOGS / f"chain_r{ix}_extract_photo.log"
    n_proc = n_dup = 0
    rate = 0.0
    if log.exists():
        txt = log.read_text(encoding="utf-8", errors="replace")
        prog = re.findall(
            r"进度 (\d+)/(\d+)\s+完成 (\d+) 重复跳过 (\d+) 失败 (\d+).*?([\d.]+) 文件/秒", txt)
        if prog:
            last = prog[-1]
            n_proc = int(last[2]) + int(last[3])
            n_dup = int(last[3])
            rate = float(last[5])
    dur = ""
    if r.get("start") and r.get("face_end"):
        t0 = datetime.fromisoformat(r["start"])
        t1 = datetime.fromisoformat(r["face_end"])
        dur = fmt_secs((t1 - t0).total_seconds())
    rows.append((ix, n_proc, n_dup, rate, dur))

for ix, n_proc, n_dup, rate, dur in rows:
    tag = "← 进行中" if ix == max(rounds) and not rounds[ix].get("face_end") else ""
    print(f"  第 {ix} 轮：处理 {n_proc:>6,} 个（判重 {n_dup:>5,}）"
          f" 末速 {rate:>4.1f} 张/秒  耗时 {dur:<10} {tag}")

finished = [r for r in rows if r[4]]
secs = []
for ix, *_rest, dur in finished:
    if rounds[ix].get("start") and rounds[ix].get("face_end"):
        t0 = datetime.fromisoformat(rounds[ix]["start"])
        t1 = datetime.fromisoformat(rounds[ix]["face_end"])
        secs.append((t1 - t0).total_seconds())
avg_round = sum(secs) / len(secs) if secs else 0
avg_files = sum(r[1] for r in finished) / len(finished) if finished else 25000

# ---------------------------------------------------------------- 5. ETA
print("\n【5】ETA（按实测轮次耗时外推）")
pend = photo.get("pending", 0) + photo.get("error", 0)
if avg_round and avg_files:
    print(f"  已完成 {len(finished)} 轮，平均每轮 {fmt_secs(avg_round)}，"
          f"每轮约 {avg_files:,.0f} 个文件")
    remain_rounds = pend / avg_files
    eta = remain_rounds * avg_round
    print(f"  照片剩余 {pend:,} 个 → 约 {remain_rounds:.1f} 轮 → **{fmt_secs(eta)}**")
    print(f"  （每轮内部是「抽帧 ∥ 语义编码」并行，取两者中较慢的那个）")
else:
    print("  轮次数据还不足，无法外推")
print(f"  视频 {video.get('pending',0):,} 个：照片全部完成后才开始，预计数天")

# ---------------------------------------------------------------- 6. 双阶段一致性
print("\n【6】两个模型的两阶段入库（铁律 3 / 铁律 1）")
for r in conn.execute("SELECT model,status,COUNT(*) n FROM file_encoding GROUP BY model,status "
                      "ORDER BY model,n DESC"):
    print(f"  {r['model']:<9} {r['status']:<12} {r['n']:>8,}")
dl = conn.execute("SELECT COUNT(*) FROM dead_letter").fetchone()[0]
print(f"  死信 {dl}")
conn.close()

# ---------------------------------------------------------------- 7. 磁盘
print("\n【7】NVMe 派生数据 / 预算")
tot = 0
for d in ("frames", "faces/raw", "faces/thumbs", "embeddings", "thumbs"):
    p = ROOT / "data" / "nvme" / d
    if p.exists():
        fs = [f for f in p.rglob("*") if f.is_file()]
        s = sum(f.stat().st_size for f in fs)
        tot += s
        print(f"  {d:<14} {len(fs):>8,} 个  {s/1e9:>7.2f} GB")
print(f"  合计 {tot/1e9:.1f} GB / 300 GB 预算（{100*tot/300e9:.0f}%）")
db = ROOT / "data" / "library.db"
if db.exists():
    print(f"  library.db {db.stat().st_size/1e9:.2f} GB")
