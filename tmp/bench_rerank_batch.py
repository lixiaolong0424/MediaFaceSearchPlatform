"""重排批大小对吞吐的影响，并验证「批大小不改变分数」。

为什么要专门验证「不改变分数」：
vendored `tokenize` 会把一批里的所有对 padding 到该批最长，
所以批大小变了、padding 长度就变了 —— 必须确认打分只取决于序列本身
（`compute_scores` 取 `last_hidden_state[:, -1]`，而 processor 是 `padding_side='left'`，
最后一列应当始终是真实末 token）。若分数随批大小漂移，就不能靠调批大小提速。

用法：
    .venv\\Scripts\\python.exe tmp\\bench_rerank_batch.py
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
N = 24


def main() -> int:
    import torch

    conn = sqlite3.connect(str(ROOT / "data" / "library.db"))
    fids = [int(r[0]) for r in conn.execute(
        "SELECT file_id FROM files WHERE kind='photo' AND status='done' "
        "ORDER BY file_id LIMIT 6000 OFFSET 1000")]
    conn.close()
    fr = ROOT / "data" / "nvme" / "frames"
    docs = []
    for f in fids:
        d = fr / str(f)
        if d.exists():
            j = sorted(d.glob("0000_*.jpg"))
            if j:
                docs.append(str(j[0]))
        if len(docs) >= N:
            break
    print(f"文档图 {len(docs)} 张，batch 对比")

    spec = importlib.util.spec_from_file_location("q3vlr", MODEL / "qwen3_vl_reranker.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    r = mod.Qwen3VLReranker(model_name_or_path=str(MODEL), dtype=torch.bfloat16,
                            attn_implementation="sdpa", local_files_only=True)
    torch.cuda.empty_cache()
    q = {"text": "一个人"}
    pairs_all = [r.format_mm_instruction(
        q.get("text"), None, None, None, p, None,
        instruction=r.default_instruction) for p in docs]

    base = None
    for bs in (1, 2, 4, 8, 16):
        scores = []
        t0 = time.monotonic()
        try:
            with torch.inference_mode():
                for i in range(0, len(pairs_all), bs):
                    chunk = pairs_all[i:i + bs]
                    inputs = r.tokenize(chunk)
                    inputs = inputs.to(r.model.device)
                    scores.extend(float(x) for x in r.compute_scores(inputs))
                    del inputs
        except RuntimeError as exc:
            print(f"  batch={bs:>2}  失败（很可能是显存）：{str(exc)[:90]}")
            torch.cuda.empty_cache()
            continue
        el = time.monotonic() - t0
        per = el / len(scores)
        if base is None:
            base = scores
            diff = 0.0
        else:
            diff = max(abs(a - b) for a, b in zip(base, scores))
        print(f"  batch={bs:>2}  {el:6.2f}s  {per*1000:6.0f} ms/对  "
              f"{len(scores)/el:5.2f} 对/秒  → 60 条 {per*60:5.1f}s / 400 条 {per*400:6.1f}s"
              f"  |与 batch=4 的最大分数差 {diff:.2e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
