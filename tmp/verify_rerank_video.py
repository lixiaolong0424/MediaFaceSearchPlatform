"""核实 src/rerank.py 里关于「视频查询会被静默降级」的那条论断。

src/rerank.py::build_query_media 的注释声称：
    权重目录自带的 tokenize 会调 process_vision_info(..., return_video_metadata=True)，
    而本机 qwen-vl-utils 不认这个参数，报
        open() got an unexpected keyword argument 'metadata_errors'
    而且 vendored 代码**把异常吞掉了**，退化成一条 "NULL" 文本对，
    于是所有候选拿到相同分数、召回顺序被打乱（实测自身相似度从 ~1.0 掉到 0.459）。

这条论断决定了一个真实的质量取舍（视频查询改成「抽第 1 秒的帧」当图像查询）。
如果它其实不成立，就应该直接用真视频重排，别白丢信息。所以必须验。

用法：
    .venv\\Scripts\\python.exe tmp\\verify_rerank_video.py <某个真实视频路径>
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

MODEL = ROOT / "weight" / "Qwen3-VL-Reranker-2B"


def main() -> int:
    video = sys.argv[1] if len(sys.argv) > 1 else None
    if not video:
        # 从库里挑一个最小的视频，避免解码大文件
        import sqlite3
        conn = sqlite3.connect(str(ROOT / "data" / "library.db"))
        row = conn.execute(
            "SELECT path, size FROM files WHERE kind='video' AND status='pending' "
            "ORDER BY size ASC LIMIT 1").fetchone()
        conn.close()
        if not row:
            print("库里没有视频可用于测试")
            return 2
        video = str(row[0])
    vp = Path(video)
    print(f"测试视频：{vp}  ({vp.stat().st_size/1e6:.1f} MB)")

    # qwen-vl-utils 版本与 process_vision_info 的签名
    import inspect

    import qwen_vl_utils
    from qwen_vl_utils import process_vision_info

    print(f"qwen-vl-utils 版本：{getattr(qwen_vl_utils, '__version__', '未知')}")
    try:
        sig = inspect.signature(process_vision_info)
        print(f"process_vision_info 签名：{sig}")
    except (TypeError, ValueError):
        print("process_vision_info 签名不可读")

    import torch

    spec = importlib.util.spec_from_file_location("q3vlr", MODEL / "qwen3_vl_reranker.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]

    r = mod.Qwen3VLReranker(model_name_or_path=str(MODEL), dtype=torch.bfloat16,
                            attn_implementation="sdpa", local_files_only=True)

    # 1) 直接看 tokenize 对视频对做了什么：真实文本长度 / 是否退化成 NULL
    pair = r.format_mm_instruction(
        None, None, str(vp),          # query = 这个视频
        None, None, str(vp),          # document = 同一个视频（自身匹配，分数应最高）
        instruction=r.default_instruction,
    )
    inputs = r.tokenize([pair])
    ids = inputs["input_ids"][0]
    tokenizer = r.processor.tokenizer
    n_vision = int((ids == r.processor.image_token_id).sum()) if hasattr(r.processor, "image_token_id") else -1
    text = tokenizer.decode(ids, skip_special_tokens=False)
    degraded = "NULL" in text
    print(f"\n[视频对] 序列长度 {len(ids)}  视觉 token {n_vision}  含 'NULL' 退化标记：{degraded}")
    print(f"  前 160 字符：{text[:160]!r}")

    # 2) 和一个纯文本对比较，确认不是所有输入都被降级
    pair_txt = r.format_mm_instruction(
        "一只猫", None, None, "一只趴在窗台上的橘猫", None, None,
        instruction=r.default_instruction)
    inputs_txt = r.tokenize([pair_txt])
    txt = tokenizer.decode(inputs_txt["input_ids"][0], skip_special_tokens=False)
    print(f"\n[文本对] 序列长度 {len(inputs_txt['input_ids'][0])}  含 'NULL'：{'NULL' in txt}")

    # 3) 真视频重排的分数：自身对自身应当明显高
    if not degraded:
        with torch.inference_mode():
            sc = r.compute_scores(inputs.to(r.model.device))
        print(f"\n[视频自身匹配分数] {float(sc[0]):.4f}  （若远低于 0.5 说明不可用）")
    else:
        print("\n→ 视频对确实退化成 NULL 文本对：build_query_media 里「视频改抽帧」的处理是必要的。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
