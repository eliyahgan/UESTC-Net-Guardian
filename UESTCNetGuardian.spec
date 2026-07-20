# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
import sys

project = Path(SPECPATH)

# This virtual environment is based on Miniconda.  The Python extension
# modules used by Pillow/requests/pystray depend on these runtime DLLs; a
# machine running the built EXE will not have the developer's conda PATH.
conda_bin = Path(sys.base_prefix) / "Library" / "bin"
runtime_dlls = []
for dll_name in (
    "ffi.dll",
    "libcrypto-3-x64.dll",
    "libssl-3-x64.dll",
    "libmpdec-4.dll",
    "liblzma.dll",
    "libbz2.dll",
    "libexpat.dll",
    "zlib.dll",
    "sqlite3.dll",
):
    dll_path = conda_bin / dll_name
    if dll_path.exists():
        runtime_dlls.append((str(dll_path), "."))

a = Analysis(
    [str(project / "guardian_app.py")],
    pathex=[str(project)],
    binaries=runtime_dlls,
    datas=[(str(project / "assets" / "guardian.png"), "assets")],
    hiddenimports=[
        "pystray._win32",
        "winrt.runtime",
        "winrt.windows.foundation",
        "winrt.windows.foundation.collections",
        "winrt.windows.networking.connectivity",
        "winrt.windows.networking.networkoperators",
        "winrt.windows.storage",
        "winrt.windows.devices",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="UESTCNetGuardian",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(project / "assets" / "guardian.ico"),
    version=str(project / "guardian_version.txt"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="UESTCNetGuardian",
)
