"""rclone 可执行文件定位与版本解析。"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

# 常见安装目录（%ProgramFiles% 等会在运行时展开）
SEARCH_DIRS = [
    r"%ProgramFiles%\rclone",
    r"%ProgramFiles(x86)%\rclone",
    r"%LocalAppData%\rclone",
    r"%LocalAppData%\Programs\rclone",
    r"%USERPROFILE%\rclone",
    r"C:\rclone",
    r"C:\tools\rclone",
    r"%USERPROFILE%\scoop\apps\rclone\current",
    r"%LOCALAPPDATA%\Microsoft\WinGet\Links",
]


def expand(p: str) -> str:
    return os.path.expandvars(os.path.expanduser(p))


def _script_dir() -> str:
    """主脚本/可执行文件所在目录（支持源码运行与 PyInstaller 打包）。"""
    try:
        return os.path.dirname(os.path.abspath(sys.argv[0]))
    except Exception:
        return os.getcwd()


def _bundle_dir() -> str:
    """PyInstaller 解包资源目录；源码运行时即脚本目录。"""
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", _script_dir())
    return _script_dir()


def _candidates() -> list[str]:
    cands: list[str] = []
    # 项目自带：打包解包目录 或 源码目录 中的 rclone\rclone.exe
    cands.append(os.path.join(_bundle_dir(), "rclone", "rclone.exe"))
    if getattr(sys, "frozen", False):
        # 打包后也允许用 exe 旁的 rclone 目录覆盖
        cands.append(os.path.join(_script_dir(), "rclone", "rclone.exe"))
    # PATH
    w = shutil.which("rclone")
    if w:
        cands.append(w)
    for d in SEARCH_DIRS:
        cands.append(os.path.join(expand(d), "rclone.exe"))
    return cands


def find_rclone(preferred: str = "") -> Optional[str]:
    """按优先级查找 rclone.exe；preferred 为用户配置/手动指定的路径。"""
    if preferred and preferred.strip():
        p = Path(expand(preferred.strip()))
        if p.is_file():
            return str(p)
    for c in _candidates():
        p = Path(c)
        if p.is_file():
            return str(p)
    return None


def parse_version(text: str) -> Optional[str]:
    m = re.search(r"rclone v([\d.]+)", text)
    return m.group(1) if m else None


def version_tuple(version: Optional[str]) -> tuple:
    try:
        return tuple(int(x) for x in version.split(".")[:2]) if version else (0, 0)
    except Exception:
        return (0, 0)


def get_version(exe: str) -> Optional[str]:
    """运行 `rclone version` 并解析版本号。"""
    try:
        r = subprocess.run(
            [exe, "version"],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return parse_version(r.stdout + r.stderr)
    except Exception:
        return None


def no_window_flag() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)
