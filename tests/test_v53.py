# -*- coding: utf-8 -*-
"""v5.3 新增功能回归测试（任务队列 / 设置 / 路径变更 / 分组 / 文案清理）。

为什么单独一个文件：这五块是 v5.3 的地基，任何一块退化都会直接表现成
「按钮点了没反应」这种最难查的现象，所以必须有能一键重跑的断言。

用法：
    .venv\\Scripts\\python.exe tests\\test_v53.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import db as D  # noqa: E402
from src import migrate as MG  # noqa: E402
from src import settings_store as SS  # noqa: E402
from src import tasks as T  # noqa: E402
from src import ui_text as U  # noqa: E402
from src.common import load_config  # noqa: E402

PASS = 0
FAIL = 0
FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"  [FAIL] {name}  {detail}")


def section(title: str) -> None:
    print("\n" + "-" * 74)
    print(title)
    print("-" * 74)


def make_cfg(tmp: Path, media: Path):
    """把配置的「根」改到临时目录，避免测试写到生产 config.local.yaml。"""
    cfg = load_config()
    cfg.root = tmp                       # 只影响 overlay 写入位置
    cfg.override("db.path", str(tmp / "test.db"))
    cfg.override("media_root", str(media))
    return cfg


# --------------------------------------------------------------------------- #
def test_tasks(cfg) -> None:
    section("1. 任务队列（tasks 表 / 进度 / 取消 / 僵尸回收）")
    T.init(cfg)
    tid = T.create(cfg, kind="scan_new", title="扫描新文件",
                   plan=[{"script": "01_manifest.py", "args": [], "code": "scan"}],
                   total=100)
    t = T.get(cfg, tid)
    check("创建后状态是 queued", t["state"] == "queued", t["state"])
    check("计划被完整保存", T.plan_of(t)["steps"][0]["script"] == "01_manifest.py")
    check("创建即写事件", T.latest_event_id(cfg) > 0)

    rep = T.Reporter(cfg, tid)
    rep.start()
    check("start 后是 running", T.get(cfg, tid)["state"] == "running")
    check("running 被 active() 看到", any(x["task_id"] == tid for x in T.active(cfg)))
    rep.stage("extract", total=50, ix=1, n=1)
    rep.tick(10, total=50)
    rep._flush()
    t2 = T.get(cfg, tid)
    check("进度写回 done=10", int(t2["done"]) == 10, str(t2["done"]))
    check("阶段名是业务词而不是代号", t2["stage"] == U.t("stage.extract"), t2["stage"])
    snap = T.snapshot(t2)
    check("snapshot 能报出已用时间", snap["elapsed_sec"] >= 0, str(snap))
    # 把开始时间往前拨 10 秒，验证速率与预计剩余真的会算
    conn = D.connect(cfg.db_path, cfg=cfg)
    try:
        conn.execute("UPDATE tasks SET started_at='2026-01-01T00:00:00' WHERE task_id=?",
                     (tid,))
        conn.commit()
    finally:
        conn.close()
    snap2 = T.snapshot(T.get(cfg, tid))
    check("速率按实际耗时会算出来", (snap2["rate"] or 0) > 0, str(snap2["rate"]))
    check("预计剩余按速率外推", snap2["eta_sec"] is not None and snap2["eta_sec"] > 0,
          str(snap2["eta_sec"]))

    rep.success("完成")
    t3 = T.get(cfg, tid)
    check("success 后 finished_at 有值", bool(t3["finished_at"]))
    check("终态不再出现在 active()", all(x["task_id"] != tid for x in T.active(cfg)))

    # 取消标志
    tid2 = T.create(cfg, kind="encode", title="画面向量编码", plan=[{"script": "x"}])
    T.request_cancel(cfg, tid2)
    check("取消请求写进库", T.cancel_requested(cfg, tid2) is True)
    rep2 = T.Reporter(cfg, tid2)
    try:
        rep2.check_cancel()
        check("Reporter 读到取消标志会抛 TaskCancelled", False, "没抛")
    except T.TaskCancelled:
        check("Reporter 读到取消标志会抛 TaskCancelled", True)

    # 僵尸回收：running 且心跳很旧（updated_at 由 tick 维护，测试里直接改库来造场景）
    conn = D.connect(cfg.db_path, cfg=cfg)
    try:
        conn.execute("UPDATE tasks SET state='running', updated_at='2020-01-01T00:00:00' "
                     "WHERE task_id=?", (tid2,))
        conn.commit()
    finally:
        conn.close()
    n = T.reap_stale(cfg, minutes=5)
    check("僵尸 running 被回收成 failed", n >= 1 and T.get(cfg, tid2)["state"] == "failed",
          f"n={n} state={T.get(cfg, tid2)['state']}")

    # 历史清理
    removed = T.purge_history(cfg, days=-1)     # 负数 = 全部算过期
    check("purge_history 能清掉终态任务", removed["tasks"] >= 2, str(removed))

    # app_settings
    T.setting_set(cfg, "cluster_version", "vTEST")
    check("app_settings 读写正常", T.setting_get(cfg, "cluster_version") == "vTEST")


# --------------------------------------------------------------------------- #
def test_settings(cfg) -> None:
    section("2. 设置界面化（校验 / 保存 / 恢复默认 / 审计）")
    cur = SS.current(cfg)
    check("current() 覆盖所有登记项", set(cur) == set(SS.BY_KEY), f"{len(cur)}")

    ok = 0
    for bad in ({"retrieval.min_score": 5.0}, {"ui.page_size": "abc"},
                {"not.a.key": 1}):
        try:
            SS.validate(bad)
        except (ValueError, KeyError):
            ok += 1
    check("非法值与未登记项都被拒绝", ok == 3, f"ok={ok}")

    res = SS.save(cfg, {"ui.page_size": 120, "retrieval.min_score": 0.4}, who="test")
    check("保存返回改动数", res["changed"] == 2, str(res))
    p = SS.overlay_path(cfg)
    check("覆盖层文件已写出", p.exists(), str(p))
    text = p.read_text(encoding="utf-8")
    check("覆盖层里有新值", "page_size: 120" in text and "min_score: 0.4" in text, text[:200])

    row = None
    conn = D.connect(cfg.db_path, cfg=cfg, readonly=True)
    try:
        row = conn.execute(
            "SELECT what, before, after FROM audit_log WHERE what LIKE 'settings%' "
            "ORDER BY at DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    check("旧值→新值写进审计日志", row is not None and "120" in str(row["after"]),
          str(dict(row)) if row else "无记录")

    res2 = SS.save(cfg, {"ui.page_size": 120}, who="test")
    check("重复保存同一值不算改动", res2["changed"] == 0, str(res2))

    n = SS.reset(cfg, ["ui.page_size"])
    check("恢复默认删掉了该键", n["removed"] == 1, str(n))
    check("恢复默认后文件里不再有该项",
          "page_size: 120" not in SS.overlay_path(cfg).read_text(encoding="utf-8"))

    # 灵敏度三档必须是实测标定过的区间（曾经 0.55 导致所有搜索 0 结果）
    s = SS.BY_KEY["retrieval.min_score"]
    vals = [c[0] for c in s.choices]
    check("灵敏度三档都不高于模型上限 0.50", max(vals) <= 0.50, str(vals))


# --------------------------------------------------------------------------- #
def test_migrate(cfg) -> None:
    section("3. 素材路径变更（预览 / 改前缀 / 缺失处理 / 恢复）")
    tmp = Path(cfg.root)
    old = tmp / "mediaA"
    new = tmp / "mediaB"
    for d, names in ((old, ["a.jpg", "b.mp4", "c.jpg"]), (new, ["a.jpg", "b.mp4"])):
        d.mkdir(parents=True, exist_ok=True)
        for n in names:
            (d / n).write_bytes(b"x" * 32)

    conn = D.connect(cfg.db_path, cfg=cfg)
    try:
        for i, n in enumerate(["a.jpg", "b.mp4", "c.jpg"], 1):
            D.upsert_file(conn, path=str(old / n), rel_path=n, path_hash=f"h{i}",
                          inode=i, size=32, mtime=1, kind="photo", status="done")
        conn.commit()
    finally:
        conn.close()

    ok, msg = MG.validate_new_root(cfg, str(new))
    check("合法新路径通过校验", ok, msg)
    ok2, _ = MG.validate_new_root(cfg, str(old / "sub"))
    check("新路径在旧路径里面会被拒绝", not ok2)

    pv = MG.preview(cfg, str(new))
    check("预览数量正确", pv.total == 3 and pv.ok == 2 and pv.missing == 1,
          f"total={pv.total} ok={pv.ok} missing={pv.missing}")
    check("预览不改数据", _n_ok(cfg) == 3)

    res = MG.apply(cfg, str(new), strategy=MG.STRATEGY_KEEP)
    check("改前缀条数正确", res["remapped"] == 3, str(res))
    conn = D.connect(cfg.db_path, cfg=cfg, readonly=True)
    try:
        paths = [r["path"] for r in conn.execute("SELECT path FROM files ORDER BY file_id")]
        rels = [r["rel_path"] for r in conn.execute("SELECT rel_path FROM files ORDER BY file_id")]
    finally:
        conn.close()
    check("path 前缀已换到新根", all(p.startswith(str(new)) for p in paths), str(paths))
    check("rel_path 完全没变（向量与去重都不受影响）",
          rels == ["a.jpg", "b.mp4", "c.jpg"], str(rels))
    check("库根登记写到了新根", _library_root(cfg) == str(new) + "\\"
          or _library_root(cfg) == str(new), _library_root(cfg))

    rep = MG.missing_report(cfg)
    check("缺失文件数为 0（策略①不改状态）", rep["total"] == 0, str(rep["total"]))

    # 手动标一个缺失，再验证「恢复」与「清理报告」
    conn = D.connect(cfg.db_path, cfg=cfg)
    try:
        conn.execute("UPDATE files SET status='missing' WHERE rel_path='c.jpg'")
        conn.commit()
        fid = int(conn.execute(
            "SELECT file_id FROM files WHERE rel_path='c.jpg'").fetchone()[0])
    finally:
        conn.close()
    check("missing_report 能看到它", MG.missing_report(cfg)["total"] == 1)
    pr = MG.purge_missing(cfg, dry_run=True)
    check("清理报告是 dry-run（不真删）", pr["dry_run"] and pr["would_purge"] == 1, str(pr))
    n = MG.restore_missing(cfg, [fid])
    check("恢复缺失文件成功", n["restored"] == 1, str(n))
    check("恢复后不再缺失", MG.missing_report(cfg)["total"] == 0)


def _n_ok(cfg) -> int:
    conn = D.connect(cfg.db_path, cfg=cfg, readonly=True)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0])
    finally:
        conn.close()


def _library_root(cfg) -> str:
    conn = D.connect(cfg.db_path, cfg=cfg, readonly=True)
    try:
        row = conn.execute("SELECT root FROM libraries LIMIT 1").fetchone()
        return str(row["root"]) if row else ""
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
def test_cluster() -> None:
    section("4. 自动分组（密度聚类 / 置信度）")
    from src.cluster import _cluster_confidence, run_hdbscan

    rng = np.random.default_rng(0)
    base = rng.normal(size=(4, 32))
    pts, labels_true = [], []
    for i, b in enumerate(base):
        for _ in range(30):
            pts.append(b + rng.normal(scale=0.05, size=32))
            labels_true.append(i)
    pts.append(rng.normal(size=(1, 32))[0])
    x = np.asarray(pts, dtype=np.float32)
    x /= np.linalg.norm(x, axis=1, keepdims=True)

    class _Cfg:
        def get(self, k, d=None):
            return {
                "cluster.hdbscan_min_cluster_size": 3,
                "cluster.hdbscan_min_samples": 2,
                "cluster.hdbscan_cluster_selection_epsilon": 0.15,
                "cluster.hdbscan_cluster_selection_method": "leaf",
            }.get(k, d)

    labels = run_hdbscan(_Cfg(), x)
    n_clusters = len({int(v) for v in labels if v >= 0})
    check("能分出 4 个人群", 3 <= n_clusters <= 6, f"got {n_clusters}")
    # 同一个真实簇里绝大多数应被分到同一个 label
    same = [labels[i] for i in range(30)]
    check("同一人群绝大多数被分到一起",
          len({int(v) for v in same if v >= 0}) <= 2, str(set(same)))
    conf = _cluster_confidence(x, labels)
    check("置信度都在 0~1", all(0.0 <= v <= 1.0 for v in conf.values()), str(conf)[:120])
    check("紧密簇的置信度明显更高", max(conf.values()) > 0.9, str(conf)[:120])


# --------------------------------------------------------------------------- #
def test_reset_reprocess(cfg) -> None:
    section("5. 「重新扫描全部」的重置语义")
    from src.manifest import reset_for_full_reprocess

    conn = D.connect(cfg.db_path, cfg=cfg)
    try:
        conn.execute("DELETE FROM files")
        for i, st in enumerate(["done", "done", "skipped_dup", "missing", "pending"], 1):
            D.upsert_file(conn, path=f"X:/f{i}.jpg", rel_path=f"f{i}.jpg",
                          path_hash=f"r{i}", inode=i, size=1, mtime=1,
                          kind="photo", status=st)
        D.enc_done(conn, 1, D.MODEL_SEMANTIC)
        D.enc_done(conn, 1, D.MODEL_FACE)
        D.enc_skipped_dup(conn, 3, D.MODEL_SEMANTIC)
        conn.commit()
        counted = reset_for_full_reprocess(conn, cfg, dry_run=True)
        check("dry-run 只统计不写库", counted["files_done"] == 2, str(counted))
        check("dry-run 后状态没变", _status_of(cfg, 1) == "done")

        reset_for_full_reprocess(conn, cfg)
        check("done 被重新排成 pending", _status_of(cfg, 1) == "pending")
        check("skipped_dup 原样保留", _status_of(cfg, 3) == "skipped_dup")
        check("missing 原样保留", _status_of(cfg, 4) == "missing")
        n_enc = conn.execute(
            "SELECT COUNT(*) FROM file_encoding WHERE status='done'").fetchone()[0]
        check("done 的编码结论被清掉", n_enc == 0, f"got {n_enc}")
        n_skip = conn.execute(
            "SELECT COUNT(*) FROM file_encoding WHERE status='skipped_dup'").fetchone()[0]
        check("skipped_dup 的编码结论保留", n_skip == 1, f"got {n_skip}")
    finally:
        conn.close()


def _status_of(cfg, file_id: int) -> str:
    conn = D.connect(cfg.db_path, cfg=cfg, readonly=True)
    try:
        return str(conn.execute("SELECT status FROM files WHERE file_id=?",
                                (file_id,)).fetchone()[0])
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
def test_terms() -> None:
    section("6. 界面文案清理（规格 §2 术语表）")
    bad = []
    for k, v in U.TEXTS.items():
        for w in U.BANNED_IN_UI:
            if w in v:
                bad.append((k, w))
    check("文案字典里没有禁用词", not bad, str(bad[:5]))
    check("所有文案都有值", not U.missing_keys(), str(U.missing_keys()[:5]))

    for code in ("scan", "extract", "encode", "face_detect", "face_encode",
                 "cluster", "migrate", "reconcile"):
        name = U.stage_name(code)
        check(f"阶段 {code} 有业务词", bool(name) and name != code, name)

    keys_needed = ["scan.new", "scan.all", "act.face_detect", "act.face_encode",
                   "act.cluster", "act.reconcile", "task.busy", "task.cancel",
                   "wall.title", "set.title", "maint.title", "about.title",
                   "nav.wall", "nav.groups", "nav.search", "nav.tasks",
                   "nav.settings", "nav.maintain", "nav.about"]
    missing = [k for k in keys_needed if k not in U.TEXTS]
    check("界面要用的文案键都存在", not missing, str(missing))


def test_media_roots(cfg) -> None:
    section("7. 素材目录增删改（多目录）")
    from src import media_roots as MR

    tmp = Path(cfg.root)
    # 前面的「路径变更」测试会通过 _update_registry 改写登记表，
    # 这里先清掉覆盖层，从「只有一个默认目录」的干净状态开始。
    from src.common import CONFIG_LOCAL_FILENAME

    ov = tmp / CONFIG_LOCAL_FILENAME
    if ov.exists():
        ov.unlink()
    a = tmp / "mediaA"
    # 前面的迁移测试把内存里的 media_root 改成了 mediaB，这里拨回来
    cfg.override("media_root", str(a))
    b = tmp / "mediaB"
    c = b / "inner"
    for d in (a, b, c):
        d.mkdir(parents=True, exist_ok=True)

    n0 = len(MR.list_roots(cfg))
    check("默认至少有一个登记的目录", n0 >= 1, str(n0))

    MR.add(cfg, str(b), label="第二个")
    roots = MR.list_roots(cfg)
    check("添加成功", any(r["path"].endswith("mediaB") for r in roots), str(roots))
    check("新目录 enabled 默认开", next(r for r in roots if r["path"].endswith("mediaB"))["enabled"])

    try:
        MR.add(cfg, str(b))
        check("重复添加被拒绝", False, "没抛异常")
    except ValueError:
        check("重复添加被拒绝", True)

    try:
        MR.add(cfg, str(c))
        check("嵌套目录被拒绝", False, "没抛异常")
    except ValueError:
        check("嵌套目录被拒绝", True)

    try:
        MR.add(cfg, str(tmp / "not_exist_xyz"))
        check("不存在的目录被拒绝", False, "没抛异常")
    except ValueError:
        check("不存在的目录被拒绝", True)

    MR.set_enabled(cfg, str(b), False)
    e = next(r for r in MR.list_roots(cfg) if r["path"].endswith("mediaB"))
    check("停用生效", e["enabled"] is False)
    check("停用后不在扫描列表里",
          not any(p.endswith("mediaB") for p in MR.enabled_roots(cfg)),
          str(MR.enabled_roots(cfg)))

    MR.update(cfg, str(b), label="改过备注")
    e = next(r for r in MR.list_roots(cfg) if r["path"].endswith("mediaB"))
    check("备注可修改", e["label"] == "改过备注", e["label"])

    n_before = len(MR.list_roots(cfg))
    MR.remove(cfg, str(b))
    check("移除成功", len(MR.list_roots(cfg)) == n_before - 1)
    check("移除不动文件记录（数据保留）", _n_ok(cfg) >= 3, str(_n_ok(cfg)))

    try:
        MR.remove(cfg, str(a))
        check("最后一个目录不能被移除", False, "没抛异常")
    except ValueError:
        check("最后一个目录不能被移除", True)


# --------------------------------------------------------------------------- #
def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="mms_v53_"))
    media = tmp / "mediaA"
    media.mkdir(parents=True, exist_ok=True)
    try:
        cfg = make_cfg(tmp, media)
        # 注意：迁移脚本目录在真正的项目根下，而 cfg.root 已被改到临时目录
        # （防止测试写坏生产 config.local.yaml），所以这里显式传 ROOT。
        D.ensure_schema(cfg.db_path, ROOT, cfg)
        print(f"临时库：{cfg.db_path}")

        test_tasks(cfg)
        test_settings(cfg)
        test_migrate(cfg)
        test_cluster()
        test_reset_reprocess(cfg)
        test_media_roots(cfg)
        test_terms()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 74)
    print(f"PASS={PASS}  FAIL={FAIL}")
    if FAILED:
        print("失败项：" + "；".join(FAILED))
    print("=" * 74)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
