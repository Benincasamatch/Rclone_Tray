"""开机自启：写入 HKCU 注册表 Run 项。"""
from __future__ import annotations

import os
import sys
import winreg

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "rclone_tray"


def _command(script_path: str) -> str:
    exe = sys.executable
    if exe.lower().endswith("python.exe"):
        candidate = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(candidate):
            exe = candidate
    return f'"{exe}" "{os.path.abspath(script_path)}"'


def enable(script_path: str) -> None:
    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE)
    try:
        winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, _command(script_path))
    finally:
        winreg.CloseKey(key)


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
