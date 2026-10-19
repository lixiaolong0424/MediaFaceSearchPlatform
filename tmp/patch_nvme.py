# -*- coding: utf-8 -*-
"""补丁：中间文件占用统计
  1) 开始统计时写 pending 标记、完成时清掉（跨会话可见）
  2) 侧边栏那段在「统计中」时自己刷新，不需要用户手动再点
"""
import io

p = "app.py"
s = io.open(p, encoding="utf-8").read()

# ---- 1) 写值的时候清掉 pending 标记 ----
old_write = '''                (_NVME_KEY, json.dumps({"bytes": int(used), "at": datetime.now().isoformat(
                    timespec="seconds")}), datetime.now().isoformat(timespec="seconds")),
            )
            con.commit()'''
new_write = '''                (_NVME_KEY, json.dumps({"bytes": int(used), "at": datetime.now().isoformat(
                    timespec="seconds")}), datetime.now().isoformat(timespec="seconds")),
            )
            con.execute("DELETE FROM app_settings WHERE key=?", (_NVME_PENDING_KEY,))
            con.commit()'''
assert old_write in s
s = s.replace(old_write, new_write, 1)

# ---- 2) 常量 + 开始统计时写标记 ----
old_start = '''def _start_nvme_walk(db_path: str, nvme_root: str) -> None:
    """后台线程统计占用。**绝不在渲染路径上做**，界面先显示上次的值。"""
    if _NVME_WALKING["on"]:
        return
    _NVME_WALKING["on"] = True'''
new_start = '''def _mark_nvme_pending(db_path: str) -> None:
    import sqlite3
    from datetime import datetime

    try:
        con = sqlite3.connect(db_path, timeout=5.0)
        try:
            con.execute(
                "INSERT INTO app_settings(key, value, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "updated_at=excluded.updated_at",
                (_NVME_PENDING_KEY, datetime.now().isoformat(timespec="seconds"),
                 datetime.now().isoformat(timespec="seconds")))
            con.commit()
        finally:
            con.close()
    except Exception:  # noqa: BLE001
        pass


def _start_nvme_walk(db_path: str, nvme_root: str) -> None:
    """后台线程统计占用。**绝不在渲染路径上做**，界面先显示上次的值。"""
    if _NVME_WALKING["on"]:
        return
    _NVME_WALKING["on"] = True
    _mark_nvme_pending(db_path)'''
assert old_start in s
s = s.replace(old_start, new_start, 1)

s = s.replace('_NVME_KEY = "nvme_usage"',
              '_NVME_KEY = "nvme_usage"\n_NVME_PENDING_KEY = "nvme_pending_at"', 1)

# ---- 3) _nvme_usage 增加 pending ----
old_ret = '''    return {
        "used": used, "budget_gb": budget_gb, "ratio": ratio,
        "warn": ratio >= warn_at, "alert": ratio >= warn_at,
        "free_gb": free_gb, "measured_at": at, "known": bool(at), "stale": stale,
    }'''
new_ret = '''    pending_raw = _read_setting_raw(str(cfg.db_path), _NVME_PENDING_KEY)
    pending = bool(pending_raw) and (not at or str(pending_raw) >= str(at))
    return {
        "used": used, "budget_gb": budget_gb, "ratio": ratio,
        "warn": ratio >= warn_at, "alert": ratio >= warn_at,
        "free_gb": free_gb, "measured_at": at, "known": bool(at), "stale": stale,
        "pending": pending,
    }'''
assert old_ret in s
s = s.replace(old_ret, new_ret, 1)

# ---- 4) 读一个原始 app_settings 值的工具 ----
old_read = '''def _write_nvme_usage(db_path: str, used: int) -> None:'''
new_read = '''def _read_setting_raw(db_path: str, key: str) -> str:
    import sqlite3

    try:
        con = sqlite3.connect(db_path, timeout=3.0)
        try:
            row = con.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
        finally:
            con.close()
        return str(row[0]) if row and row[0] else ""
    except Exception:  # noqa: BLE001
        return ""


def _write_nvme_usage(db_path: str, used: int) -> None:'''
assert old_read in s
s = s.replace(old_read, new_read, 1)

# ---- 5) 侧边栏空间区块抽成函数 + 统计中自动刷新的 fragment ----
old_block = '''        # ---- 空间 ----
        st.divider()
        st.markdown("### 空间")
        if not nvme["known"]:
            # 首次还没有统计值：后台线程正在算，界面不等它（否则要卡 50 秒）
            st.caption("正在后台统计中间文件占用…（首次约 1 分钟，只算一次，不影响使用）")
        else:
            st.progress(min(1.0, float(nvme["ratio"])),
                        text=f"中间文件 {human_bytes(nvme['used'])} / "
                             f"{nvme['budget_gb']:.0f} GB（{nvme['ratio']*100:.1f}%）")
            st.caption(f"统计于 {(_age_text(nvme.get('measured_at')))}"
                       + ("　（正在后台刷新）" if nvme["stale"] else ""))
            if nvme["warn"]:
                st.error(f"中间文件已超 {nvme['ratio']*100:.0f}%，"
                         "建议先跑完编码再继续入库。")
        if st.button("重新统计占用", use_container_width=True):
            _start_nvme_walk(str(cfg.db_path), str(cfg.path("paths.nvme_root")))
            st.cache_data.clear()
            st.rerun()'''
new_block = '''        # ---- 空间 ----
        st.divider()
        st.markdown("### 空间")
        if nvme.get("pending") or not nvme["known"]:
            # 正在算：挂一个 4 秒自刷新的片段，算完自动显示新数字，
            # 不需要用户再点一次（否则会出现「点了还是 22 分钟前」的错觉）。
            _space_live(cfg)
        else:
            _space_block(cfg, nvme)'''
assert old_block in s
s = s.replace(old_block, new_block, 1)

# ---- 6) 追加两个函数（放在 _age_text 之前） ----
anchor = "def _age_text(iso: Optional[str]) -> str:"
add = '''def _space_block(cfg: Config, nvme: Dict[str, Any]) -> None:
    if not nvme["known"]:
        st.caption("正在后台统计中间文件占用…（约 1 分钟，不影响使用）")
    else:
        st.progress(min(1.0, float(nvme["ratio"])),
                    text=f"中间文件 {human_bytes(nvme['used'])} / "
                         f"{nvme['budget_gb']:.0f} GB（{nvme['ratio']*100:.1f}%）")
        st.caption(f"统计于 {_age_text(nvme.get('measured_at'))}"
                   + ("　· 正在后台更新…" if nvme.get("pending") else ""))
        if nvme["warn"]:
            st.error(f"中间文件已超 {nvme['ratio']*100:.0f}%，建议先跑完编码再继续入库。")
    if st.button("重新统计占用", use_container_width=True):
        _start_nvme_walk(str(cfg.db_path), str(cfg.path("paths.nvme_root")))
        st.rerun()


@st.fragment(run_every="4s")
def _space_live(cfg: Config) -> None:
    st.caption("正在后台统计中间文件占用…（约 1 分钟，算完这里会自动更新）")
    nvme = _nvme_usage(cfg, start_walk=False)
    if nvme["known"] and not nvme.get("pending"):
        _space_block(cfg, nvme)
        st.rerun(scope="app")


'''
assert anchor in s
s = s.replace(anchor, add + anchor, 1)

io.open(p, "w", encoding="utf-8", newline="\n").write(s)
print("patched app.py")
