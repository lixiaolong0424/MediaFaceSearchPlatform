import sqlite3, time, sys
from concurrent.futures import ThreadPoolExecutor
import os

c = sqlite3.connect(r'data\library.db')
paths = [r[0] for r in c.execute("SELECT path FROM files WHERE kind='photo' ORDER BY file_id LIMIT 4000")]
c.close()
print("photos:", len(paths))

def read_one(p):
    try:
        with open(p, 'rb') as f:
            return len(f.read())
    except OSError:
        return 0

def bench(n, workers, chunk):
    sel = paths[:n]
    t0 = time.monotonic()
    if workers == 1:
        tot = sum(read_one(p) for p in sel)
    else:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            tot = sum(ex.map(read_one, sel, chunksize=chunk))
    el = time.monotonic() - t0
    return len(sel)/el, tot/el/1e6, el

for workers, chunk in [(1,1),(4,1),(8,1),(16,1),(32,1),(8,4),(16,4)]:
    n = 400 if workers<=8 else 800
    fps, mbs, el = bench(n, workers, chunk)
    print(f"  workers={workers:>2} chunk={chunk}  {fps:6.1f} files/s  {mbs:6.1f} MB/s  ({el:.1f}s for {n})")
