import sqlite3, time, io, os
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from PIL import Image, ImageFile
import imagehash
ImageFile.LOAD_TRUNCATED_IMAGES = True

c = sqlite3.connect(r'data\library.db')
paths = [r[0] for r in c.execute("SELECT path FROM files WHERE kind='photo' ORDER BY file_id")]
c.close()

def phash_plain(p):
    try:
        with Image.open(p) as im:
            return str(imagehash.phash(im.convert("RGB"), hash_size=8))
    except Exception:
        return None

def phash_draft(p):
    try:
        with Image.open(p) as im:
            try:
                im.draft("L", (64, 64))
            except Exception:
                pass
            return str(imagehash.phash(im.convert("RGB"), hash_size=8))
    except Exception:
        return None

def run(fn, sel, workers, label):
    t0=time.monotonic()
    if workers==1:
        n=sum(1 for p in sel if fn(p))
    else:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            n=sum(1 for h in ex.map(fn, sel, chunksize=8) if h)
    el=time.monotonic()-t0
    print(f"  {label:<36} {len(sel)/el:7.1f} files/s  ({el:.1f}s / {len(sel)})")
    return len(sel)/el

N=600
s1 = paths[60000:60000+N]
s2 = paths[70000:70000+N]
s3 = paths[80000:80000+N]
s4 = paths[90000:90000+N]
print("=== pHash throughput ===")
a=run(phash_draft, s1, 1,  "draft, 1 proc (cold)")
b=run(phash_draft, s2, 12, "draft, 12 procs (cold)")
c2=run(phash_draft, s3, 12, "draft, 12 procs (2nd)")
d=run(phash_plain, s4, 12, "plain, 12 procs (cold)")

# 校验 draft 与 plain 结果完全一致（去重正确性依赖 phash 稳定）
import random
smp = random.Random(7).sample(paths[100000:101000], 60)
same = all(phash_plain(p) == phash_draft(p) for p in smp)
print("draft == plain on 60 samples:", same)
