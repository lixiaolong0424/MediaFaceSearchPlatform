import collections
import sqlite3

c = sqlite3.connect(r'data\library.db')
c.row_factory = sqlite3.Row
print("photo error 总数:", c.execute(
    "SELECT COUNT(*) FROM files WHERE kind='photo' AND status='error'").fetchone()[0])
print()
print("=== stage_status.extract 的错误信息分布 ===")
for r in c.execute(
        "SELECT substr(COALESCE(error,'(空)'),1,150) AS e, COUNT(*) n FROM stage_status "
        "WHERE stage='extract' AND status='error' GROUP BY e ORDER BY n DESC LIMIT 12"):
    print(f"  n={r['n']:<6} {r['e']}")
print()
print("=== file_encoding 的错误信息分布（semantic） ===")
for r in c.execute(
        "SELECT substr(COALESCE(error,'(空)'),1,150) AS e, COUNT(*) n FROM file_encoding "
        "WHERE model='semantic' AND status='error' GROUP BY e ORDER BY n DESC LIMIT 12"):
    print(f"  n={r['n']:<6} {r['e']}")
print()
print("=== 失败文件的样例（看是否与目录/扩展名/大小相关） ===")
for r in c.execute(
        "SELECT f.file_id, f.size, f.path, ss.error FROM files f "
        "LEFT JOIN stage_status ss ON ss.file_id=f.file_id AND ss.stage='extract' "
        "WHERE f.status='error' LIMIT 8"):
    print(f"  id={r['file_id']:<7} size={r['size']:<10} {str(r['path'])[:90]}")
    print(f"          err={str(r['error'])[:160]}")
print()
print("=== 失败文件按扩展名 ===")
cnt = collections.Counter()
for r in c.execute("SELECT path FROM files WHERE status='error'"):
    cnt[str(r['path']).rsplit('.', 1)[-1].lower()] += 1
print("  ", cnt.most_common(10))
print()
print("=== 失败文件按顶层目录 ===")
cnt2 = collections.Counter()
for r in c.execute("SELECT rel_path, path FROM files WHERE status='error'"):
    seg = str(r['rel_path'] or r['path']).replace('/', '\\').lstrip('\\').split('\\')[0]
    cnt2[seg] += 1
print("  ", cnt2.most_common(8))
c.close()
