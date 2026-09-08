"""One bounded unavailable-data startup probe; never points at production data."""
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

exe=Path(sys.argv[1]).resolve()
with tempfile.TemporaryDirectory(prefix='acr-popup-probe-') as folder:
    blocked=Path(folder)/'not-a-directory'
    blocked.write_text('isolated startup failure',encoding='utf-8')
    env=os.environ.copy()
    env.pop('AGENTCHATROOM_CONFIG',None)
    env['AGENTCHATROOM_DATA_DIR']=str(blocked)
    env['AGENTCHATROOM_SOFTWARE_KEY']='offline-probe'
    env['AGENTCHATROOM_SOFTWARE_NAME']='Offline probe'
    env['AGENTCHATROOM_SOFTWARE_CLIENT']='test'
    process=subprocess.Popen([str(exe),'mcp'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env,creationflags=subprocess.CREATE_NO_WINDOW)
    visible=[]
    user=ctypes.WinDLL('user32')
    callback_type=ctypes.WINFUNCTYPE(wintypes.BOOL,wintypes.HWND,wintypes.LPARAM)
    user.EnumWindows.argtypes=[callback_type,wintypes.LPARAM]
    user.IsWindowVisible.argtypes=[wintypes.HWND]
    user.GetWindowThreadProcessId.argtypes=[wintypes.HWND,ctypes.POINTER(wintypes.DWORD)]
    @callback_type
    def cb(hwnd,param):
        pid=wintypes.DWORD()
        user.GetWindowThreadProcessId(hwnd,ctypes.byref(pid))
        if pid.value==process.pid and user.IsWindowVisible(hwnd):
            visible.append(int(hwnd))
        return True
    expired=False
    try:
        until=time.monotonic()+8
        while process.poll() is None and time.monotonic()<until:
            user.EnumWindows(cb,0)
            time.sleep(.02)
        if process.poll() is None:
            expired=True
            process.kill()
        stdout,stderr=process.communicate(timeout=3)
    finally:
        if process.poll() is None:
            process.kill(); process.wait(timeout=3)
    print(json.dumps({'exe':str(exe),'returncode':process.returncode,'timed_out':expired,'visible_windows':len(set(visible)),'stdout_bytes':len(stdout),'stderr':stderr.decode('utf-8',errors='replace')[:1500]}))
