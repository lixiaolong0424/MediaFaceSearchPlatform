# =============================================================================
# chain_full.ps1 · 全集流水线串联（照片链 / 视频链）
#
# 为什么要串成脚本（2026-10-16 实测的硬件约束）：
#   E: 是 14TB SATA 机械盘，照片 3.235 TB / 视频 6.104 TB。
#   照片一趟读盘 6~9 小时；视频全集要跑好几天。
#   一次会话跑不完，必须能无人值守地一路跑下去、断了能接着跑。
#
# 照片链顺序（有意为之）：
#   03 --kind photo   融合去重 + EXIF + NVMe 缓存图（唯一碰机械盘的一趟）
#   06_face_detect    F1+F2 检脸（只读 NVMe 缓存图）
#   07_face_encode    F3 ArcFace + 缩略图 + Qdrant → 照片墙这时才有内容
#   04_encode         语义编码（只读 NVMe 缓存图）→ Tab2 文字/图片检索这时才可用
#
#   把 06/07 排在 04 前面是刻意的：照片墙（Tab1）与「精确找人」只依赖人脸管线，
#   先跑就能在夜里更早看到可用的界面，语义检索晚一两个小时不影响验收。
#
# 视频链单独跑（03 --kind video），与照片链并行：
#   ffmpeg 解码是 CPU/机械盘密集，人脸与语义编码是 GPU 密集，互不抢资源。
#
# 用法：
#   pwsh -NoProfile -File tmp\chain_full.ps1 -Stage photos
#   pwsh -NoProfile -File tmp\chain_full.ps1 -Stage videos
# =============================================================================
param(
    [ValidateSet("photos", "videos")]
    [string]$Stage = "photos",
    [switch]$SkipExtract   # 照片已抽完时，只补跑后面的阶段
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
$state = "$root\logs\chain_state.tsv"
$logDir = "$root\logs"

function Invoke-Stage {
    param([string]$Name, [string[]]$ArgLine)
    $log = Join-Path $logDir "chain_$Name.log"
    $err = Join-Path $logDir "chain_$Name.err"
    "[chain] $(Get-Date -Format 'HH:mm:ss') START $Name"
    $t0 = Get-Date
    $p = Start-Process -FilePath $py -ArgumentList $ArgLine -WorkingDirectory $root `
        -NoNewWindow -Wait -PassThru -RedirectStandardOutput $log -RedirectStandardError $err
    $mins = [math]::Round(((Get-Date) - $t0).TotalMinutes, 1)
    "[chain] $(Get-Date -Format 'HH:mm:ss') END   $Name rc=$($p.ExitCode) ($mins min)"
    "$(Get-Date -Format o)`t$Name`t$($p.ExitCode)`t$mins" | Add-Content -Path $state -Encoding UTF8
    return $p.ExitCode
}

"$(Get-Date -Format o)`t=== chain start stage=$Stage ===" | Add-Content -Path $state -Encoding UTF8

if ($Stage -eq "photos") {
    if (-not $SkipExtract) {
        Invoke-Stage "extract_photo" @("-u", "03_extract.py", "--kind", "photo") | Out-Null
    }
    Invoke-Stage "face_detect" @("-u", "06_face_detect.py") | Out-Null
    Invoke-Stage "face_encode" @("-u", "07_face_encode.py") | Out-Null
    Invoke-Stage "encode" @("-u", "04_encode.py") | Out-Null
    Invoke-Stage "reconcile" @("-u", "12_maintain.py", "reconcile") | Out-Null
} else {
    Invoke-Stage "extract_video" @("-u", "03_extract.py", "--kind", "video") | Out-Null
}

"$(Get-Date -Format o)`t=== chain end stage=$Stage ===" | Add-Content -Path $state -Encoding UTF8
"[chain] all done for stage=$Stage"
