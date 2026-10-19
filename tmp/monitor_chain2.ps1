# 监视 03_extract 跑完，然后自动串联：02_dedup(快速版) → 04_encode。
#
# 上一版的两个 bug（已修）：
#   1) 用 `CommandLine -match '03_extract'` 判断进程存活。但 Windows 上
#      python.exe 的 CommandLine 有时是 null → 匹配失败 → 误判「已结束」
#      → 立刻去跑 02_dedup（当时还是 11 小时的旧实现）。现在改为双信号：
#      「stage_status 计数是否在增长」+「python 进程是否还在」，两者都为否才认为结束。
#   2) 没有防呆：误判后无法挽回。现在加 --require-progress 语义：
#      必须观察到 stage_status 计数**增长过**才允许进入下一步。
#
# 另外：02_dedup 用写连接且只在末尾 commit，会和 03 抢 library.db 写锁，
# 所以必须等 03 真正结束才能跑，不能真并行。
#
# 用法：pwsh -NoProfile -File tmp\monitor_chain2.ps1
$ErrorActionPreference = "Continue"
$root = "D:\DATA\PROJECT\建设多媒体整理系统"
Set-Location $root

$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:TMP = "$root\tmp"; $env:TEMP = "$root\tmp"; $env:TMPDIR = "$root\tmp"

$PY = "$root\.venv\Scripts\python.exe"
$LOG = "$root\tmp\chain2.log"
$DB = "$root\data\library.db"

function Say([string]$m) {
    $line = "[{0}] {1}" -f (Get-Date -Format 'HH:mm:ss'), $m
    Write-Output $line
    Add-Content -Path $LOG -Value $line -Encoding UTF8
}

function StageCount {
    # 调独立 helper，避免 PowerShell 内联 python -c 的转义地狱
    $out = & $PY "$root\tmp\extract_count.py" $DB 2>$null
    $parts = "$out".Trim() -split '\s+'
    if ($parts.Count -ge 1 -and $parts[0] -match '^\d+$') { return [int]$parts[0] }
    return -1
}

function FramesCount {
    (Get-ChildItem "$root\data\nvme\frames" -Recurse -File -ErrorAction SilentlyContinue).Count
}

function PyAlive {
    $p = Get-Process python -ErrorAction SilentlyContinue
    return [bool]$p
}

Set-Content -Path $LOG -Value "" -Encoding UTF8
Say "监视链 v2 启动"

$start = StageCount
$frames0 = FramesCount
Say "起点：stage_status(extract)=$start  关键帧=$frames0"

# ---------- 阶段 1：等 03 真正结束 ----------
$waited = 0
$sawProgress = $false
$lastCount = $start
$lastFrames = $frames0
$stagnant = 0

while ($true) {
    Start-Sleep -Seconds 60
    $waited += 60
    $c = StageCount
    $f = FramesCount
    if ($c -gt $start -or $f -gt $frames0) { $sawProgress = $true }
    if ($c -gt $lastCount -or $f -gt $lastFrames) { $stagnant = 0 } else { $stagnant++ }
    $lastCount = $c; $lastFrames = $f

    if ($waited % 600 -eq 0) {
        Say "等待中…… $([math]::Round($waited/60,1)) 分钟  stage=$c  关键帧=$f  " +
            "存活=$(if(PyAlive){'是'}else{'否'})  停滞${stagnant}轮"
    }

    # 结束判定：需要「确实见过进展」+「库计数停滞 3 轮」+「没有 python 进程」
    if ($sawProgress -and $stagnant -ge 3 -and -not (PyAlive)) {
        Say "判定 03 已结束（曾见到进展；库计数停止增长；无 python 进程）"
        break
    }
    # 安全阀：超过 30 小时强制继续，避免无限等
    if ($waited -ge 108000) {
        Say "[警告] 等待超过 30 小时，强制继续"
        break
    }
}

if (-not $sawProgress) {
    Say "[警告] 全程没见到 stage_status 或关键帧增长——03 可能根本没在跑。"
    Say "        为安全起见不自动进入下一步（避免用旧版 dedup 又跑 11 小时）。"
    exit 1
}

Say "--- 03 结果 ---"
& $PY 12_maintain.py status --json 2>&1 | Select-String -Pattern "by_status|shots|time_source" | ForEach-Object { Say $_.Line }

# ---------- 阶段 2：02_dedup（快速版）----------
Say "--- 开始 02_dedup（快速版 tests\dedup_fast.py）---"
& $PY tests\dedup_fast.py 2>&1 | Tee-Object -FilePath "$root\tmp\chain2_dedup.log" |
    Select-Object -Last 15 | ForEach-Object { Say $_ }
Say "dedup 退出码=$LASTEXITCODE"

# ---------- 阶段 3：04_encode ----------
Say "--- 开始 04_encode ---"
& $PY 04_encode.py 2>&1 | Tee-Object -FilePath "$root\tmp\chain2_encode.log" |
    Select-Object -Last 25 | ForEach-Object { Say $_ }
Say "encode 退出码=$LASTEXITCODE"

# ---------- 收尾 ----------
Say "--- 最终 ---"
& $PY 12_maintain.py status --json 2>&1 | Select-String -Pattern "by_status|shots|faces|nvme_used" | ForEach-Object { Say $_.Line }
& $PY 12_maintain.py reconcile --json 2>&1 | Select-String -Pattern "total_diff|verdict" | ForEach-Object { Say $_.Line }
Say "监视链 v2 结束"
