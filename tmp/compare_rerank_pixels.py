"""重排序像素预算对**真实图片候选排序**的影响。

为什么必须单独测：自检里的「相关=0.5469 / 无关=0.1953」是**纯文本对**，
像素预算根本不参与，所以自检在所有预算下都一样 —— 它证明不了图片侧的质量。
本脚本对同一批真实候选分别在 1280*32*32 与更低预算下打分，比较：
  * 逐条分数的 Pearson / Spearman 相关
  * Top-10 / Top-20 的顺序重合度（重排的用处就是重排前几名，这个必须稳住）
  * 分数均值偏移（尺度是否被压缩）

用法：
    .venv\\Scripts\\python.exe tmp\\compare_rerank_pixels.py
"""

from __future__ import annotations

import gc
import importlib.util
import sys

import numpy as np

ROOT = r'D:\DATA\PROJECT\建设多媒体整理系统'
sys.path.insert(0, ROOT)

import torch  # noqa: E402

from src import db as D  # noqa: E402
from src.common import load_config  # noqa: E402
from src.encode_model import encode_query  # noqa: E402
from src.rerank import doc_media_for_shot  # noqa: E402
from src.search import enrich, semantic_recall  # noqa: E402
from src.vector_store import open_store  # noqa: E402

FACTOR = 32
N_DOCS = 40
QUERY = "人物"
BUDGETS = [1280 * FACTOR ** 2, 768 * FACTOR ** 2, 512 * FACTOR ** 2, 384 * FACTOR ** 2]


def spearman(a, b):
    ra = np.argsort(np.argsort(a))
    rb = np.argsort(np.argsort(b))
    ra = ra - ra.mean()
    rb = rb - rb.mean()
    den = float(np.linalg.norm(ra) * np.linalg.norm(rb))
    return float(ra.dot(rb) / den) if den else float("nan")


def main() -> int:
    cfg = load_config(None, None)
    store = open_store(cfg)
    conn = D.connect(cfg.db_path, cfg=cfg, readonly=True)
    vec, _ = encode_query(cfg, text=QUERY)
    shots = semantic_recall(conn, cfg, store, query_vec=vec, recall_k=200)
    enrich(conn, cfg, shots)
    docs, kept = [], []
    for h in shots[:N_DOCS]:
        d = doc_media_for_shot(h)
        if d:
            docs.append(d)
            kept.append(h)
    conn.close()
    print(f"查询 {QUERY!r}，候选 {len(docs)} 条（1280*32*32 为基准）")

    script = f"{ROOT}/weight/Qwen3-VL-Reranker-2B/qwen3_vl_reranker.py"
    spec = importlib.util.spec_from_file_location("q3vlr", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]

    results = {}
    for px in BUDGETS:
        impl = mod.Qwen3VLReranker(
            model_name_or_path=f"{ROOT}/weight/Qwen3-VL-Reranker-2B",
            dtype=torch.bfloat16, attn_implementation="sdpa",
            local_files_only=True, max_pixels=int(px),
        )
        scores = []
        with torch.inference_mode():
            for i in range(0, len(docs), 8):
                chunk = docs[i:i + 8]
                pairs = [impl.format_mm_instruction(
                    QUERY, None, None, None, d.get("image"), None,
                    instruction=impl.default_instruction) for d in chunk]
                inputs = impl.tokenize(pairs).to(impl.model.device)
                scores.extend(impl.compute_scores(inputs))
        results[px] = np.asarray(scores, dtype="float32")
        print(f"  {px // FACTOR**2:>4}*32*32: mean={results[px].mean():.4f} "
              f"std={results[px].std():.4f} max={results[px].max():.4f}", flush=True)
        del impl
        gc.collect()
        torch.cuda.empty_cache()

    base = results[BUDGETS[0]]
    order_base = np.argsort(-base)
    print("\n=== 与 1280*32*32 的对比 ===")
    for px in BUDGETS[1:]:
        s = results[px]
        pear = float(np.corrcoef(base, s)[0, 1])
        spear = spearman(base, s)
        order = np.argsort(-s)
        top10 = len(set(order_base[:10].tolist()) & set(order[:10].tolist()))
        top20 = len(set(order_base[:20].tolist()) & set(order[:20].tolist()))
        print(f"  {px // FACTOR**2:>4}*32*32: Pearson={pear:.4f} Spearman={spear:.4f} "
              f"Top10 重合={top10}/10 Top20 重合={top20}/20")

    print("\n=== 前 10 名（1280 基准 vs 512）===")
    o512 = np.argsort(-results[512 * FACTOR ** 2])
    for i in range(10):
        a, b = order_base[i], o512[i]
        print(f"  #{i+1}  1280→fid={kept[a].file_id} ({base[a]:.4f})   "
              f"512→fid={kept[b].file_id} ({results[512*FACTOR**2][b]:.4f})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
