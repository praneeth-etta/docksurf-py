# -*- mode: python ; coding: utf-8 -*-
import os

from PyInstaller.utils.hooks import collect_submodules

# Textual lazy-imports its widgets via a custom __getattr__, which
# PyInstaller's static analysis can't see — without this, the frozen binary
# crashes on startup with ModuleNotFoundError: textual.widgets._tab_pane (or
# similar).
hiddenimports = collect_submodules("textual.widgets")

repo_root = os.path.join(SPECPATH, "..")

a = Analysis(
    [os.path.join(SPECPATH, "entrypoint.py")],
    pathex=[repo_root],
    binaries=[],
    # Building from entrypoint.py (which imports docksurf_py as a real
    # package) instead of running docksurf_py/app.py directly as the frozen
    # script keeps docksurf_py/ intact inside the bundle, so app.tcss lands
    # where Textual's relative CSS_PATH lookup expects it.
    datas=[(os.path.join(repo_root, "docksurf_py", "app.tcss"), "docksurf_py")],
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
    name="docksurf",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
