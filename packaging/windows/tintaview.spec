# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build spec for the Windows build of TintaView.

Build with (from the repo root, inside a venv with the `ui` extra installed):

    pyinstaller packaging/windows/tintaview.spec

Produces a **onedir** bundle at ``dist/TintaView/`` — deliberately not ``--onefile``:
a onefile build re-extracts itself into a temp directory on *every* launch, which is
slow for a tray app that starts at login and makes "where is my log/config" confusing
for support. onedir starts instantly and its files sit next to `TintaView.exe`.

The build is **windowed** (no console) since this is a tray app; diagnostics go to the
rotating log file under `%LOCALAPPDATA%\\TintaView\\logs` (see `tintaview.core.log`),
not stdout.

``openrgb-python`` is optional at runtime (`tintaview.engines.openrgb` imports it lazily
inside try/except, see that module's docstring) — the app must still start without it.
PyInstaller's static analysis may or may not pick it up depending on whether it's
installed in the build environment; either way is fine, so it is neither forced into
`hiddenimports` nor excluded here.
"""

from __future__ import annotations

import pathlib

block_cipher = None

# SPECPATH is injected by PyInstaller into this file's execution namespace; it is the
# directory containing this .spec file: packaging/windows/.
REPO_ROOT = pathlib.Path(SPECPATH).resolve().parents[1]  # packaging/windows -> repo root
ASSETS_DIR = REPO_ROOT / "tintaview" / "assets" / "generated"
HOOKS_DIR = REPO_ROOT / "tintaview" / "hooks"
ICON_PATH = ASSETS_DIR / "tintaview.ico"

# PyInstaller's Analysis() needs a real script to trace from; the installed console
# script (`tintaview = tintaview.cli:main`) only exists as a generated shim inside
# whichever venv happens to be building, so a tiny equivalent bootstrap is written into
# build/ instead (already .gitignore'd, identical to every other PyInstaller scratch
# output) rather than checking in a duplicate entry-point file.
BUILD_DIR = REPO_ROOT / "build"
BUILD_DIR.mkdir(parents=True, exist_ok=True)
ENTRY_SCRIPT = BUILD_DIR / "_tintaview_entry.py"
ENTRY_SCRIPT.write_text(
    "import sys\n"
    "from tintaview.cli import main\n"
    "\n"
    "if __name__ == '__main__':\n"
    "    sys.exit(main())\n",
    encoding="utf-8",
)

# Data files the app reads at runtime, not just at build time:
#   assets/generated/*   -- icons the tray/wizard load by path (tintaview.ui.icons)
#   tintaview/hooks/*     -- tv-hook.sh / tv-hook.cmd, copied by the installer/`tintaview
#                            hooks install` to the stable path config.hook_bin_path()
# Mirroring the source-relative layout (assets/generated, tintaview/hooks) means code
# that resolves these paths relative to the package/repo root keeps working unmodified
# whether frozen or not.
datas = [
    (str(ASSETS_DIR / "*"), "tintaview/assets/generated"),
    (str(HOOKS_DIR / "*"), "tintaview/hooks"),
]

# PySide6 modules actually used (tintaview/ui/{tray,flyout,icons}.py) — listed explicitly
# even though PyInstaller's bundled PySide6 hook normally finds these on its own, as a
# defensive belt-and-suspenders against a hook regression silently shrinking the bundle.
hiddenimports = [
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    "tomllib",
]

# Heavy PySide6 submodules TintaView never imports (no QML, no web content, no 3D, no
# media/serial/sensor hardware). PyInstaller's PySide6 hook pulls a lot of this in by
# default; excluding it here is the difference between a bundle in the tens of MB and
# one pushing past 200+ MB for code paths this app never touches.
excludes = [
    "PySide6.QtQml",
    "PySide6.QtQuick",
    "PySide6.QtQuick3D",
    "PySide6.QtQuickWidgets",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick",
    "PySide6.QtWebChannel",
    "PySide6.QtWebSockets",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DRender",
    "PySide6.Qt3DAnimation",
    "PySide6.Qt3DExtras",
    "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtBluetooth",
    "PySide6.QtNfc",
    "PySide6.QtSensors",
    "PySide6.QtSerialPort",
    "PySide6.QtSerialBus",
    "PySide6.QtPositioning",
    "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtDesigner",
    "PySide6.QtHelp",
    "PySide6.QtTest",
    "PySide6.scripts",
]

a = Analysis(
    [str(ENTRY_SCRIPT)],
    pathex=[str(REPO_ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="TintaView",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX-packed antivirus false positives are common; not worth it unsigned
    console=False,  # windowed: this is a tray app, not a CLI tool
    icon=str(ICON_PATH),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="TintaView",
)
