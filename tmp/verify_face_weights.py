"""核验 buffalo_l ONNX 权重的真实身份与可用性。

规格第十一章要求 det_10g > 100MB / 2d106det > 10MB，但本机实测：
det_10g.onnx = 16.9MB、2d106det.onnx = 4.8MB、w600k_r50.onnx = 174MB。
本脚本用「读模型图结构 + 真实推理」来判定这些文件到底是完整模型还是残留，
而不是仅凭体积下结论。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import onnx
import onnxruntime as ort

ROOT = Path(__file__).resolve().parent.parent
BASE = ROOT / "weight" / "buffalo_l"


def describe(path: Path) -> None:
    print(f"\n=== {path.name}  {path.stat().st_size / 1048576:.2f} MB ===")
    m = onnx.load(str(path), load_external_data=False)
    print(f"  ir_version={m.ir_version}  opset={[(o.domain or 'ai.onnx', o.version) for o in m.opset_import]}")
    print(f"  nodes={len(m.graph.node)}  initializers={len(m.graph.initializer)}")
    for tag, coll in (("in", m.graph.input), ("out", m.graph.output)):
        for t in coll:
            dims = [(d.dim_value if d.dim_value else (d.dim_param or "?"))
                    for d in t.type.tensor_type.shape.dim]
            print(f"  {tag}: {t.name} {dims}")
    total_params = 0
    for init in m.graph.initializer:
        n = 1
        for d in init.dims:
            n *= int(d)
        total_params += n
    print(f"  参数量 ≈ {total_params / 1e6:.2f} M")


def try_detect(path: Path) -> None:
    print(f"\n--- 真实推理 {path.name} ---")
    try:
        sess = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
        print("  实际 providers:", sess.get_providers())
        inp = sess.get_inputs()[0]
        print(f"  输入: {inp.name} {inp.shape} {inp.type}")
        shape = [d if isinstance(d, int) and d > 0 else 640 for d in inp.shape]
        if len(shape) == 4:
            shape[0] = 1
            shape[1] = 3
        x = np.random.rand(*shape).astype("float32")
        outs = sess.run(None, {inp.name: x})
        print(f"  输出数={len(outs)}  形状={[tuple(o.shape) for o in outs]}")
        # SCRFD 会输出 score/bbox/kps 三组，共 9 或 15 个输出
        if len(outs) >= 6:
            print("  → 输出形态符合 SCRFD 检测器（多尺度 score/bbox/kps）")
        elif len(outs) == 1 and outs[0].shape[-1] == 512:
            print("  → 输出形态符合 ArcFace 识别器（512 维）")
        elif len(outs) == 1 and outs[0].shape[-1] == 212:
            print("  → 输出形态符合 2d106 关键点模型（106*2=212）")
    except Exception as exc:  # noqa: BLE001
        print(f"  !! 推理失败：{type(exc).__name__}: {exc}")


def main() -> int:
    if not BASE.exists():
        print(f"目录不存在：{BASE}")
        return 1
    for f in sorted(BASE.glob("*.onnx")):
        describe(f)
    # 只对能接受固定输入尺寸的两个做真实推理
    for name in ("det_10g.onnx", "w600k_r50.onnx", "2d106det.onnx"):
        p = BASE / name
        if p.exists():
            try_detect(p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
