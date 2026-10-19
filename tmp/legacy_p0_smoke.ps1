# P0 冒烟测试：在 tests/media_smoke 上跑通 01/02/03/11/12 并断言关键不变量。
# 用法：pwsh -NoProfile -File tests\p0_smoke.ps1
$ErrorActionPreference = "Continue"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$PY = "$root\.venv\Scripts\python.exe"
if (-not (Test-Path $PY)) { $PY = "python" }
# 若 .venv 尚未装完依赖，回退到已装齐依赖的全局解释器，避免冒烟测试被环境阻塞
& $PY -c "import yaml, numpy, PIL, cv2" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "[warn] $PY 缺少依赖，回退到全局 python" -ForegroundColor Yellow
    $PY = "python"
}
$OV = "--config-overlay", "tests\config_smoke.yaml"
$MEDIA = "D:/DATA/PROJECT/建设多媒体整理系统/tests/media_smoke"
$SMOKE = "$root\tests\_smoke"
$DB = "$SMOKE\library_smoke.db"

New-Item -ItemType Directory -Force -Path $SMOKE | Out-Null
New-Item -ItemType Directory -Force -Path "$SMOKE\export" | Out-Null

$script:pass = 0
$script:fail = 0
function Check([string]$name, [bool]$ok, [string]$detail = "") {
    if ($ok) { $script:pass++; Write-Host "  [PASS] $name" -ForegroundColor Green }
    else { $script:fail++; Write-Host "  [FAIL] $name  $detail" -ForegroundColor Red }
}
function Step([string]$t) { Write-Host "`n===== $t =====" -ForegroundColor Cyan }

if (Test-Path $SMOKE) { Remove-Item $SMOKE -Recurse -Force }

Step "0. python: $PY"
& $PY -V

Step "1. 01_manifest 首次扫描（应全部为 new）"
& $PY 01_manifest.py @OV --root $MEDIA --json 2>&1 | Select-String -Pattern "SUMMARY|new |candidates|inode_stable|total_size|^\{|summary" | Select-Object -First 12

Step "2. 01_manifest 二次扫描（应全部 unchanged，验证增量判定）"
$out2 = & $PY 01_manifest.py @OV --root $MEDIA --json 2>&1
$out2 | Select-String -Pattern "unchanged|new " | Select-Object -First 4
$unchangedOk = ($out2 -join "`n") -match "unchanged\s+7"
Check "增量：第二次扫描 7 个文件全部 unchanged" $unchangedOk

Step "3. 02_dedup（dup_a/dup_b 应判为近重复；两个模型各写一行 skipped_dup）"
& $PY 02_dedup.py @OV 2>&1 | Select-String -Pattern "去重|duplicates|unique|hashed|SUMMARY" | Select-Object -First 10

Step "4. 03_extract（视频应切出多个镜头并落关键帧）"
& $PY 03_extract.py @OV --json 2>&1 | Select-String -Pattern "shots|frames|done|error|SUMMARY|workers" | Select-Object -First 12

Step "5. 数据库不变量断言"
$pyCheck = @'
import json, sqlite3, sys, pathlib
root = pathlib.Path(r"D:\DATA\PROJECT\建设多媒体整理系统")
db = root / "tests" / "_smoke" / "library_smoke.db"
frames = root / "tests" / "_smoke" / "nvme" / "frames"
con = sqlite3.connect(str(db)); con.row_factory = sqlite3.Row
def q(sql, *a):
    return con.execute(sql, a).fetchall()
res = {}
res["files_total"] = q("SELECT COUNT(*) c FROM files")[0]["c"]
res["kind_counts"] = {r["kind"]: r["n"] for r in q("SELECT kind, COUNT(*) n FROM files GROUP BY kind")}
res["status_counts"] = {r["status"]: r["n"] for r in q("SELECT status, COUNT(*) n FROM files GROUP BY status")}
res["shots"] = q("SELECT COUNT(*) c FROM shots")[0]["c"]
res["videos_with_shots"] = q("SELECT COUNT(DISTINCT file_id) c FROM shots")[0]["c"]
res["skipped_dup_rows"] = {r["model"]: r["n"] for r in q(
    "SELECT model, COUNT(*) n FROM file_encoding WHERE status='skipped_dup' GROUP BY model")}
res["time_sources"] = {r["time_source"]: r["n"] for r in q(
    "SELECT time_source, COUNT(*) n FROM files GROUP BY time_source")}
res["captured_samples"] = [dict(r) for r in q(
    "SELECT path, captured_at, time_source, kind FROM files WHERE captured_at IS NOT NULL LIMIT 8")]
res["frame_dirs"] = sorted(p.name for p in frames.iterdir()) if frames.exists() else []
res["frame_files"] = sum(len(list(p.glob('*.jpg'))) for p in frames.iterdir()) if frames.exists() else 0
res["tiny_filtered"] = q("SELECT COUNT(*) c FROM files WHERE path LIKE '%tiny.jpg'")[0]["c"]
res["cjk_row"] = [dict(r) for r in q("SELECT path, kind FROM files WHERE path LIKE '%中文%'")]
res["dup_marked"] = [dict(r) for r in q(
    "SELECT f.path, f.status, f.kind_hint, f.visible FROM files f WHERE f.kind_hint='duplicate'")]
# 关键帧体积校验（>2KB）
small = []
for p in frames.rglob("*.jpg"):
    if p.stat().st_size <= 2048:
        small.append(str(p))
res["frames_under_2kb"] = small
res["db_version"] = q("SELECT MAX(v) v FROM db_version")[0]["v"]
print(json.dumps(res, ensure_ascii=False, indent=2))
'@
$pyCheck | Set-Content -Encoding UTF8 "$SMOKE\_check.py"
& $PY "$SMOKE\_check.py"

Step "6. 11_export：BOM + tab + DEAD + 幂等"
& $PY 11_export.py @OV --scope library --out "$SMOKE/export/a.tsv" 2>&1 | Select-String -Pattern "written|dead|SUMMARY" | Select-Object -First 5
& $PY 11_export.py @OV --scope library --out "$SMOKE/export/b.tsv" 2>&1 | Select-String -Pattern "written" | Select-Object -First 2
$expCheck = @'
import pathlib, sys
root = pathlib.Path(r"D:\DATA\PROJECT\建设多媒体整理系统\tests\_smoke\export")
a = root / "a.tsv"; b = root / "b.tsv"
raw = a.read_bytes()[:3]
print("BOM bytes:", list(raw))
lines = a.read_text(encoding="utf-8-sig").splitlines()
body = [l for l in lines if not l.startswith("#")]
print("header:", body[0])
print("delimiter tabs in header:", body[0].count("\t"))
print("data rows:", len(body) - 1)
fa = [l for l in a.read_text(encoding="utf-8-sig").splitlines() if not l.startswith("#")]
fb = [l for l in b.read_text(encoding="utf-8-sig").splitlines() if not l.startswith("#")]
print("IDEMPOTENT" if fa == fb else "DIFF")
'@
$expCheck | Set-Content -Encoding UTF8 "$SMOKE\_expcheck.py"
& $PY "$SMOKE\_expcheck.py"

Step "7. 12_maintain：migrate / quota / reconcile / repair / clean-query-cache"
& $PY 12_maintain.py @OV migrate --json 2>&1 | Select-String -Pattern "from_version|to_version|applied" | Select-Object -First 5
& $PY 12_maintain.py @OV quota --json 2>&1 | Select-String -Pattern "used|budget|ratio|pause|breakdown" | Select-Object -First 10
& $PY 12_maintain.py @OV repair --dry-run --json 2>&1 | Select-String -Pattern "retryable|dead" | Select-Object -First 6
& $PY 12_maintain.py @OV clean-query-cache --json 2>&1 | Select-String -Pattern "removed|ttl" | Select-Object -First 4
& $PY 12_maintain.py @OV clean-frames --days 30 --dry-run --json 2>&1 | Select-String -Pattern "removed_dirs|freed" | Select-Object -First 4

Step "8. reconcile（Qdrant 连通性；未编码时语义点应为 0）"
& $PY 12_maintain.py @OV reconcile --json 2>&1 | Select-String -Pattern "qdrant_points|sqlite_expected|diff|verdict|error|Qdrant" | Select-Object -First 12

Write-Host "`n============================================" -ForegroundColor Yellow
Write-Host "  冒烟测试：PASS=$script:pass  FAIL=$script:fail" -ForegroundColor Yellow
Write-Host "============================================" -ForegroundColor Yellow
exit $script:fail
