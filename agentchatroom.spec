# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

# Hidden imports required across CLI, GUI, MCP, FastAPI, Uvicorn, and Pywebview
hidden_imports = [
    "agentchatroom",
    "agentchatroom.api",
    "agentchatroom.cli",
    "agentchatroom.client_config",
    "agentchatroom.config",
    "agentchatroom.contracts",
    "agentchatroom.database",
    "agentchatroom.desktop",
    "agentchatroom.errors",
    "agentchatroom.gui",
    "agentchatroom.integrations",
    "agentchatroom.local_mcp",
    "agentchatroom.mcp_server",
    "agentchatroom.mcp_bridge",
    "agentchatroom.stdio_runtime",
    "agentchatroom.models",
    "agentchatroom.project_registration",
    "agentchatroom.services",
    "agentchatroom.shell",
    "agentchatroom.task_history",
    "uvicorn",
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespans",
    "uvicorn.lifespans.on",
    "starlette",
    "fastapi",
    "mcp",
    "webview",
    "clr_loader",
    "pythonnet",
    "pystray",
    "pystray._win32",
    "PIL",
    "PIL.Image",
    "PIL.ImageDraw",
]

# Web static assets (HTML, CSS, JS) must be included for backend to serve
datas = [
    ('src/agentchatroom/web', 'agentchatroom/web'),
]

# Single windowed executable: packaging/entry_app.py dispatches on the first
# argument — default/gui opens the pywebview shell panel, `mcp` runs the
# stdio MCP server, and any other subcommand reaches the console CLI (the
# detached serve subprocess reuses the very same exe via cli.py).
a_app = Analysis(
    ['packaging/entry_app.py'],
    pathex=['src'],
    binaries=[],
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['PySide6', 'PyQt5', 'PyQt6', 'tkinter'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz_app = PYZ(a_app.pure, a_app.zipped_data, cipher=block_cipher)

exe_app = EXE(
    pyz_app,
    a_app.scripts,
    [],
    exclude_binaries=True,
    name='agentchatroom',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    # Windowed: double-click opens the GUI panel without a console window.
    # stdio_runtime explicitly restores inherited pipe/file handles; never
    # assume that a windowed bootloader provides Python standard streams.
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe_app,
    a_app.binaries,
    a_app.zipfiles,
    a_app.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='agentchatroom',
)
