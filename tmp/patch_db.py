# -*- coding: utf-8 -*-
"""一次性补丁：把 db.py 的 path_index 三个函数改成 root 感知。"""
import io

p = "src/db.py"
s = io.open(p, encoding="utf-8").read()
start = s.index("def load_path_index(conn: sqlite3.Connection)")
end = s.index("\ndef ", s.index("def drop_path_index"))
new = '''def load_path_index(conn: sqlite3.Connection,
                    root: Optional[str] = None) -> Dict[str, sqlite3.Row]:
    """path_hash → row。**按 root 过滤**：两个素材根下可能有同名相对路径，
    用全局字典会互相覆盖（这是多素材目录支持的前提）。"""
    if root is None:
        return {str(r["path_hash"]): r for r in conn.execute("SELECT * FROM path_index")}
    return {
        str(r["path_hash"]): r
        for r in conn.execute("SELECT * FROM path_index WHERE root=?", (root,))
    }


def upsert_path_index(conn: sqlite3.Connection, root: str, ph: str, file_id: int,
                      inode: Optional[int], mtime: Optional[int], size: Optional[int]) -> None:
    conn.execute(
        """INSERT INTO path_index(root, path_hash, file_id, inode, mtime, size, updated_at)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(root, path_hash) DO UPDATE SET
               file_id=excluded.file_id, inode=excluded.inode,
               mtime=excluded.mtime, size=excluded.size, updated_at=excluded.updated_at""",
        (root, ph, file_id, inode, mtime, size, local_now_iso()),
    )


def drop_path_index(conn: sqlite3.Connection, root: str, ph: str) -> None:
    conn.execute("DELETE FROM path_index WHERE root=? AND path_hash=?", (root, ph))


'''
s = s[:start] + new + s[end:]
io.open(p, "w", encoding="utf-8", newline="\n").write(s)
print("patched db.py")
