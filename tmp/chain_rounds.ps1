# =============================================================================
# chain_rounds.ps1 · 分轮次全集流水线（照片链 → 视频链）
#
# 为什么分轮次，而不是「一个阶段跑到底再跑下一个」
# ------------------------------------------------
# 实测（2026-10-16/18，E: 是 14TB SATA 机械盘）：
#   照片融合抽帧 7.6~15.7 张/秒（照片 3.235 TB，一趟读盘 8~17 小时）
#   06 检脸 79.6 张/秒（只读 NVMe）· 07 编码 6.6 分钟/1.6 万文件 → 人脸链极快
#   04 语义编码单进程 3.6 张/秒（GPU 利用率仅 65%）→ 与抽帧并行能把它藏进读盘时间里
#
# 关键原则：**抽帧（等机械盘）与语义编码（吃 GPU）并行**，人脸链在两批之间独占 GPU。
#   串行：抽帧 + 编码 + 人脸逐段相加（照片侧实测约 42 小时）
#   并行：max(抽帧, 编码) + 人脸（约 19~28 小时）
#
# ⚠️ 2026-10-18 重要修正：视频分支原来只有一条 `03_extract --kind video`，
#    抽完帧就结束 —— 视频**不会**被编码、不会被检脸，即“抽帧完成”≠“可检索/能上墙”。
#    实测那时 6,001 个抽帧完成的视频里只有 622 个真正编码过，5,380 个连编码记录都没有。
#    新增 -Stage videoall：视频也按轮次跑（抽一批 → 人脸 → 编码），
#    整条链跑完时视频就已经入库、上墙了，不用再手动补 04/06/07。
#
# 用法：
#   pwsh -NoProfile -File tmp\chain_rounds.ps1 -Stage all       # 照片轮次 → 视频轮次（推荐）
#   pwsh -NoProfile -File tmp\chain_rounds.ps1 -Stage videoall  # 只跑视频轮次
#   pwsh -NoProfile -File tmp\chain_rounds.ps1 -Stage videos    # 只抽视频帧（不编码，老行为）
#   pwsh -NoProfile -File tmp\chain_rounds.ps1 -Stage photos
# =============================================================================
param(
    [ValidateSet("photos", "videos", "videoall", "all")]
    [string]$Stage = "all",
    [int]$RoundSize = 25000,        # 照片每轮抽帧数
    [int]$VideoRoundSize = 3000,    # 视频每轮抽帧数（视频单个要 15~30s，轮次得小）
    [int]$Encoders = 1,             # 并行语义编码进程数（实测 2 个只比 1 个快 5%，却多占一倍显存）
    [int]$MaxRounds = 0             # 0 = 不限
)

$ErrorActionPreference = "Continue"
$root = "D:\DATA\PROJECT\建设多媒体整理系统"
Set-Location $root
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:TMP = "$root\tmp"; $env:TEMP = $env:TMP; $env:TMPDIR = $env:TMP
Remove-Item Env:MMS_CONFIG -ErrorAction SilentlyContinue
Remove-Item Env:MMS_CONFIG_OVERLAY -ErrorAction SilentlyContinue

$py = "$root\.venv\Scripts\python.exe"
$logDir = "$root\logs"
$state = "$root\logs\chain_state.tsv"

function Write-State {
    param([string]$Text)
    # ⚠️ 必须用 Write-Host 而不是裸表达式（2026-10-18 修的真实 bug）：
    #   裸写字符串会把它送进**管道**，于是 `$encProcs += Start-Stage ...`
    #   收集到的是「字符串 + Process 对象」混在一起的数组，
    #   后面 `$p.WaitForExit()` 就在字符串上报
    #   "Method invocation failed because [System.String] does not contain a method named 'WaitForExit'"
    #   → 那条「等编码收尾」实际上**从来没生效**，人脸链会和编码抢 GPU。
    Write-Host "[chain] $(Get-Date -Format 'HH:mm:ss') $Text"
    "$(Get-Date -Format o)`t$Text" | Add-Content -Path $state -Encoding UTF8
}

function Invoke-Stage {
    param([string]$Name, [string[]]$ArgLine)
    $log = Join-Path $logDir "chain_$Name.log"
    $err = Join-Path $logDir "chain_$Name.err"
    $t0 = Get-Date
    Write-State "START $Name $($ArgLine -join ' ')"
    $p = Start-Process -FilePath $py -ArgumentList $ArgLine -WorkingDirectory $root `
        -NoNewWindow -Wait -PassThru -RedirectStandardOutput $log -RedirectStandardError $err
    $mins = [math]::Round(((Get-Date) - $t0).TotalMinutes, 1)
    Write-State "END   $Name rc=$($p.ExitCode) ($mins min)"
    return $p
}

function Start-Stage {
    param([string]$Name, [string[]]$ArgLine)
    $log = Join-Path $logDir "chain_$Name.log"
    $err = Join-Path $logDir "chain_$Name.err"
    Write-State "SPAWN $Name $($ArgLine -join ' ')"
    $proc = Start-Process -FilePath $py -ArgumentList $ArgLine -WorkingDirectory $root `
        -NoNewWindow -PassThru -RedirectStandardOutput $log -RedirectStandardError $err
    return $proc
}

function Get-PendingCount {
    param([string]$Kind)
    # tmp\pending.py 与 03_extract.select_targets 同口径：已放弃（死信）的不算，
    # 否则「pending 永远 > 0」会让链误判还有活可干（实测空转过 291 轮）。
    $n = & $py -u "$root\tmp\pending.py" $Kind 2>$null | Select-Object -Last 1
    if (-not $n) { return 0 }
    return [int]$n
}

# --------------------------------------------------------------------------- #
# 收尾：把剩下的语义编码补完，再补一遍人脸，最后对账
# --------------------------------------------------------------------------- #
function Invoke-Finalize {
    param([string]$Tag)
    Write-State "[$Tag] 收尾：补跑语义编码与人脸"
    $tail = @()
    for ($i = 1; $i -le $Encoders; $i++) {
        $tail += Start-Stage "final_encode_${Tag}_$i" @("-u", "04_encode.py", "--shard", "$i/$Encoders")
    }
    foreach ($p in $tail) { if (-not $p.HasExited) { $p.WaitForExit() } }
    Invoke-Stage "final_face_detect_$Tag" @("-u", "06_face_detect.py") | Out-Null
    Invoke-Stage "final_face_encode_$Tag" @("-u", "07_face_encode.py") | Out-Null
}

# --------------------------------------------------------------------------- #
# 分轮次主循环（照片与视频共用一套逻辑，只是 kind 与每轮规模不同）
# --------------------------------------------------------------------------- #
function Invoke-KindRounds {
    param([string]$Kind, [int]$RoundSize)

    $round = 0
    $lastPending = -1
    $stalls = 0
    $maxStalls = 2          # 连续几轮「待处理数没下降」就判定卡住
    while ($true) {
        $round++
        if ($MaxRounds -gt 0 -and $round -gt $MaxRounds) {
            Write-State "[$Kind] 达到 MaxRounds=$MaxRounds，停止"; break
        }

        $pending = Get-PendingCount $Kind
        Write-State "=== [$Kind] 第 $round 轮开始，待处理 $pending ==="
        if ($pending -le 0) { Write-State "[$Kind] 没有待处理，本阶段完成"; break }

        # 停滞守卫（2026-10-18 加，起因是真实事故）：
        # 剩下的都是「反复失败」的文件时，每轮都会重试+再失败，pending 永不归零。
        # 连续 $maxStalls 轮没下降就判定本阶段结束，继续往下走。
        if ($lastPending -ge 0 -and $pending -ge $lastPending) {
            $stalls++
            Write-State "[$Kind] 待处理数未下降（$lastPending → $pending），停滞 $stalls/$maxStalls"
            if ($stalls -ge $maxStalls) {
                Write-State "[$Kind] 判定已到极限（剩余多为反复失败的文件），转入下一阶段"
                break
            }
        } else {
            $stalls = 0
        }
        $lastPending = $pending

        # ① 后台语义编码（消化上一轮产出；第一轮通常没活可干，秒退）
        # 注意：变量名不能叫 $encoders —— PowerShell 变量名不区分大小写，
        # 会与 [int]$Encoders 参数撞名。
        $encProcs = @()
        for ($i = 1; $i -le $Encoders; $i++) {
            $encProcs += Start-Stage "${Kind}_r${round}_encode_$i" @(
                "-u", "04_encode.py", "--limit", "$RoundSize", "--shard", "$i/$Encoders")
        }

        # ② 前台抽帧（唯一碰机械盘的一趟）
        $kindArg = if ($Kind -eq "video") { "video" } else { "photo" }
        Invoke-Stage "${Kind}_r${round}_extract" @(
            "-u", "03_extract.py", "--kind", $kindArg, "--limit", "$RoundSize") | Out-Null

        # ③ 等语义编码收尾（此时 GPU 独占，人脸链不能同时跑）
        foreach ($p in $encProcs) {
            if ($p -is [System.Diagnostics.Process] -and -not $p.HasExited) { $p.WaitForExit() }
        }
        Write-State "[$Kind] 第 $round 轮：语义编码 worker 全部退出"

        # ④ 人脸链
        Invoke-Stage "${Kind}_r${round}_face_detect" @(
            "-u", "06_face_detect.py", "--limit", "$RoundSize") | Out-Null
        Invoke-Stage "${Kind}_r${round}_face_encode" @("-u", "07_face_encode.py") | Out-Null
    }
    Invoke-Finalize $Kind
}

Write-State "=== chain_rounds start stage=$Stage photo=$RoundSize video=$VideoRoundSize encoders=$Encoders ==="

switch ($Stage) {
    "photos" {
        Invoke-KindRounds -Kind "photo" -RoundSize $RoundSize
        Invoke-Stage "reconcile" @("-u", "12_maintain.py", "reconcile") | Out-Null
    }
    "videos" {
        # 老行为：只抽视频帧，不编码（抽完还得手动跑 04/06/07）
        Invoke-Stage "extract_video" @("-u", "03_extract.py", "--kind", "video") | Out-Null
    }
    "videoall" {
        Invoke-KindRounds -Kind "video" -RoundSize $VideoRoundSize
        Invoke-Stage "reconcile" @("-u", "12_maintain.py", "reconcile") | Out-Null
    }
    "all" {
        # 照片：机械盘上的大头，跑完就有完整可检索的照片库 + 照片墙
        Invoke-KindRounds -Kind "photo" -RoundSize $RoundSize
        Invoke-Stage "reconcile_photo" @("-u", "12_maintain.py", "reconcile") | Out-Null
        # 视频：抽帧与编码交替，跑完即入库、上墙
        Write-State "照片链完成，转入视频链（两者不能并行：都疯狂读同一块机械盘）"
        Invoke-KindRounds -Kind "video" -RoundSize $VideoRoundSize
        Invoke-Stage "reconcile" @("-u", "12_maintain.py", "reconcile") | Out-Null
    }
}

Write-State "=== chain_rounds end stage=$Stage ==="
