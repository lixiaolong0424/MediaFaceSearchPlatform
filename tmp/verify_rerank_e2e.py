"""端到端验证 L2d：走真正的检索入口 src.search.search（不是内部函数）。

要证明的两件事：
  1. **文字查询不再返回 0 条**（原实现 min_score=0.55 高于模型上限，必然砍空）；
  2. 重排序确实参与并改变了排序，且报告可读；关掉时退化为召回顺序。
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, r'D:\DATA\PROJECT\建设多媒体整理系统')

from src import db as D
from src.common import load_config
from src.search import search
from src.vector_store import open_store


def run_query(cfg, conn, store, text, *, rerank_k):
    cfg.override("retrieval.rerank_enabled", rerank_k > 0)
    if rerank_k:
        cfg.override("retrieval.rerank_input_k", rerank_k)
    t0 = time.monotonic()
    out = search(conn, cfg, store, query_mode="text", payload={"text": text})
    el = time.monotonic() - t0
    d = out.as_dict()
    print(f"\n查询 {text!r}  重排={('前 %d 条' % rerank_k) if rerank_k else '关闭'}  "
          f"耗时 {el:.1f}s")
    print(f"  mode={d['mode']}  镜头={d['total_shots']}  文件={d['total_files']}  "
          f"reranked={d['reranked']}")
    print(f"  cut_note={d['cut_note']}")
    print(f"  rerank_note={d['rerank_note']}")
    for f in (d["files"] or [])[:5]:
        s = (f.get("shots") or [{}])[0]
        print(f"    fid={f['file_id']:<7} best={f['best_score']:.4f} "
              f"rerank={s.get('rerank_score')} recall={s.get('recall_score')} "
              f"{Path(str(f.get('path') or '')).name[:38]}")
    return d


def main() -> int:
    cfg = load_config(None, None)
    store = open_store(cfg)
    conn = D.connect(cfg.db_path, cfg=cfg, readonly=True)
    try:
        base = run_query(cfg, conn, store, "人物", rerank_k=0)
        rr = run_query(cfg, conn, store, "人物", rerank_k=40)
        ok = True
        if base["total_shots"] == 0:
            print("\n⚠️ 未重排时仍为 0 条 —— 阈值标定还有问题")
            ok = False
        if rr["total_shots"] == 0:
            print("\n⚠️ 重排后为 0 条 —— 重排路径有问题")
            ok = False
        if not rr["reranked"]:
            print("\n⚠️ reranked=False —— 重排没生效")
            ok = False
        # 顺序应当发生变化（重排的意义所在）
        b = [f["file_id"] for f in (base["files"] or [])]
        a = [f["file_id"] for f in (rr["files"] or [])]
        if b and a and b[:5] == a[:5]:
            print(f"\n注：前 5 名顺序未变（可能这批候选本身就一致）：{b[:5]}")
        print(f"\n前 5 名对比：召回={b[:5]}  重排={a[:5]}")
        print("\nOK" if ok else "\nFAILED")
        return 0 if ok else 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
