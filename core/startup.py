"""开机自启：写入 HKCU 注册表 Run 项。"""
from __future__ import annotations

import os
import sys
import winreg

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "rclone_tray"
# 写入 Run 项的命令行标记，用于区分“开机自启”与“用户手动打开”
AUTOSTART_FLAG = "--autostart"


def _command(script_path: str) -> str:
    if getattr(sys, "frozen", False):
        # 打包后自身即可执行文件，无需再传脚本路径
        return f'"{os.path.abspath(sys.executable)}" {AUTOSTART_FLAG}'
    exe = sys.executable
    if exe.lower().endswith("python.exe"):
        candidate = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(candidate):
            exe = candidate
    return f'"{exe}" "{os.path.abspath(script_path)}" {AUTOSTART_FLAG}'


def is_autostart_launch(argv: list[str] | None = None) -> bool:
    """本次进程是否由开机自启（Run 项）拉起。"""
    args = sys.argv[1:] if argv is None else argv
    return AUTOSTART_FLAG in args


def enable(script_path: str) -> None:
    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE)
    try:
        winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, _command(script_path))
    finally:
        winreg.CloseKey(key)


def refresh(script_path: str) -> None:
    """已启用自启时，把旧版（缺少 --autostart 标记）的 Run 项升级为新命令。"""
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ)
        try:
            current, _ = winreg.QueryValueEx(key, VALUE_NAME)
        finally:
            winreg.CloseKey(key)
    except FileNotFoundError:
        return
    if AUTOSTART_FLAG not in str(current):
        enable(script_path)


def disable() -> None:
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE)
        try:
            winreg.DeleteValue(key, VALUE_NAME)
        finally:
            winreg.CloseKey(key)
    except FileNotFoundError:
        pass


def is_enabled() -> bool:
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ)
        try:
            winreg.QueryValueEx(key, VALUE_NAME)
        finally:
            winreg.CloseKey(key)
        return True
    except FileNotFoundError:
        return False
