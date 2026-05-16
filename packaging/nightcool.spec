# PyInstaller spec for NightCool.
#
# Builds a single-folder distribution that bundles Python, the daemon, the
# FastAPI server, and the PWA static assets. Run from the repo root on the
# target platform (Windows produces `dist/nightcool/nightcool.exe`; macOS
# produces an .app bundle if you flip `console=False` + add a BUNDLE; Linux
# produces a regular ELF):
#
#   pip install pyinstaller
#   pyinstaller packaging/nightcool.spec
#
# The resulting binary runs `nightcool serve --open-browser` by default — it
# opens the PWA in the user's browser, listens on 127.0.0.1:8765, and runs
# the polling loop in the background. Drop a shortcut in the Startup folder
# for "set and forget" behavior.

import sys
from pathlib import Path

repo = Path.cwd()
static_dir = repo / "src" / "nightcool" / "web" / "static"

block_cipher = None

a = Analysis(
    [str(repo / "packaging" / "launcher.py")],
    pathex=[str(repo / "src")],
    binaries=[],
    datas=[
        (str(static_dir), "nightcool/web/static"),
    ],
    hiddenimports=[
        "uvicorn.logging",
        "uvicorn.loops",
        "uvicorn.loops.auto",
        "uvicorn.protocols",
        "uvicorn.protocols.http",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan",
        "uvicorn.lifespan.on",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="nightcool",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,  # Keep the console on Windows so users can see logs.
    icon=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="nightcool",
)
