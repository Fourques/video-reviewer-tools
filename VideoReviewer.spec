# -*- mode: python ; coding: utf-8 -*-

import sys
import os
from pathlib import Path
from importlib.metadata import distribution
from PyInstaller.utils.hooks import collect_all


imageio_datas, imageio_binaries, imageio_hiddenimports = collect_all("imageio_ffmpeg")
if sys.platform.startswith('linux'):
    os.environ['QT_API'] = 'pyside6'
webview_datas, webview_binaries, webview_hiddenimports = collect_all("webview")
license_datas = []
for package in ('pywebview', 'PySide6', 'PySide6_Essentials', 'PySide6_Addons', 'shiboken6', 'QtPy', 'pythonnet', 'proxy_tools', 'bottle', 'typing_extensions'):
    try:
        dist = distribution(package)
        for file in dist.files or ():
            if 'license' in str(file).lower() and not str(file).endswith('.py'):
                actual = dist.locate_file(file)
                if actual.is_file():
                    license_datas.append((str(actual), 'licenses/' + package))
    except Exception:
        pass  # Platform-specific packages are not all present on each runner.
app_datas = [
    ("launcher.html", "."),
    ("project_center.html", "."),
    ("index.html", "."),
    ("quick_label.html", "."),
    ("app.js", "."),
    ("playback.js", "."),
    ("layout.css", "."),
    ("workbench.html", "."),
    ("workbench.js", "."),
    ("workbench.css", "."),
    ("media.js", "."),
    ("THIRD_PARTY_NOTICES.md", "."),
    ("licenses", "licenses"),
]

a = Analysis(
    ["start.py"],
    pathex=[],
    binaries=imageio_binaries + webview_binaries,
    datas=app_datas + imageio_datas + webview_datas + license_datas,
    hiddenimports=imageio_hiddenimports + webview_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "PyQt5", "PyQt6", "PySide2", "qtpy.QtDataVisualization", "PySide6.QtDataVisualization", "qtpy.QtCharts", "PySide6.QtCharts", "PySide6.QtGraphs"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="VideoReviewer",
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
)

bundle = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="VideoReviewer",
)

if sys.platform == "darwin":
    app = BUNDLE(
        bundle,
        name="VideoReviewer.app",
        icon=None,
        bundle_identifier="com.fourques.video-reviewer",
        version="2.0.4",
        info_plist={
            "CFBundleDisplayName": "视频人工审核工具",
            "NSHighResolutionCapable": True,
        },
    )
