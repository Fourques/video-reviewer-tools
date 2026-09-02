# -*- mode: python ; coding: utf-8 -*-

import sys
from PyInstaller.utils.hooks import collect_all


imageio_datas, imageio_binaries, imageio_hiddenimports = collect_all("imageio_ffmpeg")
app_datas = [
    ("launcher.html", "."),
    ("index.html", "."),
    ("quick_label.html", "."),
    ("THIRD_PARTY_NOTICES.md", "."),
    ("licenses", "licenses"),
]

a = Analysis(
    ["start.py"],
    pathex=[],
    binaries=imageio_binaries,
    datas=app_datas + imageio_datas,
    hiddenimports=imageio_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter"],
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
        version="1.2.0",
        info_plist={
            "CFBundleDisplayName": "视频人工审核工具",
            "NSHighResolutionCapable": True,
        },
    )
