# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules

datas = []
binaries = []
hiddenimports = (
    collect_submodules("pycaw")
    + collect_submodules("comtypes", filter=lambda name: ".test" not in name)
    + ["_cffi_backend", "cffi", "psutil"]
)

a = Analysis(
    ["pc/gui.py"],
    pathex=["pc"],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="PhoneAudioBridge",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
)
