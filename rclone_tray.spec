# -*- mode: python ; coding: utf-8 -*-
# rclone_tray 打包配置：单文件、无控制台窗口

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=[
        ("rclone", "rclone"),          # 内置 rclone.exe
        ("docs", "docs"),              # 计划文档
        ("assets/icon.ico", "assets"), # 图标资源
    ],
    hiddenimports=["pystray._win32"],
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
    a.binaries,
    a.datas,
    [],
    name="rclone_tray",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=["assets/icon.ico"],
)
