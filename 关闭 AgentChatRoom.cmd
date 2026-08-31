@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title AgentChatRoom 清理残留
set "AGENTCHATROOM_PYTHON=%~dp0.venv\Scripts\python.exe"
if not defined AGENTCHATROOM_DATA_DIR set "AGENTCHATROOM_DATA_DIR=%~dp0.agentchatroom\runtime"
if not defined AGENTCHATROOM_PORT set "AGENTCHATROOM_PORT=8765"

if exist "%AGENTCHATROOM_PYTHON%" (
  for /f "delims=" %%p in ('"%AGENTCHATROOM_PYTHON%" -c "from agentchatroom.config import load_settings; print(load_settings().port)" 2^>nul') do set "AGENTCHATROOM_PORT=%%p"
  echo [1/3] 请求后台服务正常停止...
  "%AGENTCHATROOM_PYTHON%" -m agentchatroom stop >nul 2>&1
) else (
  echo [1/3] Python 环境不可用，跳过正常停止。
)

echo [2/3] 清理 PID 文件记录的进程树...
set "AGENTCHATROOM_PID="
if exist "%AGENTCHATROOM_DATA_DIR%\server.pid" (
  set /p "AGENTCHATROOM_PID="<"%AGENTCHATROOM_DATA_DIR%\server.pid"
)
if defined AGENTCHATROOM_PID (
  taskkill /T /F /PID !AGENTCHATROOM_PID! >nul 2>&1
)
del /Q "%AGENTCHATROOM_DATA_DIR%\server.pid" >nul 2>&1

echo [3/3] 清理仍占用 %AGENTCHATROOM_PORT% 端口的残留进程树...
set "KILLED="
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":%AGENTCHATROOM_PORT%" ^| findstr "LISTENING"') do (
  taskkill /T /F /PID %%p >nul 2>&1 && set "KILLED=1"
)
if defined KILLED (
  echo 已结束占用端口的残留进程树。
) else (
  echo 没有发现占用 %AGENTCHATROOM_PORT% 端口的进程，服务已是停止状态。
)
echo.
echo 清理完成。
pause
exit /b 0
