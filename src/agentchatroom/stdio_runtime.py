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


def _fail_no_protocol_stdin() -> None:
    try:
        if sys.stderr is not None:
            sys.stderr.write(
                'agentchatroom mcp unavailable (startup_failed/no_protocol_stdin)\n'
            )
            sys.stderr.flush()
    except Exception:
        pass
    raise SystemExit(2) from None


class _ProtocolStdin:
    """Fail closed when the first MCP read is immediate EOF."""

    _acr_protocol_guard = True

    def __init__(self, inner):
        object.__setattr__(self, '_inner', inner)
        object.__setattr__(self, '_received', False)

    def read(self, size=-1):
        data = self._inner.read(size)
        self._note(data)
        return data

    def readline(self, *args, **kwargs):
        data = self._inner.readline(*args, **kwargs)
        self._note(data)
        return data

    def read1(self, size=-1):
        # TextIOWrapper in the MCP SDK reads binary stdin through read1.
        # Delegating it via __getattr__ bypassed the immediate-EOF guard.
        read = getattr(self._inner, 'read1', self._inner.read)
        data = read(size)
        self._note(data)
        return data

    def readinto(self, buffer):
        count = self._inner.readinto(buffer)
        self._note(count)
        return count

    def readinto1(self, buffer):
        read = getattr(self._inner, 'readinto1', self._inner.readinto)
        count = read(buffer)
        self._note(count)
        return count

    def readlines(self, hint=-1):
        data = self._inner.readlines(hint)
        self._note(b''.join(data) if data and isinstance(data[0], (bytes, bytearray)) else ''.join(data) if data else data)
        return data

    def _note(self, data) -> None:
        if data:
            object.__setattr__(self, '_received', True)

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def __setattr__(self, name, value):
        if name in {'_inner', '_received'}:
            object.__setattr__(self, name, value)
            return
        setattr(self._inner, name, value)


def install_protocol_stdin_guard() -> None:
    current = getattr(sys, 'stdin', None)
    if current is None:
        _fail_no_protocol_stdin()
    if getattr(current, '_acr_protocol_guard', False):
        return
    wrapper = _ProtocolStdin(current)
    buffer = getattr(current, 'buffer', None)
    if buffer is not None and not getattr(buffer, '_acr_protocol_guard', False):
        object.__setattr__(wrapper, 'buffer', _ProtocolStdin(buffer))
    sys.stdin = wrapper


def verify_protocol_stdin() -> None:
    """Report an empty completed transport from the main thread.

    Raising SystemExit from the SDK's stdin worker produces an exception group
    and runtime traceback. Let EOF close the transport normally, then fail here.
    """
    current = getattr(sys, 'stdin', None)
    buffer = getattr(current, 'buffer', None)
    if not (getattr(current, '_received', False) or getattr(buffer, '_received', False)):
        _fail_no_protocol_stdin()


def run_mcp_entry(arguments: list[str]) -> None:
    """No GUI error dialog may escape from a headless MCP entry point."""
    try:
        prepare_standard_streams(require_protocol=True)
        install_protocol_stdin_guard()
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
