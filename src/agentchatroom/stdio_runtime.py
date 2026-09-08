"""Recover inherited pipes for Windows windowed executables without a console."""
from __future__ import annotations

import io
import os
import sys


def _inherited_stream(identifier: int, *, reading: bool):
    if sys.platform != 'win32':
        return None
    import ctypes
    from ctypes import wintypes
    import msvcrt

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetStdHandle.argtypes = [wintypes.DWORD]
    kernel.GetStdHandle.restype = wintypes.HANDLE
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.DuplicateHandle.argtypes = [wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE,
                                      ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD,
                                      wintypes.BOOL, wintypes.DWORD]
    kernel.DuplicateHandle.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.GetStdHandle(identifier)
    if handle in (None, 0, ctypes.c_void_p(-1).value):
        return None
    duplicate = wintypes.HANDLE()
    process = kernel.GetCurrentProcess()
    if not kernel.DuplicateHandle(process, handle, process, ctypes.byref(duplicate), 0, False, 2):
        return None
    try:
        descriptor = msvcrt.open_osfhandle(duplicate.value, os.O_BINARY | (os.O_RDONLY if reading else os.O_WRONLY))
    except OSError:
        kernel.CloseHandle(duplicate)
        return None
    try:
        binary = os.fdopen(descriptor, 'rb' if reading else 'wb', buffering=0)
    except Exception:
        os.close(descriptor)
        raise
    return io.TextIOWrapper(binary, encoding='utf-8', errors='replace',
                            line_buffering=not reading, write_through=not reading)


def prepare_standard_streams(*, require_protocol: bool = False) -> None:
    missing_protocol = False
    # stderr is prepared first so even a missing protocol pipe has a safe sink.
    for name, identifier, reading in (('stderr', -12, False), ('stdin', -10, True), ('stdout', -11, False)):
        if getattr(sys, name, None) is not None:
            continue
        stream = _inherited_stream(identifier, reading=reading)
        if stream is None:
            if name in {'stdin', 'stdout'}:
                missing_protocol = True
            stream = open(os.devnull, 'r' if reading else 'w', encoding='utf-8')
        setattr(sys, name, stream)
    if require_protocol and missing_protocol:
        raise RuntimeError('mcp_stdio_unavailable')


def run_mcp_entry(arguments: list[str]) -> None:
    """No GUI error dialog may escape from a headless MCP entry point."""
    try:
        prepare_standard_streams(require_protocol=True)
        from .mcp_server import main
        main(arguments)
    except SystemExit:
        raise
    except Exception:
        try:
            if sys.stderr is not None:
                sys.stderr.write('agentchatroom mcp unavailable (startup_failed)\n')
                sys.stderr.flush()
        except Exception:
            pass
        raise SystemExit(2) from None
