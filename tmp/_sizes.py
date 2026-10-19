import sqlite3
c = sqlite3.connect(r'data\library.db')
for kind in ('photo','video'):
    n, tot, avg, mn, mx = c.execute(
        "SELECT COUNT(*), SUM(size), AVG(size), MIN(size), MAX(size) FROM files WHERE kind=?",
        (kind,)).fetchone()
    print(f"{kind}: n={n:,}  total={tot/1e12:.3f} TB  avg={avg/1e6:.2f} MB  min={mn/1e6:.3f} MB  max={mx/1e9:.2f} GB")
print()
print("size buckets (photo):")
for lo, hi, lbl in [(0,1e6,'<1MB'),(1e6,2e6,'1-2MB'),(2e6,5e6,'2-5MB'),(5e6,1e7,'5-10MB'),(1e7,5e7,'10-50MB'),(5e7,1e12,'>50MB')]:
    n, tot = c.execute("SELECT COUNT(*), COALESCE(SUM(size),0) FROM files WHERE kind='photo' AND size>=? AND size<?", (lo,hi)).fetchone()
    print(f"  {lbl:<8} n={n:>8,}  {tot/1e9:>8.1f} GB")
print()
print("video: >60MB count:", c.execute("SELECT COUNT(*) FROM files WHERE kind='video' AND size>6e7").fetchone()[0])
c.close()
