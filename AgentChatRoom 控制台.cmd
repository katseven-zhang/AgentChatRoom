@echo off
setlocal
cd /d "%~dp0"
title AgentChatRoom 控制台
call "%~dp0scripts\bootstrap.cmd"
if errorlevel 1 goto :failed

rem 与 GUI 内部一致：默认关闭请求日志刷屏，GUI 日志区只显示启动、错误与停止信息
set "AGENTCHATROOM_ACCESS_LOG=0"

"%AGENTCHATROOM_PYTHON%" -m agentchatroom gui
if errorlevel 1 goto :failed
exit /b 0

:failed
echo.
echo AgentChatRoom 控制台启动失败，请查看上方错误信息。
pause
exit /b 1
