@echo off
setlocal
cd /d "%~dp0"
title AgentChatRoom 清理残留
call "%~dp0scripts\bootstrap.cmd"
if errorlevel 1 goto :failed

echo [1/2] 停止后台模式残留的服务...
"%AGENTCHATROOM_PYTHON%" -m agentchatroom stop >nul 2>&1

for /f "delims=" %%p in ('"%AGENTCHATROOM_PYTHON%" -c "from agentchatroom.config import load_settings; print(load_settings().port)"') do set "AGENTCHATROOM_PORT=%%p"
if not defined AGENTCHATROOM_PORT goto :failed

echo [2/2] 清理仍占用 %AGENTCHATROOM_PORT% 端口的残留进程...
set "KILLED="
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":%AGENTCHATROOM_PORT%" ^| findstr "LISTENING"') do (
  taskkill /F /PID %%p >nul 2>&1 && set "KILLED=1"
)
if defined KILLED (
  echo 已结束占用端口的残留进程。
) else (
  echo 没有发现占用 8765 端口的进程，服务已是停止状态。
)
echo.
echo 清理完成。
pause
exit /b 0

:failed
echo.
echo 清理脚本执行失败，请检查 Python 环境。
pause
exit /b 1
