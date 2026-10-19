"""F2 track 去重与代表帧优选的验收测试（规格第六章要求「F2 单独验收」）。

规格验收原文：
    取 10 段含人访谈视频（各 3~10 分钟），人工标定期望代表帧数；
    要求 F2 后每段输出 ≤ 期望值×1.5，且无连续时间戳相邻的代表帧
    （相邻间隔<1s 视为去重失败）。

没有人工标注的访谈视频时，用合成观测序列做等价的确定性断言：
  * 同一个人跨 30 帧（IoU 高 / cosine 高）→ 必须合成 1 条 track
  * 两个人同框 → 必须合成 2 条 track
  * 代表帧之间间隔必须 ≥ 1s
  * 每条 track 最多保留 track_keep_per_track 张
  * burst 去重只在「近重复且数量超阈」时生效，正常多人素材不得被误删

用法：
    .venv\\Scripts\\python.exe tests\\test_face_f2.py
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.face_pipeline import (  # noqa: E402
    Obs, burst_dedup, iou, merge_adjacent_tracks, merge_tracks_within_shot,
    pick_representatives,
)
from src.face_search import DetectedFace  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


def rnd_unit(seed: int, dim: int = 8) -> np.ndarray:
    rng = np.random.default_rng(seed)
    v = rng.normal(size=dim).astype("float32")
    return v / float(np.linalg.norm(v))


def near(vec: np.ndarray, cos: float, dim: int = 8) -> np.ndarray:
    """构造与 vec 夹角 cosine≈cos 的另一单位向量。"""
    rng = np.random.default_rng(12345)
    orth = rng.normal(size=dim).astype("float32")
    orth -= orth.dot(vec) * vec
    orth /= float(np.linalg.norm(orth))
    v = cos * vec + np.sqrt(max(0.0, 1 - cos * cos)) * orth
    return (v / float(np.linalg.norm(v))).astype("float32")


def make(bbox, emb, *, quality=0.5, det=0.9) -> DetectedFace:
    f = DetectedFace(index=0, bbox=tuple(float(x) for x in bbox), kps=None, det_score=det)
    f.embedding = [float(x) for x in emb]
    f.quality = float(quality)
    return f


def test_iou() -> None:
    print("\n[1] IoU 基本正确性")
    check("完全重合 = 1.0", abs(iou([0, 0, 10, 10], [0, 0, 10, 10]) - 1.0) < 1e-9)
    check("不相交 = 0.0", iou([0, 0, 10, 10], [20, 20, 30, 30]) == 0.0)
    # 交 50，并 150 → 1/3
    check("半重叠 = 1/3", abs(iou([0, 0, 10, 10], [5, 0, 15, 10]) - (50 / 150)) < 1e-9)


def test_one_person_one_track() -> None:
    print("\n[2] 同一个人跨 30 帧 → 1 条 track（IoU 路径）")
    v = rnd_unit(1)
    obs = []
    for i in range(30):
        x = 100 + i * 0.5           # 缓慢移动，相邻 IoU 很高
        obs.append(Obs(make([x, 200, x + 80, 280], v, quality=0.5 + i * 0.001),
                       float(i), Path("f.jpg"), 0))
    tracks = merge_tracks_within_shot(obs, iou_thr=0.5, cos_thr=0.85)
    check("合并为 1 条 track", len(tracks) == 1, f"got {len(tracks)}")
    check("track 含全部 30 帧", len(tracks[0].obs) == 30)

    reps = pick_representatives(tracks, keep_per_track=2)
    check("代表帧 ≤ 2", len(reps) <= 2, f"got {len(reps)}")
    for a, b in zip(reps, reps[1:]):
        check("相邻代表帧间隔 ≥ 1s", abs(b.sec - a.sec) >= 1.0,
              f"{a.sec} vs {b.sec}")


def test_two_people_two_tracks() -> None:
    print("\n[3] 两个人同框 → 2 条 track")
    va, vb = rnd_unit(2), rnd_unit(3)
    # 确保两个向量不相似
    if float(va.dot(vb)) > 0.3:
        vb = near(va, -0.5)
    obs = []
    for i in range(10):
        obs.append(Obs(make([0, 0, 80, 80], va), float(i), Path("f.jpg"), 0))
        obs.append(Obs(make([400, 0, 480, 80], vb), float(i), Path("f.jpg"), 0))
    tracks = merge_tracks_within_shot(obs, iou_thr=0.5, cos_thr=0.85)
    check("合并为 2 条 track", len(tracks) == 2, f"got {len(tracks)}")


def test_appearance_merge_without_iou() -> None:
    print("\n[4] 无空间重叠但外观一致（cosine 路径）→ 合并")
    v = rnd_unit(4)
    obs = [
        Obs(make([0, 0, 80, 80], v), 0.0, Path("f.jpg"), 0),
        Obs(make([500, 500, 580, 580], v), 1.5, Path("f.jpg"), 0),   # IoU=0，cosine=1
    ]
    tracks = merge_tracks_within_shot(obs, iou_thr=0.5, cos_thr=0.85)
    check("cosine=1.0 → 合并为 1 条", len(tracks) == 1, f"got {len(tracks)}")


def test_adjacent_track_merge() -> None:
    print("\n[5] 相邻 track 再合并（adjacent_track_merge_cosine）")
    v = rnd_unit(5)
    t1 = merge_tracks_within_shot([Obs(make([0, 0, 80, 80], v), 0.0, Path("f.jpg"), 0)],
                                  iou_thr=0.5, cos_thr=0.85)
    v2 = near(v, 0.95)
    t2 = merge_tracks_within_shot([Obs(make([900, 900, 980, 980], v2), 5.0, Path("f.jpg"), 0)],
                                  iou_thr=0.5, cos_thr=0.85)
    check("合并前 2 条", len(t1) + len(t2) == 2)
    merged = merge_adjacent_tracks(t1 + t2, cos_thr=0.85)
    check("cosine 0.95 > 0.85 → 合并为 1 条", len(merged) == 1, f"got {len(merged)}")
    merged2 = merge_adjacent_tracks(t1 + t2, cos_thr=0.99)
    check("cosine 0.95 < 0.99 → 保持 2 条", len(merged2) == 2, f"got {len(merged2)}")


def test_representative_quality() -> None:
    print("\n[6] 代表帧取质量最高者")
    v = rnd_unit(6)
    obs = [
        Obs(make([0, 0, 80, 80], v, quality=0.1), 0.0, Path("f.jpg"), 0),
        Obs(make([0, 0, 80, 80], v, quality=0.9), 3.0, Path("f.jpg"), 0),
        Obs(make([0, 0, 80, 80], v, quality=0.5), 6.0, Path("f.jpg"), 0),
    ]
    tracks = merge_tracks_within_shot(obs, iou_thr=0.5, cos_thr=0.85)
    reps = pick_representatives(tracks, keep_per_track=1)
    check("只留 1 张", len(reps) == 1)
    check("留下的质量是 0.9", abs(reps[0].face.quality - 0.9) < 1e-9,
          f"got {reps[0].face.quality}")
    check("时间戳是 3.0s", abs(reps[0].sec - 3.0) < 1e-9, f"got {reps[0].sec}")


def test_no_adjacent_within_1s() -> None:
    print("\n[7] 所有代表帧两两间隔 ≥ 1s（规格验收硬条件）")
    rng = random.Random(7)
    obs = []
    for i in range(200):
        v = rnd_unit(100 + i % 5)
        # 时间戳刻意密集：0.05s 一个
        obs.append(Obs(make([0, 0, 80, 80], v), round(i * 0.05, 3), Path("f.jpg"), 0))
    tracks = merge_tracks_within_shot(obs, iou_thr=0.5, cos_thr=0.85)
    tracks = merge_adjacent_tracks(tracks, 0.85)
    reps = pick_representatives(tracks, keep_per_track=2)
    secs = sorted(r.sec for r in reps)
    gaps = [b - a for a, b in zip(secs, secs[1:])]
    check(f"代表帧 {len(reps)} 张，全部间隔 ≥ 1s",
          all(g >= 1.0 for g in gaps), f"min gap={min(gaps) if gaps else 'n/a'}")
    check("代表帧数量远小于原始 200 帧", len(reps) < 40, f"got {len(reps)}")


def test_burst_dedup() -> None:
    print("\n[8] burst 去重（同文件内近重复过多才生效）")
    v = rnd_unit(8)
    dup = [Obs(make([0, 0, 80, 80], v), float(i), Path("f.jpg"), 0) for i in range(80)]
    kept, is_burst = burst_dedup(dup, cos_thr=0.90, count_thr=50)
    check("80 张全同 → 判定 burst", is_burst)
    check("burst 后只剩 1 张", len(kept) == 1, f"got {len(kept)}")

    distinct = [Obs(make([0, 0, 80, 80], rnd_unit(1000 + i)), float(i), Path("f.jpg"), 0)
                for i in range(80)]
    kept2, is_burst2 = burst_dedup(distinct, cos_thr=0.90, count_thr=50)
    check("80 张各不同 → 不判 burst", not is_burst2)
    check("不删任何一张", len(kept2) == 80, f"got {len(kept2)}")

    few = dup[:30]
    kept3, is_burst3 = burst_dedup(few, cos_thr=0.90, count_thr=50)
    check("30 张未超阈值 → 不处理", (not is_burst3) and len(kept3) == 30)


def test_realistic_interview() -> None:
    """模拟 5 分钟访谈：1fps 采样 300 帧，2 个人轮流出现。

    规格验收「F2 后每段输出 ≤ 期望值×1.5」。这里期望值人工定为
    2 人 × 每条 track 2 张 = 4 张，要求 F2 输出 ≤ 6 张。
    """
    print("\n[9] 模拟 5 分钟 1fps 访谈（300 帧 / 2 人）")
    va, vb = rnd_unit(20), near(rnd_unit(20), -0.6)
    obs = []
    for i in range(300):
        t = float(i)
        # A 在第 0~150s 出现，B 在第 120~300s 出现（有交叠）
        if i <= 150:
            obs.append(Obs(make([100, 100, 200, 200], va,
                                quality=0.4 + 0.001 * (i % 40)), t, Path("f.jpg"), 0))
        if i >= 120:
            obs.append(Obs(make([500, 100, 600, 200], vb,
                                quality=0.4 + 0.001 * (i % 40)), t, Path("f.jpg"), 0))
    # 1fps 下每帧一个 shot（长镜头 1fps 采样），故逐帧单独成 shot 会过度合并；
    # 这里按「一个长 shot」处理，正是 F2 要解决的场景。
    tracks = merge_tracks_within_shot(obs, iou_thr=0.5, cos_thr=0.85)
    tracks = merge_adjacent_tracks(tracks, 0.85)
    reps = pick_representatives(tracks, keep_per_track=2)
    check(f"300 帧 × 2 人 → track 数 ≤ 4（实际 {len(tracks)}）", len(tracks) <= 4)
    check(f"代表帧 ≤ 期望 4 × 1.5 = 6（实际 {len(reps)}）", len(reps) <= 6)
    secs = sorted(r.sec for r in reps)
    check("全部间隔 ≥ 1s",
          all(b - a >= 1.0 for a, b in zip(secs, secs[1:])),
          f"secs={secs}")


def main() -> int:
    print("=" * 72)
    print("F2 track 去重 / 代表帧优选 验收测试")
    print("=" * 72)
    test_iou()
    test_one_person_one_track()
    test_two_people_two_tracks()
    test_appearance_merge_without_iou()
    test_adjacent_track_merge()
    test_representative_quality()
    test_no_adjacent_within_1s()
    test_burst_dedup()
    test_realistic_interview()
    print("\n" + "=" * 72)
    print(f"PASS={PASS}  FAIL={FAIL}")
    print("=" * 72)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
