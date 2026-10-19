import sqlite3, time, os
from concurrent.futures import ThreadPoolExecutor

c = sqlite3.connect(r'data\library.db')
paths = [r[0] for r in c.execute("SELECT path FROM files WHERE kind='photo' ORDER BY file_id")]
c.close()
print("total photos:", len(paths))
print("RAM GB:", round(os.sysconf("SC_PAGE_SIZE")*os.sysconf("SC_PHYS_PAGES")/1e9,1) if hasattr(os,'sysconf') else "n/a")
import psutil; print("RAM total/avail GB:", round(psutil.virtual_memory().total/1e9,1), round(psutil.virtual_memory().available/1e9,1))

def read_one(p):
    try:
        with open(p,'rb') as f: return f.read()
    except OSError: return b""

def cold_read(slice_, workers, label):
    sel = paths[slice_]
    t0=time.monotonic()
    if workers==1:
        tot=sum(len(read_one(p)) for p in sel)
    else:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            tot=sum(len(b) for b in ex.map(read_one, sel, chunksize=1))
    el=time.monotonic()-t0
    print(f"  {label:<34} {len(sel)/el:7.1f} files/s  {tot/el/1e6:7.1f} MB/s  ({el:.1f}s / {len(sel)})")

N=1200
print("=== cold read only ===")
cold_read(slice(20000, 20000+N), 1,  "1 worker, cold")
cold_read(slice(30000, 30000+N), 16, "16 workers, cold")

print("=== read + pHash (real work) ===")
import imagehash
from PIL import Image, ImageFile
ImageFile.LOAD_TRUNCATED_IMAGES = True
def phash_bytes(b):
    import io
    try:
        with Image.open(io.BytesIO(b)) as im:
            return str(imagehash.phash(im.convert("RGB"), hash_size=8))
    except Exception:
        return None

def cold_phash(slice_, workers, label):
    sel = paths[slice_]
    t0=time.monotonic()
    if workers==1:
        n=0
        for p in sel:
            b=read_one(p)
            if phash_bytes(b): n+=1
    else:
        n=0
        for b in ThreadPoolExecutor(max_workers=workers).map(read_one, sel, chunksize=1):
            pass
        n=len(sel)
        with ThreadPoolExecutor(max_workers=workers) as ex:
            n=sum(1 for h in ex.map(phash_bytes, [read_one(p) for p in sel], chunksize=1) if h)
    el=time.monotonic()-t0
    print(f"  {label:<34} {len(sel)/el:7.1f} files/s  ({el:.1f}s / {len(sel)})")

cold_phash(slice(40000, 40000+N), 1, "serial read+pHash, cold")
