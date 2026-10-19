@echo off
REM ============================================================================
REM  素材人脸检索系统 . start.bat
REM
REM  为什么不用 Windows 服务跑 Streamlit：Session 0 隔离会让 explorer /select,
REM  等桌面交互型 subprocess 静默失败或弹到不可见会话。正确做法就是双击启动。
REM
REM  重要：本文件必须是 CRLF 行尾。cmd.exe 无法解析 LF-only 的 .bat，
REM        会报 'XXX' is not recognized as an internal or external command。
REM ============================================================================
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul 2>&1
cd /d "%~dp0"

set "PROJECT_ROOT=%~dp0"
if "%PROJECT_ROOT:~-1%"=="\" set "PROJECT_ROOT=%PROJECT_ROOT:~0,-1%"

REM --- PYTHONUTF8=1：全局 UTF-8 模式，防止中文路径乱码 ------------------------
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "MMS_PROJECT_ROOT=%PROJECT_ROOT%"

REM --- TMP/TEMP/TMPDIR 指向项目 tmp，避免 C 盘空间不足 ------------------------
if not exist "%PROJECT_ROOT%\tmp" mkdir "%PROJECT_ROOT%\tmp" >nul 2>&1
set "TMP=%PROJECT_ROOT%\tmp"
set "TEMP=%PROJECT_ROOT%\tmp"
set "TMPDIR=%PROJECT_ROOT%\tmp"

set "PY=%PROJECT_ROOT%\.venv\Scripts\python.exe"

echo ============================================================================
echo   素材人脸检索系统
echo   项目根目录 : %PROJECT_ROOT%
echo ============================================================================
echo.

if not exist "%PY%" (
    echo [错误] 未找到虚拟环境：
    echo         %PY%
    echo.
    echo 请先建立环境，二选一：
    echo     uv venv --python 3.12 --seed .venv
    echo     pwsh -NoProfile -File tmp\setup_venv4.ps1
    echo.
    goto :done
)

REM --- 启动前准备：Streamlit 凭据 + 端口守卫 ---------------------------------
REM 必须放在自检之前：重复双击时端口当然被占，那是「已经在运行」而非故障。
REM 不做这一步，首次双击会停在 Streamlit 的 "Email:" 交互提示上（看起来就是启动不了），
REM 重复双击则会被自检报成「端口不可用」失败。
echo [1/3] 启动前准备（Streamlit 凭据 / 端口守卫）...
"%PY%" "src\startup_prep.py"
set "PREP_RC=%errorlevel%"
if "%PREP_RC%"=="1" (
    echo [中止] 启动前准备失败。
    goto :done
)
if "%PREP_RC%"=="3" (
    echo.
    echo 界面已经在运行，正在打开浏览器...
    start "" "http://127.0.0.1:8501"
    echo 若要重启，请先关闭已有的 start.bat 窗口。
    goto :done
)

REM --- 启动自检：任一项 FAIL 就中止（规格第十一章）--------------------------
echo.
echo [2/3] 启动自检...
"%PY%" "00_bootstrap.py"
if errorlevel 1 (
    echo.
    echo [中止] 自检未通过。请按上面的提示修复后重试。
    echo.
    goto :done
)

REM --- Qdrant 容器（Docker 是规格允许的例外）--------------------------------
echo.
echo [3/3] 检查 Qdrant 容器并启动界面...
where docker >nul 2>&1
if errorlevel 1 (
    echo [警告] 未找到 docker 命令，跳过容器检查。
) else (
    set "QD_RUNNING="
    for /f "delims=" %%L in ('docker ps --filter "name=qdrant" --filter "status=running" --format "{{.Names}}" 2^>nul') do (
        if /i "%%L"=="qdrant" set "QD_RUNNING=1"
    )
    if defined QD_RUNNING (
        echo        qdrant 正在运行。
    ) else (
        echo        qdrant 未运行，尝试启动...
        docker start qdrant >nul 2>&1
        if errorlevel 1 (
            echo [警告] 启动 qdrant 失败。首次部署请执行：
            echo         docker run -d --name qdrant -p 127.0.0.1:7333:6333 -p 127.0.0.1:7334:6334 -v qdrant_storage:/qdrant/storage -v qdrant_snapshots:/qdrant/snapshots -e QDRANT__TELEMETRY_DISABLED=true --restart unless-stopped qdrant/qdrant
        ) else (
            echo        qdrant 已启动，等待就绪...
            timeout /t 5 /nobreak >nul
        )
    )
)

REM --- 启动 Streamlit --------------------------------------------------------
echo.
echo 界面地址：http://127.0.0.1:8501
echo 关闭本窗口即停止服务。
echo.
set "STREAMLIT_SERVER_HEADLESS=false"
set "STREAMLIT_BROWSER_GATHER_USAGE_STATS=false"
set "STREAMLIT_SERVER_ADDRESS=127.0.0.1"
set "STREAMLIT_SERVER_PORT=8501"

"%PY%" -m streamlit run "app.py" ^
    --server.address=127.0.0.1 ^
    --server.port=8501 ^
    --server.headless=false ^
    --browser.gatherUsageStats=false ^
    --server.maxUploadSize=1024

echo.
echo 服务已停止（退出码 %errorlevel%）。

:done
echo.
pause
endlocal
