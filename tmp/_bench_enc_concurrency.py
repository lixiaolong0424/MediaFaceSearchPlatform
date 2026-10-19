import subprocess, sys, time, sqlite3
from pathlib import Path
ROOT = Path(r'D:\DATA\PROJECT\建设多媒体整理系统')
PY = str(ROOT / '.venv' / 'Scripts' / 'python.exe')
LOG = ROOT / 'logs'

def count_points():
    import sys as s; s.path.insert(0, str(ROOT))
    from src.common import load_config
    from src.vector_store import open_store, semantic_collection
    cfg = load_config(None, None)
    st = open_store(cfg)
    return st.count(semantic_collection(cfg))

def run(nproc, limit, label):
    procs = []
    for i in range(nproc):
        args = [PY, '-u', str(ROOT / '04_encode.py'), '--limit', str(limit)]
        if nproc > 1:
            args += ['--shard', f'{i+1}/{nproc}']
        lg = open(LOG / f'bench_enc_{nproc}p_{i}.log', 'ab')
        procs.append(subprocess.Popen(args, cwd=str(ROOT), stdout=lg, stderr=subprocess.STDOUT))
    t0 = time.monotonic()
    for p in procs:
        p.wait()
    el = time.monotonic() - t0
    print(f'  {label}: {nproc} 进程 × {limit} 目标，用时 {el/60:.1f} 分钟 -> 合计 {nproc*limit/el:.2f} 张/秒', flush=True)
    return nproc * limit / el

print('测 1 进程（基线）...', flush=True)
a = run(1, 400, 'baseline')
print('测 3 进程并发...', flush=True)
b = run(3, 400, 'concurrent')
print(f'并发加速比: {b/a:.2f}x')
