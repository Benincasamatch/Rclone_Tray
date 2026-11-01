"""打包资源定位：源码运行与 PyInstaller 解包目录统一入口。"""
from __future__ import annotations

import os
import sys


def base_dir() -> str:
    """资源根目录：打包时为 sys._MEIPASS，源码运行时为项目根目录。"""
    base = getattr(sys, "_MEIPASS", None)
    if base is None:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return base


def resource_path(rel: str) -> str:
    return os.path.join(base_dir(), rel)


def icon_path() -> str:
    """程序图标 assets/icon.ico 的绝对路径；不存在时返回空串。"""
    p = resource_path(os.path.join("assets", "icon.ico"))
    return p if os.path.exists(p) else ""
