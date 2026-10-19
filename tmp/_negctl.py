import sys, tempfile
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, r'D:\DATA\PROJECT\建设多媒体整理系统')
from src.common import load_config
from src.search import ShotHit, _final_cut

cfg = load_config(None, None)
cfg.override("retrieval.rerank_topk", 50)
# 手工构造「修复前」的返回序列：重排段 0.39，尾部原样 0.44
hits = []
for i in range(30):
    h = ShotHit(file_id=i+1, shot_ix=0, score=0.390, sec=0.0, shot_start=0.0, shot_end=0.0)
    h.rerank_score = 0.390; h.recall_score = 0.44; hits.append(h)
for i in range(30):
    h = ShotHit(file_id=100+i, shot_ix=0, score=0.440, sec=0.0, shot_start=0.0, shot_end=0.0)
    h.rerank_score = None; h.recall_score = None; hits.append(h)
kept, cut = _final_cut(cfg, hits, reranked=True)
seq = [h.score for h in kept]
rise = next((i for i,(a,b) in enumerate(zip(seq, seq[1:])) if b > a + 1e-9), -1)
print(f"  修复前的序列在 index {rise} 上升（{seq[rise]:.4f} → {seq[rise+1]:.4f}）"
      if rise >= 0 else "  未出现上升")
print(f"  → 该构造下断言'单调不增'会失败：{'是' if rise >= 0 else '否'}")
