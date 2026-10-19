"""语义编码像素预算 → 吞吐 的实测对比（决定 config semantic.max_pixels）。

背景（2026-10-16 实测）：checkpoint 自带 MAX_PIXELS = 1800*28*28 ≈ 1.41 MP，
每张图约 1000~1800 个视觉 token，2B 模型单图 ≈0.25s → **只有 4 张/秒**
（batch=16，GPU 利用率 65%，显存 8.9/24GB）。33 万张唯一照片 → 23 小时。

视觉 token 数 ≈ 像素数/(28*28)，所以降 max_pixels 基本线性降算力。
本脚本在同一批 NVMe 缓存图上，对比几档像素预算的吞吐与向量一致性。

用法：
    .venv\\Scripts\\python.exe tmp\\bench_encode_pixels.py
"""

from __future__ import annotations

import gc
import importlib.util
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

IMAGE_FACTOR = 28
N = 48
BATCH = 16


def main() -> int:
    import torch

    conn = sqlite3.connect(str(ROOT / "data" / "library.db"))
    fids = [int(r[0]) for r in conn.execute(
        "SELECT file_id FROM files WHERE kind='photo' AND status='done' "
        "ORDER BY file_id LIMIT 4000 OFFSET 6000")]
    conn.close()
    frames_root = ROOT / "data" / "nvme" / "frames"
    paths = []
    for f in fids:
        d = frames_root / str(f)
        if d.exists():
            jpgs = sorted(d.glob("0000_*.jpg"))
            if jpgs:
                paths.append(jpgs[0])
        if len(paths) >= N:
            break
    print(f"测试图片 {len(paths)} 张（768px 短边 NVMe 缓存）")
    if not paths:
        print("没有缓存图可用")
        return 2

    script = ROOT / "weight" / "Qwen3-VL-Embedding-2B" / "qwen3_vl_embedding.py"
    spec = importlib.util.spec_from_file_location("q3vl", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]

    results = {}
    for label, px in (("1800*28*28 (checkpoint 默认, 1.41MP)", 1800 * IMAGE_FACTOR ** 2),
                      ("1024*28*28 (0.80MP)", 1024 * IMAGE_FACTOR ** 2),
                      ("768*28*28  (0.60MP)", 768 * IMAGE_FACTOR ** 2),
                      ("512*28*28  (0.21MP)", 512 * IMAGE_FACTOR ** 2)):
        t0 = time.monotonic()
        impl = mod.Qwen3VLEmbedder(
            model_name_or_path=str(ROOT / "weight" / "Qwen3-VL-Embedding-2B"),
            dtype=torch.bfloat16, attn_implementation="sdpa",
            local_files_only=True, max_pixels=int(px),
        )
        load = time.monotonic() - t0
        vecs = []
        t1 = time.monotonic()
        with torch.inference_mode():
            for i in range(0, len(paths), BATCH):
                chunk = paths[i:i + BATCH]
                inputs = [{"image": str(p), "instruction": ""} for p in chunk]
                out = impl.process(inputs)
                vecs.extend(out.detach().float().cpu().numpy())
        el = max(1e-9, time.monotonic() - t1)
        fps = len(paths) / el
        results[label] = (fps, vecs)
        print(f"  {label:<40} {fps:6.2f} 张/秒   (加载 {load:.0f}s, 编码 {el:.1f}s)  "
              f"→ 33 万张 {330000 / fps / 3600:5.2f} 小时", flush=True)
        del impl
        gc.collect()
        torch.cuda.empty_cache()

    # 向量一致性：与 checkpoint 默认设置相比的 cosine
    base_label = "1800*28*28 (checkpoint 默认, 1.41MP)"
    base = results[base_label][1]
    import numpy as np

    print("\n=== 与 checkpoint 默认（1.41MP）的向量一致性 ===")
    for label, (fps, vecs) in results.items():
        if label == base_label:
            continue
        sims = []
        for a, b in zip(base, vecs):
            a = np.asarray(a, dtype="float32"); b = np.asarray(b, dtype="float32")
            na, nb = np.linalg.norm(a), np.linalg.norm(b)
            if na > 0 and nb > 0:
                sims.append(float(a.dot(b) / (na * nb)))
        if sims:
            arr = np.asarray(sims)
            print(f"  {label:<40} cos 均值 {arr.mean():.4f}  "
                  f"最小 {arr.min():.4f}  5% 分位 {np.percentile(arr, 5):.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
