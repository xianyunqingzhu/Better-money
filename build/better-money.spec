# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: windowless BetterMoney.exe (onedir) for Windows 10/11 x64."""
from pathlib import Path

REPO_ROOT = Path(SPECPATH).parent  # build/ -> repository root

a = Analysis(
    [str(REPO_ROOT / "windows_entry.py")],
    pathex=[str(REPO_ROOT)],
    binaries=[],
    datas=[
        (str(REPO_ROOT / "static"), "static"),
        (str(REPO_ROOT / "icon.ico"), "."),
    ],
    hiddenimports=[
        "uvicorn.logging",
        "uvicorn.loops.auto",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan.on",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tests", "pytest", "tkinter", "unittest"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="BetterMoney",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=str(REPO_ROOT / "icon.ico"),
    version=str(Path(SPECPATH) / "version_info.txt"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="BetterMoney",
)
