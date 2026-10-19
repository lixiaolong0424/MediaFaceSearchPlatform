"""诊断：用与生产完全相同的 SemanticEncoder 路径，测加载/文本编码/图像编码耗时。

关键：-u 无缓冲 + 逐行 flush，避免误判为卡死。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")


def step(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def main() -> int:
    from src.common import load_config

    cfg = load_config(None, None, overlay=[str(ROOT / "tests" / "config_smoke.yaml")])

    from src.encode_model import SemanticEncoder

    step("构造 SemanticEncoder 并 load()")
    enc = SemanticEncoder(cfg)
    t = time.monotonic()
    st = enc.load()
    step(f"load ok={st.loaded} backend={st.backend} device={st.device} "
         f"dim={enc.dimension} elapsed={time.monotonic()-t:.1f}s")
    if not st.loaded:
        step(f"ERROR: {st.error}")
        return 1

    step("encode_texts x2")
    t = time.monotonic()
    v = enc.encode_texts(["一只猫在草地上", "汽车在高速公路上行驶"])
    step(f"  ok shape={len(v)}x{len(v[0])} elapsed={time.monotonic()-t:.1f}s "
         f"cos={float(sum(a*b for a, b in zip(v[0], v[1]))):.4f}")

    imgs = sorted((ROOT / "tests" / "media_smoke").glob("*.jpg"))[:2]
    if imgs:
        step(f"encode_images x{len(imgs)}: {[p.name for p in imgs]}")
        t = time.monotonic()
        iv = enc.encode_images(imgs)
        step(f"  ok shape={len(iv)}x{len(iv[0])} elapsed={time.monotonic()-t:.1f}s")

        step("混合：图片 vs 文本 相似度（检查跨模态是否有意义）")
        sims = [sum(a * b for a, b in zip(iv[0], enc.encode_texts([q])[0]))
                for q in ("一张照片", "纯色噪点图")]
        step(f"  sims={[round(s, 4) for s in sims]}")
    step("完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
