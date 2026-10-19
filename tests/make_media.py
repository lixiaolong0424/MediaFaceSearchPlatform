"""生成 P0 冒烟测试用的合成素材（照片 + 近重复 + 中文/空格/括号/& 路径 + 过小文件）。"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

BASE = Path(r"D:\DATA\PROJECT\建设多媒体整理系统\tests\media_smoke")
SUB = BASE / "sub 目录 (括号)&特殊"


def mk(path: Path, seed: int, w: int = 800, h: int = 600, label: str = "") -> Path:
    rng = np.random.default_rng(seed)
    arr = (rng.random((h, w, 3)) * 60 + 100).astype("uint8")
    im = Image.fromarray(arr)
    d = ImageDraw.Draw(im)
    d.rectangle([w // 4, h // 4, 3 * w // 4, 3 * h // 4], outline=(255, 0, 0), width=8)
    d.text((20, 20), label or path.name, fill=(0, 0, 0))
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path, quality=92)
    return path


def main() -> None:
    if BASE.exists():
        import shutil

        shutil.rmtree(BASE)
    BASE.mkdir(parents=True, exist_ok=True)
    SUB.mkdir(parents=True, exist_ok=True)

    mk(BASE / "IMG_20230101_120000.jpg", 1, label="A")
    mk(BASE / "IMG_20230615_093000.jpg", 2, label="B")

    # 近重复：同一图重编码 + 轻微缩放（真实场景里「同一张图的不同版本」就是这样），
    # pHash 汉明距离应 <= 8。注意：不要用「加随机噪声」制造近重复——
    # 随机扰动会同时改变 DCT 系数，pHash 距离反而很大（实测 >= 14）。
    src = mk(BASE / "dup_a.jpg", 3, w=800, h=600, label="DUP")
    with Image.open(src) as im:
        im2 = im.convert("RGB").resize((760, 570), Image.LANCZOS)
        im2.save(BASE / "dup_b.jpg", quality=72)

    # 中文 + 空格 + 括号 + & 的路径（验收项 ②）
    mk(SUB / "中文 名称 (1) & 测试.jpg", 4, label="CJK")

    # 文件名日期解析
    mk(BASE / "2024-03-05 18-22-33.png", 5, label="DATE")

    # 过小文件（应被 library.min_photo_bytes=2048 过滤）
    (BASE / "tiny.jpg").write_bytes(b"\xff\xd8\xff\xd9")

    print("images done:", sum(1 for _ in BASE.rglob("*") if _.is_file()))


if __name__ == "__main__":
    main()
