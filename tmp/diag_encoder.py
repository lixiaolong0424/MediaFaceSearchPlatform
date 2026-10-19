"""诊断：Qwen3-VL-Embedding-2B 加载与编码耗时/设备定位。

用法：.venv\\Scripts\\python.exe tests\diag_encoder.py [--backend qwen_transformers|sentence_transformers]
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from src.common import load_config  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default=None)
    ap.add_argument("--overlay", default=str(ROOT / "tests" / "config_smoke.yaml"))
    args = ap.parse_args()

    import torch

    print(f"torch={torch.__version__} cuda_avail={torch.cuda.is_available()}")
    if torch.cuda.is_available():
        print(f"  device0={torch.cuda.get_device_name(0)} "
              f"mem_alloc={torch.cuda.memory_allocated()/1e9:.2f}GB")

    cfg = load_config(None, None, overlay=[args.overlay])
    print(f"model_path={cfg.get('semantic.model_path')}")
    print(f"dtype={cfg.get('semantic.dtype')} batch={cfg.get('semantic.batch_size')}")

    from src.encode_model import SemanticEncoder

    enc = SemanticEncoder(cfg, backend=args.backend)
    print(f"resolved device(before load)={enc.device}")

    t0 = time.monotonic()
    st = enc.load()
    t_load = time.monotonic() - t0
    print(f"--- load ok={st.loaded} backend={st.backend} device={st.device} "
          f"dim={enc.dimension} elapsed={t_load:.1f}s")
    if not st.loaded:
        print("ERROR:", st.error)
        return 1

    if torch.cuda.is_available():
        print(f"  cuda mem after load: {torch.cuda.memory_allocated()/1e9:.2f}GB")

    texts = ["一只猫在草地上", "a cat on the grass", "汽车在高速公路上行驶"]
    t0 = time.monotonic()
    vecs = enc.encode_texts(texts)
    t_enc = time.monotonic() - t0
    a = np.asarray(vecs, dtype="float32")
    print(f"--- encode 3 texts: {t_enc:.2f}s  shape={a.shape}")
    print(f"    norms={np.round(np.linalg.norm(a, axis=1), 4).tolist()}")
    print(f"    finite={bool(np.isfinite(a).all())}")
    print(f"    cos(cat_zh,cat_en)={float(a[0].dot(a[1])):.4f}")
    print(f"    cos(cat_zh,car)   ={float(a[0].dot(a[2])):.4f}")
    print(f"    cos(cat_en,car)   ={float(a[1].dot(a[2])):.4f}")

    # 图像编码
    img = ROOT / "tests" / "media_smoke" / "dup_a.jpg"
    if img.exists():
        t0 = time.monotonic()
        iv = enc.encode_images([img])
        print(f"--- encode 1 image: {time.monotonic()-t0:.2f}s shape={np.asarray(iv).shape}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
