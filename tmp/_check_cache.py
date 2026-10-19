import sqlite3
from pathlib import Path

c = sqlite3.connect(r'data\library.db')
print("files by kind/status:")
for r in c.execute("SELECT kind,status,COUNT(*) FROM files GROUP BY kind,status ORDER BY COUNT(*) DESC"):
    print("  ", tuple(r))
print("stage_status:", c.execute("SELECT stage,status,COUNT(*) FROM stage_status GROUP BY stage,status").fetchall())
print("sample done photos:")
for r in c.execute("SELECT file_id,width,height,captured_at,time_source FROM files WHERE kind='photo' AND status='done' LIMIT 3"):
    print("  ", tuple(r))
c.close()

fr = Path('data/nvme/frames')
dirs = sorted(fr.iterdir()) if fr.exists() else []
print("frame dirs:", len(dirs))
shown = 0
for d in dirs:
    for f in sorted(d.iterdir()):
        print("   ", d.name, f.name, round(f.stat().st_size / 1024, 1), "KB")
        shown += 1
    if shown >= 3:
        break
files = [f for d in dirs for f in d.iterdir() if f.is_file()]
tot = sum(f.stat().st_size for f in files)
print(f"cache total: {tot/1e6:.1f} MB for {len(files)} photos -> {tot/max(1,len(files))/1024:.1f} KB/photo")
