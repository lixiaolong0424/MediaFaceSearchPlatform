# 监视 03_extract 跑完，然后串联：02_dedup(快速版) → 04_encode。
#
# 演进历史（每次都是被真实故障逼出来的）：
#   v1 用 `CommandLine -match '03_extract'` 判断进程存活 —— 但 Windows 上
#      python.exe 的 CommandLine 常为 null → 误判「已结束」→ 立刻跑了当时
#      还是 11 小时的旧 dedup。已废弃。
#   v2 改为「库计数停滞 3 轮 + 无 python 进程」。但这仍不安全：一个超长视频
#      解码 3 分钟以上就会误判，然后去跑 dedup，而 03 还在写库 → 写锁冲突。
#   v3（本版）判定优先级：
#      (a) 首选「显式完成」：无 python 进程 且 剩余待处理(pending/error) = 0
#          —— 这是唯一无歧义的完成信号
#      (b) 退而求其次：库计数与关键帧双双停滞 ≥ 10 分钟（10 轮）且无 python 进程
#      另加「启动后 20 分钟内必须见到进展」的防呆，否则直接退出不进入下一步。
#
# 为什么必须等 03 真正结束才跑 dedup：
#   02_dedup / dedup_fast 用写连接且按批提交，会和 03 抢 library.db 的写锁；
#   两者并发还会争同一块磁盘的 I/O（实测 dedup 从 46 张/秒掉到 13 张/秒）。
#
# 用法：pwsh -NoProfile -File tmp\monitor_chain3.ps1
$ErrorActionPreference = "Continue"
$root = "D:\DATA\PROJECT\建设多媒体整理系统"
Set-Location $root

$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:TMP = "$root\tmp"; $env:TEMP = "$root\tmp"; $env:TMPDIR = "$root\tmp"

$PY = "$root\.venv\Scripts\python.exe"
$LOG = "$root\tmp\chain3.log"
$DB = "$root\data\library.db"
$HELPER = "$root\tmp\extract_count.py"

$STALL_LIMIT = 10          # 连续停滞多少轮（每轮 60s）才认定结束
$BOOTSTRAP_GRACE = 20      # 启动后多少分钟内必须见到进展

function Say([string]$m) {
    $line = "[{0}] {1}" -f (Get-Date -Format 'HH:mm:ss'), $m
    Write-Output $line
    Add-Content -Path $LOG -Value $line -Encoding UTF8
}

function Counts {
    # helper 输出 "stage total"；这里再加 pending 数，用于显式完成判定
    $out = & $PY $HELPER $DB 2>$null
    $p = "$out".Trim() -split '\s+'
    $stage = if ($p.Count -ge 1 -and $p[0] -match '^\d+$') { [int]$p[0] } else { -1 }
    $total = if ($p.Count -ge 2 -and $p[1] -match '^\d+$') { [int]$p[1] } else { -1 }
    return @($stage, $total)
}

function Pending {
    $out = & $PY "$root\tmp\pending_count.py" $DB 2>$null
    if ("$out".Trim() -match '^\d+$') { return [int]"$out".Trim() }
    return -1
}

function FrameCount {
    (Get-ChildItem "$root\data\nvme\frames" -Recurse -File -ErrorAction SilentlyContinue).Count
}

function PyAlive {
    [bool](Get-Process python -ErrorAction SilentlyContinue)
}

Set-Content -Path $LOG -Value "" -Encoding UTF8
Say "监视链 v3 启动（显式完成优先 + 10 分钟停滞兜底 + 20 分钟防呆）"

$c0 = Counts
$stage0 = $c0[0]; $total = $c0[1]
$pend0 = Pending
$fr0 = FrameCount
Say "起点：stage=$stage0  total=$total  pending=$pend0  关键帧=$fr0"

$waited = 0
$sawProgress = $false
$stagnant = 0
$lastStage = $stage0
$lastFrames = $fr0

while ($true) {
    Start-Sleep -Seconds 60
    $waited += 60
    $c = Counts
    $stage = $c[0]
    $fr = FrameCount
    $pend = Pending
    $alive = PyAlive

    if ($stage -gt $stage0 -or $fr -gt $fr0) { $sawProgress = $true }
    if ($stage -gt $lastStage -or $fr -gt $lastFrames) { $stagnant = 0 } else { $stagnant++ }
    $lastStage = $stage; $lastFrames = $fr

    if ($waited % 300 -eq 0) {
        Say ("等待中 {0:N0} 分钟  stage={1}  pending={2}  关键帧={3}  " +
             "存活={4}  停滞={5}轮") -f ($waited/60), $stage, $pend, $fr,
             $(if ($alive) { "是" } else { "否" }), $stagnant
    }

    # (a) 显式完成：无 python 进程 且 没有待处理文件
    if (-not $alive -and $pend -eq 0 -and $pend -ne -1) {
        Say "判定 03 完成（显式：无 python 进程 且 pending=0）"
        break
    }
    # (b) 兜底：长时间停滞 且 无 python 进程
    if ($stagnant -ge $STALL_LIMIT -and -not $alive) {
        Say "判定 03 结束（兜底：计数停滞 $STALL_LIMIT 轮=$([math]::Round($STALL_LIMIT)) 分钟 且无 python 进程）"
        Say "[注意] pending 仍为 $pend，可能还有未处理文件——进入下一步前会先记入日志"
        break
    }
    # 防呆：启动后迟迟没有进展
    if (-not $sawProgress -and $waited -ge ($BOOTSTRAP_GRACE * 60)) {
        Say "[中止] 启动 $BOOTSTRAP_GRACE 分钟内未见到任何进展（stage 与关键帧都没涨）。"
        Say "       03 可能没在跑。为安全起见不进入下一步。"
        exit 1
    }
    if ($waited -ge 172800) { Say "[警告] 等待超过 48 小时，强制继续"; break }
}

Say "--- 03 结果 ---"
& $PY 12_maintain.py status --json 2>&1 |
    Select-String -Pattern "by_status|shots|time_source" | ForEach-Object { Say $_.Line }

Say "--- 开始 02_dedup（快速版 tests\dedup_fast.py）---"
& $PY tests\dedup_fast.py 2>&1 | Tee-Object -FilePath "$root\tmp\chain3_dedup.log" |
    Select-Object -Last 15 | ForEach-Object { Say $_ }
Say "dedup 退出码=$LASTEXITCODE"

Say "--- 开始 04_encode ---"
& $PY 04_encode.py 2>&1 | Tee-Object -FilePath "$root\tmp\chain3_encode.log" |
    Select-Object -Last 25 | ForEach-Object { Say $_ }
Say "encode 退出码=$LASTEXITCODE"

Say "--- 最终 ---"
& $PY 12_maintain.py status --json 2>&1 |
    Select-String -Pattern "by_status|shots|faces|nvme_used" | ForEach-Object { Say $_.Line }
& $PY 12_maintain.py reconcile --json 2>&1 |
    Select-String -Pattern "total_diff|verdict" | ForEach-Object { Say $_.Line }
Say "监视链 v3 结束"
