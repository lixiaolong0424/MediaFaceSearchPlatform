@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"
title 推送到远端
echo ============================================================
echo   素材人脸检索系统 · 一键推送（github + gitcode）
echo ============================================================
echo.

rem ---------- 0) 身份 ----------
git config user.name >nul 2>&1 || (echo [错误] 未配置 git user.name & goto :fail)
echo [0/5] 提交身份:
for /f "tokens=*" %%i in ('git config user.name') do echo     %%i
for /f "tokens=*" %%i in ('git config user.email') do echo     %%i
echo.

rem ---------- 1) github 走 443（22 端口在本机被中间设备劫持） ----------
echo [1/5] 检查 SSH 通道...
findstr /i "ssh.github.com" "%USERPROFILE%\.ssh\config" >nul 2>&1
if errorlevel 1 (
  echo     未配置 github 走 443，正在写入 %USERPROFILE%\.ssh\config ...
  >>"%USERPROFILE%\.ssh\config" echo Host github.com
  >>"%USERPROFILE%\.ssh\config" echo     HostName ssh.github.com
  >>"%USERPROFILE%\.ssh\config" echo     Port 443
  >>"%USERPROFILE%\.ssh\config" echo     User git
  >>"%USERPROFILE%\.ssh\config" echo     IdentityFile ~/.ssh/id_ed25519
  >>"%USERPROFILE%\.ssh\config" echo     IdentitiesOnly yes
) else (
  echo     github 已配置 443 通道
)
echo.

rem ---------- 2) 提交 ----------
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
echo [3/5] 测试鉴权...
git ls-remote --heads github >nul 2>&1
if errorlevel 1 (echo     github : 失败 & set SKIP_GH=1) else (echo     github : OK)
git ls-remote --heads gitcode >nul 2>&1
if errorlevel 1 (echo     gitcode: 失败 & set SKIP_GC=1) else (echo     gitcode: OK)
echo.

rem ---------- 4) 推送 ----------
echo [4/5] 推送...
if defined SKIP_GH (echo     github : 跳过) else (
  git push -u github main
  if errorlevel 1 echo     [失败] github 推送失败
)
if defined SKIP_GC (echo     gitcode: 跳过) else (
  git push -u gitcode main
  if errorlevel 1 echo     [失败] gitcode 推送失败
)
echo.

rem ---------- 5) 结果 ----------
echo [5/5] 结果:
echo     本地  :
for /f "tokens=*" %%i in ('git rev-parse --short HEAD') do echo       %%i
echo     github:
git ls-remote --heads github 2>nul | findstr main
echo     gitcode:
git ls-remote --heads gitcode 2>nul | findstr main
echo.
if defined SKIP_GH if defined SKIP_GC (
  echo ============================================================
  echo  两端都失败。常见原因：
  echo   1. 公钥没登记：type "%USERPROFILE%\.ssh\id_ed25519.pub" 后粘贴到
  echo      https://github.com/settings/keys
  echo      https://gitcode.com/setting/ssh
  echo   2. 公司/运营商劫持 22 端口：本脚本已让 github 走 443，gitcode 需换
  echo      ssh -T -p 443 git@gitcode.com  或改用 HTTPS
  echo ============================================================
  pause
  exit /b 1
)
echo ============================================================
echo   完成
echo     https://github.com/lixiaolong0424/MediaFaceSearchPlatform
echo     https://gitcode.com/LiXiaoLong_GitCode/MediaFaceSearchPlatform
echo ============================================================
pause
exit /b 0

:fail
echo 请先配置 git 身份：git config --global user.name "名字" / user.email "邮箱"
pause
exit /b 1
