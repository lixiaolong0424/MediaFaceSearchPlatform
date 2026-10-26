@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"
title 推送到远端
echo ============================================================
echo   素材人脸检索系统 · 一键推送（github + gitcode）
echo ============================================================
echo.

rem ---------- 0) 身份检查 ----------
git config user.name >nul 2>&1 || (echo [错误] 未配置 git user.name & goto :fail)
echo [0/5] 提交身份: 
git config user.name
git config user.email
echo.

rem ---------- 1) 清理过期的 github 主机指纹 ----------
echo [1/5] 检查 known_hosts...
ssh-keygen -F github.com >nul 2>&1
if errorlevel 1 (
  echo     没有 github.com 记录，稍后自动接受
) else (
  ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=yes -T git@github.com >nul 2>&1
  if errorlevel 255 (
    echo     主机指纹异常，重建记录...
    ssh-keygen -R github.com >nul 2>&1
  )
)
ssh-keygen -F gitcode.com >nul 2>&1 || ssh-keygen -R gitcode.com >nul 2>&1
echo.

rem ---------- 2) 提交本地改动 ----------
echo [2/5] 提交本地改动...
git add -A
git diff --cached --quiet
if errorlevel 1 (
  git -c core.safecrlf=false commit -m "chore: 同步本地改动" >nul
  echo     已提交
) else (
  echo     没有需要提交的改动
)
git log --oneline -1
echo.

rem ---------- 3) 鉴权自检 ----------
set GIT_SSH_COMMAND=ssh -o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new
echo [3/5] 测试 github 鉴权...
git ls-remote --heads github >nul 2>&1
if errorlevel 1 (
  echo     [跳过] github 鉴权失败 —— 本机公钥还没加到 GitHub
  set SKIP_GH=1
) else (
  echo     OK
)
echo [3/5] 测试 gitcode 鉴权...
git ls-remote --heads gitcode >nul 2>&1
if errorlevel 1 (
  echo     [跳过] gitcode 鉴权失败 —— 本机公钥还没加到 GitCode
  set SKIP_GC=1
) else (
  echo     OK
)
echo.

rem ---------- 4) 推送 ----------
echo [4/5] 推送...
if defined SKIP_GH (
  echo     github: 已跳过
) else (
  git push -u github main
  if errorlevel 1 echo     [失败] github 推送失败
)
if defined SKIP_GC (
  echo     gitcode: 已跳过
) else (
  git push -u gitcode main
  if errorlevel 1 echo     [失败] gitcode 推送失败
)
echo.

rem ---------- 5) 结果 ----------
echo [5/5] 当前状态:
git log --oneline -1
for /f "tokens=*" %%i in ('git remote -v ^| findstr push') do echo     %%i
echo.
if defined SKIP_GH if defined SKIP_GC (
  echo ============================================================
  echo  两个远端都鉴权失败。修复办法：
  echo   1. 复制本机公钥：
  echo      type "%USERPROFILE%\.ssh\id_ed25519.pub"
  echo   2. 粘贴到 GitHub:  Settings ^-^> SSH and GPG keys ^-^> New SSH key
  echo                    https://github.com/settings/keys
  echo   3. 粘贴到 GitCode: 设置 ^-^> SSH 公钥
  echo                    https://gitcode.com/setting/ssh
  echo   4. 重新双击本脚本
  echo ============================================================
  pause
  exit /b 1
)
echo ============================================================
echo   完成。远端仓库：
echo     https://github.com/lixiaolong0424/MediaFaceSearchPlatform
echo     https://gitcode.com/LiXiaoLong_GitCode/MediaFaceSearchPlatform
echo ============================================================
pause
exit /b 0

:fail
echo 请先配置 git 身份：git config --global user.name "你的名字"
pause
exit /b 1
