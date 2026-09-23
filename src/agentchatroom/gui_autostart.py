"""Windows current-user login startup for the packaged GUI.

The registry value is deliberately separate from the application's service
configuration: disabling login startup must never stop a running service.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "AgentChatRoom"


def startup_command(*, executable: Path | None = None, frozen: bool | None = None) -> str:
    """Return a Windows-quoted command that launches the GUI without a CMD window."""
    target = Path(executable or sys.executable).resolve()
    packaged = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    if packaged:
        args = [str(target), "gui", "--autostart"]
    else:
        pythonw = target.with_name("pythonw.exe")
        if not pythonw.is_file():
            raise OSError("开发环境缺少 pythonw.exe，无法无命令行窗口地开机启动")
        args = [str(pythonw), "-m", "agentchatroom", "gui", "--autostart"]
    return subprocess.list2cmdline(args)


def is_temporary_executable(executable: Path | None = None) -> bool:
    """Reject ephemeral OS temp and ignored project runtime/build areas."""
    target = Path(executable or sys.executable).resolve()
    temporary = Path(tempfile.gettempdir()).resolve()
    return (target == temporary or temporary in target.parents
            or ".agentchatroom" in {part.casefold() for part in target.parts})


class LoginStartup:
    def __init__(self, *, executable: Path | None = None, frozen: bool | None = None):
        self.executable = Path(executable or sys.executable).resolve()
        self.frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen

    def read_command(self) -> str | None:
        if os.name != "nt":
            return None
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
                value, kind = winreg.QueryValueEx(key, VALUE_NAME)
        except FileNotFoundError:
            return None
        text_types = (winreg.REG_SZ, getattr(winreg, "REG_EXPAND_SZ", -1))
        return str(value) if kind in text_types and isinstance(value, str) else None

    def expected_command(self) -> str:
        return startup_command(executable=self.executable, frozen=self.frozen)

    def enabled(self) -> bool:
        return self.read_command() is not None

    def is_current(self) -> bool:
        return self.read_command() == self.expected_command()

    def enable(self) -> None:
        if os.name != "nt":
            raise OSError("开机启动仅支持 Windows")
        if is_temporary_executable(self.executable):
            raise OSError("当前程序位于临时目录；请将完整发布目录移到固定位置后再启用开机启动")
        command = self.expected_command()
        import winreg

        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE
        ) as key:
            try:
                previous = winreg.QueryValueEx(key, VALUE_NAME)
            except FileNotFoundError:
                previous = None
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, command)
            try:
                verified = self.read_command() == command
            except OSError:
                verified = False
            if not verified:
                # Restore only if our just-written value is still present;
                # never overwrite a concurrent user change to the Run entry.
                try:
                    current = winreg.QueryValueEx(key, VALUE_NAME)
                except FileNotFoundError:
                    current = None
                if current == (command, winreg.REG_SZ):
                    if previous is None:
                        winreg.DeleteValue(key, VALUE_NAME)
                    else:
                        winreg.SetValueEx(key, VALUE_NAME, 0, previous[1], previous[0])
                raise OSError("开机启动项写入后校验失败；已尝试恢复原设置")

    def disable(self) -> None:
        if os.name != "nt":
            raise OSError("开机启动仅支持 Windows")
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, VALUE_NAME)
        except FileNotFoundError:
            return
        if self.read_command() is not None:
            raise OSError("开机启动项移除后校验失败")


class GuiInstanceGuard:
    """Prevent a login launch from opening a second controller window."""

    def __init__(self) -> None:
        self._handle = None

    def acquire(self) -> bool:
        if os.name != "nt":
            return True
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        ctypes.set_last_error(0)
        handle = kernel32.CreateMutexW(None, False, "Local\\AgentChatRoom_GUI_SingleInstance")
        if not handle:
            raise OSError(ctypes.get_last_error(), "无法创建 GUI 单实例锁")
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            kernel32.CloseHandle(handle)
            return False
        self._handle = (kernel32, handle)
        return True

    def release(self) -> None:
        if self._handle is not None:
            kernel32, handle = self._handle
            self._handle = None
            kernel32.CloseHandle(handle)
