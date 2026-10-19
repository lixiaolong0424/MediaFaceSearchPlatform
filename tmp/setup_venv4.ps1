$ErrorActionPreference = "Continue"
$root = "D:\DATA\PROJECT\建设多媒体整理系统"
$venv = "$root\.venv"
$py   = "$venv\Scripts\python.exe"
$wheels = "$root\tmp\wheels"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:PIP_DISABLE_PIP_VERSION_CHECK = "1"

# 统一使用可达的镜像（tsinghua 当前返回 403）
$IDX  = "https://mirrors.aliyun.com/pypi/simple/"
$HOST = "mirrors.aliyun.com"

function PIP { param([Parameter(ValueFromRemainingArguments=$true)]$Args)
    & $py -m pip install --no-cache-dir --index-url $IDX --trusted-host $HOST `
        --timeout 120 --retries 10 @Args 2>&1 | Select-Object -Last 14
}

Write-Output "### 0. 确保没有残留 python 进程占用 site-packages"
Get-Process python -ErrorAction SilentlyContinue | ForEach-Object {
    Write-Output "  kill PID $($_.Id)"
    try { $_.Kill() } catch {}
}
Start-Sleep -Seconds 3

Write-Output "### 1. 重建干净的 venv（旧环境被并发安装污染，必须删掉）"
if (Test-Path $venv) { Remove-Item $venv -Recurse -Force }
uv venv --python 3.12 --seed $venv 2>&1 | Select-Object -Last 3
& $py -V

Write-Output "### 2. 升级 pip 工具链"
PIP --upgrade pip setuptools wheel

$torchWhl = Join-Path $wheels "torch-2.8.0+cu126-cp312-cp312-win_amd64.whl"
if (-not (Test-Path $torchWhl)) { Write-Output "缺少 $torchWhl"; exit 1 }
Write-Output "### 3. 从本地 wheel 安装 torch（2.7GB，依赖走 aliyun）"
PIP $torchWhl

Write-Output "### 4. 校验 torch 可导入且 CUDA 可用"
& $py -c "import torch; print('torch', torch.__version__, 'cuda_avail', torch.cuda.is_available(), 'cuda', torch.version.cuda)" 2>&1
if ($LASTEXITCODE -ne 0) { Write-Output "!! torch 安装失败，中止"; exit 1 }

Write-Output "### 5. onnxruntime-gpu（必须 GPU 版）"
PIP onnxruntime-gpu

Write-Output "### 6. 语义模型栈"
PIP "numpy<2.3" PyYAML pillow "transformers==4.57.1" accelerate safetensors "sentence-transformers>=5.4.0" qwen-vl-utils

Write-Output "### 7. 检索与工具链"
PIP qdrant-client psutil scenedetect imagehash opencv-python-headless streamlit

Write-Output "### 8. insightface（需要 Cython + numpy 参与构建）"
PIP cython
PIP insightface

Write-Output "### 9. 最终校验"
& $py -c @"
import torch, transformers, onnxruntime as ort, numpy, cv2, qdrant_client
import scenedetect, streamlit, imagehash, sentence_transformers, insightface, PIL, yaml, psutil
print('torch              ', torch.__version__, '| cuda_avail', torch.cuda.is_available(), '|', torch.version.cuda)
print('transformers       ', transformers.__version__)
print('sentence_transformers', sentence_transformers.__version__)
print('onnxruntime-gpu    ', ort.__version__, '|', ort.get_available_providers())
print('numpy              ', numpy.__version__)
print('opencv             ', cv2.__version__)
print('streamlit          ', streamlit.__version__)
print('insightface        ', insightface.__version__ if hasattr(insightface,'__version__') else 'ok')
print('qdrant-client      import ok')
"@ 2>&1

Write-Output "### 10. 冻结依赖清单"
& $py -m pip freeze 2>&1 | Set-Content -Encoding UTF8 "$root\requirements.lock.txt"
Write-Output "### ALL DONE"
