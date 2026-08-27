@echo off
setlocal
cd /d "%~dp0"
title AgentChatRoom
call "%~dp0scripts\bootstrap.cmd"
if errorlevel 1 goto :failed

rem 关闭浏览器轮询产生的访问日志刷屏（只保留启动/错误/停止信息）；要看完整请求日志改为 1
set "AGENTCHATROOM_ACCESS_LOG=0"

echo ==================================================
echo   AgentChatRoom 启动中...
echo   日志实时显示在本窗口，直接关闭本窗口即可停止后端
echo ==================================================
echo.
"%AGENTCHATROOM_PYTHON%" -m agentchatroom serve --open-browser
echo.
echo ==================================================
echo   AgentChatRoom 已停止。
echo   如果是意外退出，请查看上方的错误信息。
echo ==================================================
pause
exit /b 0

:failed
echo.
echo AgentChatRoom 启动失败，请查看上方错误信息。
pause
exit /b 1
