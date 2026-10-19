# --------------------------------------------------------------------------- #
# 页面路由（v5.3：六个页面，一次只渲染一个 —— 原来两个 Tab 每次全渲染，
# 首屏因此多花一倍时间）
# --------------------------------------------------------------------------- #

NAV_PAGES = [
    ("wall", "🖼 ", "nav.wall"),
    ("search", "🔎 ", "nav.search"),
    ("tasks", "📋 ", "nav.tasks"),
    ("settings", "⚙️ ", "nav.settings"),
    ("maintain", "🛠 ", "nav.maintain"),
    ("about", "ℹ️ ", "nav.about"),
]


def _reload_all() -> None:
    """配置或数据变了：把配置缓存、NVMe 统计缓存、所有 cache_data 一起清掉。"""
    try:
        _cfg.clear()
    except Exception:  # noqa: BLE001
        pass
    try:
        _nvme_used_bytes.clear()
    except Exception:  # noqa: BLE001
        pass
    st.cache_data.clear()


def _goto(page: str) -> None:
    """跳到某页。必须在 radio 控件创建**之前**写 session_state。"""
    st.session_state["nav_radio"] = page
    st.session_state["page"] = page


def _index_progress_summary(cfg: Config, stats: Dict[str, Any]) -> None:
    """索引到底建到哪了 —— 只保留真正有用的三行。"""
    roots = stats.get("indexed_roots") or []
    for r in roots[:3]:
        st.caption(f"根 `{r['indexed_root']}` · {r['files']:,} 个文件 · "
                   f"上次扫描 {(r['last_scan'] or '—')[:16].replace('T', ' ')}")
    by = stats.get("by_status") or {}
    pending = int(by.get("pending") or 0)
    missing = int(by.get("missing") or 0)
    dup = int(by.get("skipped_dup") or 0)
    lines = [f"待处理 **{pending:,}**"]
    if dup:
        lines.append(f"重复跳过 **{dup:,}**")
    if missing:
        lines.append(f"缺失 **{missing:,}**")
    st.caption(" · ".join(lines))
    configured = str(cfg.media_root).rstrip("\\/")
    indexed = {str(r["indexed_root"]).rstrip("\\/") for r in roots}
    if roots and configured not in indexed:
        st.warning(f"设置的素材文件夹是 `{configured}`，但库里索引的是 {sorted(indexed)}。"
                   "请到「设置 → 更改素材文件夹」走一遍向导，别直接改。")


def sidebar(cfg: Config, stats: Dict[str, Any], nvme: Dict[str, Any]) -> str:
    ss = st.session_state
    keys = [k for k, _, _ in NAV_PAGES]
    labels = {k: icon + U.t(key) for k, icon, key in NAV_PAGES}

    with st.sidebar:
        # ---- 导航 ----
        if ss.get("page") not in keys:
            ss["page"] = "wall"
        if ss.get("nav_radio") not in keys:
            ss["nav_radio"] = ss["page"]
        pick = st.radio("页面", keys, format_func=lambda k: labels[k],
                        label_visibility="collapsed", key="nav_radio")
        ss["page"] = pick
        st.divider()

        # ---- 现在的库 ----
        st.markdown("### 现在的库")
        st.caption(f"素材文件夹 `{stats['root']}`")
        c1, c2 = st.columns(2)
        c1.metric("素材", f"{int(stats.get('files_total') or 0):,}")
        c2.metric("人脸", f"{int(stats.get('faces') or 0):,}")
        st.caption(f"照片 {int(stats.get('photos') or 0):,}（已处理 "
                   f"{int(stats.get('photos_done') or 0):,}） · "
                   f"视频 {int(stats.get('videos') or 0):,}（已处理 "
                   f"{int(stats.get('videos_done') or 0):,}）")
        _index_progress_summary(cfg, stats)

        # ---- 开始 ----
        st.divider()
        st.markdown("### 开始")
        busy = active_task(cfg) is not None
        auto = st.checkbox(U.t("scan.auto_process"), value=bool(ss.get("auto_process", True)),
                           key="auto_process", help=U.t("scan.auto_process.help"),
                           disabled=busy)
        if st.button(U.t("scan.new"), type="primary", use_container_width=True,
                     disabled=busy, help=U.t("scan.new.help")):
            if start_task(cfg, "scan_new", params={"auto_process": auto}):
                st.rerun()
        st.caption(U.t("scan.new.help"))

        if not ss.get("confirm_scan_all"):
            if st.button(U.t("scan.all"), use_container_width=True, disabled=busy,
                         help=U.t("scan.all.help")):
                ss["confirm_scan_all"] = True
                st.rerun()
        else:
            st.warning(U.t("scan.all.confirm"))
            cc1, cc2 = st.columns(2)
            if cc1.button("确定重做", type="primary", use_container_width=True):
                ss["confirm_scan_all"] = False
                if start_task(cfg, "scan_all", params={"auto_process": auto}):
                    st.rerun()
            if cc2.button("取消", use_container_width=True):
                ss["confirm_scan_all"] = False
                st.rerun()

        if st.button(U.t("act.faces"), use_container_width=True, disabled=busy,
                     help=U.t("act.faces.help")):
            if start_task(cfg, "faces"):
                st.rerun()

        with st.expander("更多操作"):
            if st.button(U.t("act.extract"), use_container_width=True, disabled=busy,
                         help=U.t("act.extract.help")):
                if start_task(cfg, "extract"):
                    st.rerun()
            if st.button(U.t("act.encode"), use_container_width=True, disabled=busy,
                         help=U.t("act.encode.help")):
                if start_task(cfg, "encode"):
                    st.rerun()
            if st.button(U.t("act.cluster"), use_container_width=True, disabled=busy,
                         help=U.t("act.cluster.help")):
                if start_task(cfg, "cluster"):
                    st.rerun()
            if st.button(U.t("act.reconcile"), use_container_width=True, disabled=busy,
                         help=U.t("act.reconcile.help")):
                if start_task(cfg, "reconcile"):
                    st.rerun()
            if st.button("更改素材文件夹…", use_container_width=True):
                ss["open_migrate"] = True
                _goto("settings")
                st.rerun()

        # ---- 当前任务 ----
        st.divider()
        st.markdown("### 当前任务")
        live_task_fragment(cfg)
        watch_events(cfg, reload_cfg=_reload_all)

        # ---- 异常角标（§15.5）----
        n_err = int(stats.get("files_error") or 0)
        if n_err:
            if st.button(f"⚠️ 待处理异常 {n_err:,}", use_container_width=True,
                         help="点开看是哪些文件、为什么失败"):
                _goto("maintain")
                st.rerun()

        # ---- 空间 ----
        st.divider()
        st.markdown("### 空间")
        st.progress(min(1.0, float(nvme["ratio"])),
                    text=f"中间文件 {human_bytes(nvme['used'])} / "
                         f"{nvme['budget_gb']:.0f} GB（{nvme['ratio']*100:.1f}%）")
        if nvme["warn"]:
            st.error(f"中间文件已超 {nvme['ratio']*100:.0f}%，建议先跑完编码再继续入库。")
    return ss["page"]


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #


def main() -> None:
    cfg = _cfg()
    _ensure_state(cfg)
    cfg.ensure_dirs()
    _ensure_schema_once(str(cfg.db_path))

    srv, srv_ok, srv_msg = _static_server()

    conn = _conn(cfg)
    try:
        try:
            stats = D.library_stats(conn, str(cfg.media_root))
        except Exception:  # noqa: BLE001 - 空库/未迁移时也要能打开界面
            stats = {"root": str(cfg.media_root), "files_total": 0, "files_done": 0,
                     "files_pending": 0, "files_error": 0, "files_missing": 0,
                     "photos": 0, "videos": 0, "shots": 0, "faces": 0, "last_scan": None,
                     "photos_done": 0, "videos_done": 0, "videos_pending": 0,
                     "videos_error": 0, "faces_pending_vector": 0, "by_status": {}}
    finally:
        conn.close()

    nvme = _nvme_usage(cfg)
    page = sidebar(cfg, stats, nvme)
    _show_toast()
    reason = st.session_state.pop("_reload_reason", None)
    if reason:
        st.caption(f"✓ {U.t('wall.reloaded', reason=reason)}")
    if not srv_ok:
        st.warning(f"缩略图服务未启动（{srv_msg}）—— 照片墙会显示不出图片。")

    # ---- 路由：一次只渲染当前页 ----
    try:
        if page == "wall":
            tab_wall(cfg, srv, stats)
        elif page == "search":
            tab_ask(cfg, srv, stats)
        elif page == "tasks":
            _pages.page_tasks(cfg)
        elif page == "settings":
            _pages.page_settings(cfg, reload_cfg=_reload_all)
        elif page == "maintain":
            _pages.page_maintain(cfg)
        elif page == "about":
            _pages.page_about(cfg, stats)
    except Exception as exc:  # noqa: BLE001 - 单页出错不该整站白屏
        LOG.exception("页面渲染失败：%s", page)
        st.error(U.t("error.generic", msg=exc))
        with st.expander("技术细节（排查用）"):
            st.exception(exc)

    # 渲染完成的证据行：出问题（白屏/卡住）时，日志里能明确区分
    # 「脚本根本没跑完」与「跑完了但前端没显示」，省掉一轮瞎猜。
    LOG.info("界面渲染完成：页面=%s · 文件 %s · 人脸 %s · 中间文件 %s · 静态服务 %s",
             page, f"{stats['files_done']:,}", f"{stats['faces']:,}",
             human_bytes(nvme["used"]), "ok" if srv_ok else "未启动")


if __name__ == "__main__":
    main()
