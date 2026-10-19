# 监视 03_extract 跑完，然后自动串联下一步。
#
# 为什么不把 02_dedup 也塞进这条链：
#   02_dedup 用写连接、且只在循环末尾 commit 一次，会和 03_extract 抢 library.db 的写锁。
#   所以本脚本的顺序是：等 03 → 02_dedup（已修好的快速版）→ 04_encode。
#
# 用法：pwsh -NoProfile -File tmp\monitor_chain.ps1
$ErrorActionPreference = "Continue"
$root = "D:\DATA\PROJECT\建设多媒体整理系统"
Set-Location $root

$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:TMP = "$root\tmp"
$env:TEMP = "$root\tmp"
$env:TMPDIR = "$root\tmp"

$PY = "$root\.venv\Scripts\python.exe"
$LOG = "$root\tmp\chain.log"

function Say([string]$m) {
    $line = "[{0}] {1}" -f (Get-Date -Format 'HH:mm:ss'), $m
    Write-Output $line
    Add-Content -Path $LOG -Value $line -Encoding UTF8
}

function ExtractRunning {
    $p = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
         Where-Object { $_.CommandLine -and $_.CommandLine -match '03_extract\.py' }
    return [bool]$p
}

Set-Content -Path $LOG -Value "" -Encoding UTF8
Say "监视链启动，等待 03_extract 结束……"

# ---------- 阶段 1：等 03_extract ----------
$waited = 0
while (ExtractRunning) {
    Start-Sleep -Seconds 30
    $waited += 30
    if ($waited % 300 -eq 0) {
        $frames = (Get-ChildItem "$root\data\nvme\frames" -Recurse -File -ErrorAction SilentlyContinue).Count
        $dirs = (Get-ChildItem "$root\data\nvme\frames" -Directory -ErrorAction SilentlyContinue).Count
        Say "等待中…… 已等 $([math]::Round($waited/60,1)) 分钟；frames 目录=$dirs 关键帧=$frames"
    }
}
Say "03_extract 已结束（等待了 $([math]::Round($waited/60,1)) 分钟）"

# ---------- 阶段 2：核对 03 结果 ----------
Say "--- 03 结果核对 ---"
& $PY 12_maintain.py status --json 2>&1 | Select-String -Pattern "by_status|shots|time_source|next_step" | ForEach-Object { Say $_.Line }
$rc1 = $LASTEXITCODE
Say "status 退出码=$rc1"

# ---------- 阶段 3：02_dedup ----------
Say "--- 开始 02_dedup ---"
& $PY 02_dedup.py 2>&1 | Tee-Object -FilePath "$root\tmp\chain_dedup.log" | Select-Object -Last 20 | ForEach-Object { Say $_ }
Say "02_dedup 退出码=$LASTEXITCODE"

# ---------- 阶段 4：04_encode ----------
Say "--- 开始 04_encode ---"
& $PY 04_encode.py 2>&1 | Tee-Object -FilePath "$root\tmp\chain_encode.log" | Select-Object -Last 25 | ForEach-Object { Say $_ }
Say "04_encode 退出码=$LASTEXITCODE"

# ---------- 收尾 ----------
Say "--- 最终状态 ---"
& $PY 12_maintain.py status --json 2>&1 | Select-String -Pattern "by_status|shots|faces|nvme_used|next_step" | ForEach-Object { Say $_.Line }
& $PY 12_maintain.py reconcile --json 2>&1 | Select-String -Pattern "total_diff|verdict" | ForEach-Object { Say $_.Line }
Say "监视链结束"
