# 重排序的图像像素预算 → 速度与区分度 扫描
# 背景：checkpoint 默认 MAX_PIXELS = 1280*32*32 ≈ 1.31MP，实测 0.225 秒/候选，
# 60 条候选要 13.5 秒/查询 —— 交互上太慢。重排序只作用于查询期、不动库里的向量，
# 所以降这个值比降 semantic.max_pixels 安全得多。这里量清楚代价再定。
$ErrorActionPreference = "Continue"
$root = "D:\DATA\PROJECT\建设多媒体整理系统"
Set-Location $root
$env:PYTHONUTF8 = "1"; $env:PYTHONIOENCODING = "utf-8"
$env:TMP = "$root\tmp"; $env:TEMP = $env:TMP; $env:TMPDIR = $env:TMP
$py = "$root\.venv\Scripts\python.exe"

# 注意：像素预算是**像素总数**，权重目录里是 边长*32*32。
# 1280*32*32 = 1310720 是 checkpoint 默认。
$cases = @(
    @{ label = "1280*32*32 (checkpoint 默认, 1.31MP)"; px = $null },
    @{ label = "1024*32*32 (1.05MP)"; px = 1024 * 32 * 32 },
    @{ label = "768*32*32  (0.60MP)"; px = 768 * 32 * 32 },
    @{ label = "512*32*32  (0.27MP)"; px = 512 * 32 * 32 },
    @{ label = "384*32*32  (0.15MP)"; px = 384 * 32 * 32 }
)
foreach ($c in $cases) {
    Write-Host "=== rerank_max_pixels = $($c.label) ==="
    $a = @("-u", "05_rerank.py", "--text", "人物", "--benchmark", "--k", "40")
    if ($c.px) { $a += @("--rerank-max-pixels", "$($c.px)") }
    & $py @a 2>&1 |
        Select-String -Pattern "相关=|sec_per_doc|docs_per_sec|eta_60_docs|eta_400_docs|n_docs" |
        Select-Object -First 8 | ForEach-Object { $_.Line.Trim() }
    Write-Host ""
}
