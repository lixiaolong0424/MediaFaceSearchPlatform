"""Qwen3-VL-Reranker-2B 加载 + 单对延迟实测（决定 05_rerank.py 的设计）。

为什么必须先测：vendored 的 `qwen3_vl_reranker.py::process()` 是
    for pair in pairs:
        inputs = self.tokenize([pair])      # ← 一次只处理一对
        scores = self.compute_scores(inputs)
**没有批处理**。所以「召回 400 → 重排 → Top-50」的真实代价 = 400 × 单对延迟。
这个数字直接决定：重排要不要做成默认开启、默认重排多少条、以及是否要放进在线检索路径。

用法：
    .venv\\Scripts\\python.exe tmp\\bench_reranker.py [pairs]
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

MODEL = ROOT / "weight" / "Qwen3-VL-Reranker-2B"
N = int(sys.argv[1]) if len(sys.argv) > 1 else 16


def main() -> int:
    import torch

    # 拿 N 张已抽帧的照片缓存图当 document
    conn = sqlite3.connect(str(ROOT / "data" / "library.db"))
    fids = [int(r[0]) for r in conn.execute(
        "SELECT file_id FROM files WHERE kind='photo' AND status='done' "
        "ORDER BY file_id LIMIT 4000 OFFSET 2000")]
    conn.close()
    frames_root = ROOT / "data" / "nvme" / "frames"
    docs = []
    for f in fids:
        d = frames_root / str(f)
        if d.exists():
            jpgs = sorted(d.glob("0000_*.jpg"))
            if jpgs:
                docs.append(str(jpgs[0]))
        if len(docs) >= N:
            break
    print(f"document 图 {len(docs)} 张（NVMe 缓存图）")

    spec = importlib.util.spec_from_file_location("q3vlr", MODEL / "qwen3_vl_reranker.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]

    t0 = time.monotonic()
    r = mod.Qwen3VLReranker(
        model_name_or_path=str(MODEL),
        dtype=torch.bfloat16,
        attn_implementation="sdpa",
        local_files_only=True,
    )
    load = time.monotonic() - t0
    print(f"加载 {load:.1f}s，显存 {torch.cuda.memory_allocated()/1e9:.2f} GB，"
          f"MAX_PIXELS={mod.MAX_PIXELS}（≈{mod.MAX_PIXELS/(32*32):.0f} token）")

    # 文本 query（最常见）+ 图片 query 两种都量
    for label, query in (
        ("文本 query", {"text": "穿红色衣服的人"}),
        ("图片 query", {"image": docs[0]}),
    ):
        docs_payload = [{"image": p} for p in docs]
        t1 = time.monotonic()
        scores = r.process({"query": query, "documents": docs_payload})
        el = time.monotonic() - t1
        per = el / max(1, len(docs_payload))
        print(f"  {label:<10} {len(scores)} 对 / {el:6.2f}s = {per*1000:6.0f} ms/对"
              f"   → 400 对需 {per*400:6.1f}s，50 对需 {per*50:5.1f}s")
        print(f"             分数范围 {min(scores):.4f} ~ {max(scores):.4f}")
        del scores
        torch.cuda.empty_cache()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
