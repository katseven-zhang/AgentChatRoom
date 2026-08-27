# -*- coding: utf-8 -*-
"""Regenerate the two portable Windows launcher files as GBK + CRLF."""

from __future__ import annotations

import argparse
from pathlib import Path

START = """@echo off
setlocal
cd /d "%~dp0"
title AgentChatRoom
call "%~dp0scripts\\bootstrap.cmd"
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
"""

STOP = """@echo off
setlocal
cd /d "%~dp0"
title AgentChatRoom 清理残留
call "%~dp0scripts\\bootstrap.cmd"
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
"""


def launcher_bytes(content: str) -> bytes:
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    return normalized.replace("\n", "\r\n").encode("gbk")


def write_launchers(root: Path) -> tuple[Path, Path]:
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    start_path = root / "启动 AgentChatRoom.cmd"
    stop_path = root / "关闭 AgentChatRoom.cmd"
    start_path.write_bytes(launcher_bytes(START))
    stop_path.write_bytes(launcher_bytes(STOP))
    return start_path, stop_path


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Repository root that receives the launcher files",
    )
    args = parser.parse_args(argv)
    start_path, stop_path = write_launchers(args.root)
    print(f"generated: {start_path!a}")
    print(f"generated: {stop_path!a}")


if __name__ == "__main__":
    main()
