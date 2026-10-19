"""端到端验证三个功能，并给出与界面逐项对应的证据。

覆盖：
  A. 照片墙（Tab1）：sqlite 分页 / 概览去重 / 时间筛选 + **缩略图真的能通过静态服务拿到**
     （这一步最关键 —— 数据对但图片 404 的话，墙上就是一片空框）
  B. 检索（Tab2）：文字查询 → 语义召回 → 重排序 → 截断
  C. 以图搜图 / 以视频搜自身：查询素材来自库内，相似度应接近 1
  D. 精确找人 + 找同款：face_id 驱动的 ArcFace 检索

只读，不改库；可以直接在流水线跑着的时候执行。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(r'D:\DATA\PROJECT\建设多媒体整理系统')
sys.path.insert(0, str(ROOT))

from src import db as D  # noqa: E402
from src import ui_wall  # noqa: E402
from src.common import load_config  # noqa: E402
from src.search import search, search_by_face_id  # noqa: E402
from src.static_serve import get_server  # noqa: E402
from src.vector_store import open_store  # noqa: E402

cfg = load_config(None, None)
conn = D.connect(cfg.db_path, cfg=cfg, readonly=True)
store = open_store(cfg)
ok = True


def head(t: str) -> None:
    print("\n" + "=" * 74)
    print(t)
    print("=" * 74)


# --------------------------------------------------------------------------- #
head("A. 照片墙（Tab1）")
page = ui_wall.wall_page(conn, cfg, limit=12, mode="tile", min_quality=0.0)
print(f"  平铺模式：total={page['total']:,}  本页 {len(page['items'])} 条  "
      f"has_more={page['has_more']}")
for it in page["items"][:4]:
    print(f"    face#{it['face_id']:<6} fid={it['file_id']:<7} {it['kind']:<5} "
          f"q={it['quality']:.3f} {it['timecode']}")
ov = ui_wall.wall_page(conn, cfg, limit=12, mode="overview", min_quality=0.0)
print(f"  概览模式（局部向量去重）：total={ov['total']:,} → 本页 {len(ov['items'])} 条"
      f"（去重率 {100*(1-len(ov['items'])/max(1,len(page['items']))):.0f}% 本页内）")
fac = ui_wall.wall_facets(conn, cfg)
print(f"  时间范围：{fac['date_range'][0]} → {fac['date_range'][1]}")
print(f"  来源文件数（facets）：{len(fac['files'])}  人脸总数：{fac['faces_total']:,}")
# 时间筛选
mid = "2020-01-01"
tf = ui_wall.wall_page(conn, cfg, limit=5, mode="tile", min_quality=0.0, t_start=mid)
print(f"  时间筛选 t_start={mid}：total={tf['total']:,}")

# 缩略图能否真的取到（静态服务 + 白名单）
# 注意 get_server() 返回的是 StaticServer 对象，要自己调 .start()（app.py 就是这么做的）；
# 它可能已经在跑（和界面共用同一个端口），start() 会幂等地报「已在运行」。
srv = get_server(cfg)
srv_ok, srv_msg = srv.start()
print(f"  静态服务：ok={srv_ok} {srv_msg}")
import urllib.request  # noqa: E402

hit = 0
for it in page["items"][:3]:
    tp = it.get("thumb_path")
    if not tp:
        print(f"    face#{it['face_id']} 无 thumb_path")
        continue
    url = srv.thumb_url(tp)
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            body = r.read()
        print(f"    face#{it['face_id']}  HTTP {r.status}  {len(body)} bytes  {url}")
        hit += 1
    except Exception as exc:  # noqa: BLE001
        print(f"    face#{it['face_id']}  取图失败：{exc}  {url}")
        ok = False
print(f"  → {hit}/3 张缩略图可通过静态服务取到"
      + ("（界面上就是能看到的图）" if hit else "（界面上会是空框！）"))

# --------------------------------------------------------------------------- #
head("B. 检索（Tab2）：文字 → 召回 → 重排序 → 截断")
for text in ("人物", "室内"):
    cfg.override("retrieval.rerank_enabled", True)
    cfg.override("retrieval.rerank_input_k", 40)
    t0 = time.monotonic()
    out = search(conn, cfg, store, query_mode="text", payload={"text": text})
    d = out.as_dict()
    print(f"\n  查询 {text!r}  耗时 {time.monotonic()-t0:.1f}s  "
          f"mode={d['mode']}  reranked={d['reranked']}")
    print(f"    {d['rerank_note']}")
    print(f"    命中 {d['total_shots']} 个镜头 / {d['total_files']} 个文件  "
          f"截断：{d['cut_note']}")
    for f in (d["files"] or [])[:3]:
        s = (f.get("shots") or [{}])[0]
        print(f"      fid={f['file_id']:<7} rerank={s.get('rerank_score')} "
              f"recall={s.get('recall_score')} {Path(str(f.get('path') or '')).name[:34]}")
    if d["total_shots"] == 0:
        ok = False
        print("      ⚠️ 返回 0 条")

# --------------------------------------------------------------------------- #
head("C. 以图搜图 / 以视频搜自身（查询素材取自库内，相似度应 ≈1）")
row = conn.execute(
    "SELECT f.file_id, f.path, f.kind FROM files f WHERE f.kind='photo' "
    "AND f.status='done' AND f.path NOT LIKE '%\"%' LIMIT 1").fetchone()
if row:
    cfg.override("retrieval.rerank_enabled", True)
    cfg.override("retrieval.rerank_input_k", 20)
    out = search(conn, cfg, store, query_mode="image", payload={"image": Path(str(row["path"]))})
    d = out.as_dict()
    top = (d["files"] or [{}])[0]
    print(f"  查询图 file_id={row['file_id']}")
    print(f"    {d['rerank_note']}")
    print(f"    top1 file_id={top.get('file_id')}  best_score={top.get('best_score')}")
    hit_self = int(top.get("file_id") or -1) == int(row["file_id"])
    print(f"    是否命中自身：{hit_self}   自身相似度：{top.get('best_score')}")
    if not hit_self:
        ok = False

vrow = conn.execute(
    "SELECT file_id, path FROM files WHERE kind='video' AND status='done' LIMIT 1").fetchone()
if vrow:
    out = search(conn, cfg, store, query_mode="video", payload={"video": Path(str(vrow["path"]))})
    d = out.as_dict()
    top = (d["files"] or [{}])[0]
    print(f"\n  查询视频 file_id={vrow['file_id']}")
    print(f"    {d['rerank_note']}")
    print(f"    top1 file_id={top.get('file_id')}  best_score={top.get('best_score')}")
else:
    print("\n  （库里还没有已完成的视频，跳过视频查询 —— 视频链要等照片跑完）")

# --------------------------------------------------------------------------- #
head("D. 精确找人（face_id） + 找同款")
fr = conn.execute(
    "SELECT fr.face_id, fr.file_id, fr.quality FROM face_records fr "
    "WHERE fr.status='alive' AND fr.has_vector=1 ORDER BY fr.quality DESC LIMIT 1").fetchone()
if fr:
    fid = int(fr["face_id"])
    t0 = time.monotonic()
    out = search_by_face_id(conn, cfg, store, fid, k=200)
    d = out.as_dict()
    print(f"  以 face_id={fid}（quality={fr['quality']}，来自 file_id={fr['file_id']}）找同款")
    print(f"    耗时 {time.monotonic()-t0:.1f}s  命中 {d['total_shots']} 个镜头 / "
          f"{d['total_files']} 个文件  badge={d['badge']}")
    for f in (d["files"] or [])[:3]:
        print(f"      fid={f['file_id']:<7} score={f['best_score']:.4f} "
              f"{Path(str(f.get('path') or '')).name[:34]}")
    if not d["files"]:
        ok = False
else:
    print("  人脸库为空")
    ok = False

# 头像反跳：墙上点「找同款」用的是同一个 face_id 通道
print("\n  头像反跳链路：墙上卡片 → st.query_params['face_id'] → Tab2 自动执行找同款"
      f"（上面已验证 face_id={int(fr['face_id']) if fr else '?'} 可查）")

conn.close()
print("\n" + "=" * 74)
print("结论：" + ("三个功能全部可用 ✅" if ok else "有项目未通过 ❌（见上面 ⚠️）"))
print("=" * 74)
raise SystemExit(0 if ok else 1)
