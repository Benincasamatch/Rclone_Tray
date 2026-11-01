"""Windows 原生窗口外观：保留系统标题栏，仅同步颜色与圆角。

系统标题栏保留贴边分屏、最大化吸附布局、Alt+Space 菜单与高对比度主题；
把标题栏底色调成与窗口底色一致即可与玻璃内容区连续。
"""
from __future__ import annotations

import ctypes
import sys
import tkinter as tk
from typing import Optional

from . import theme as T

GWL_STYLE = -16
WS_CAPTION = 0x00C00000
WS_THICKFRAME = 0x00040000
WS_MAXIMIZEBOX = 0x00010000
WS_MINIMIZEBOX = 0x00020000
WS_SYSMENU = 0x00080000

DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWA_BORDER_COLOR = 34
DWMWA_CAPTION_COLOR = 35
DWMWA_TEXT_COLOR = 36
DWMWCP_ROUND = 2


def _colorref(hex_color: str) -> int:
    """#RRGGBB → Windows COLORREF（0x00BBGGRR）。"""
    v = hex_color.lstrip("#")
    r, g, b = int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16)
    return r | (g << 8) | (b << 16)


def _set_attr(hwnd: int, attr: int, value: int) -> None:
    try:
        v = ctypes.c_int(value)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(v), ctypes.sizeof(v))
    except Exception:
        pass


def apply_native_chrome(win: tk.Misc, caption_color: Optional[str] = None) -> None:
    """保留系统标题栏，深色模式、圆角、标题栏/边框颜色随主题。尽力而为。"""
    if sys.platform != "win32":
        return
    win.update_idletasks()
    try:
        u32 = ctypes.windll.user32
        hwnd = u32.GetParent(win.winfo_id())
        style = u32.GetWindowLongW(hwnd, GWL_STYLE)
        style |= WS_CAPTION | WS_THICKFRAME | WS_SYSMENU | WS_MINIMIZEBOX | WS_MAXIMIZEBOX
        u32.SetWindowLongW(hwnd, GWL_STYLE, style)
        # FRAMECHANGED | NOSIZE | NOMOVE | NOZORDER | NOACTIVATE
        u32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0020 | 0x0001 | 0x0002 | 0x0004 | 0x0010)
        _set_attr(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, 1 if T.is_dark() else 0)
        _set_attr(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_ROUND)
        _set_attr(hwnd, DWMWA_CAPTION_COLOR, _colorref(caption_color or T.BASE))
        _set_attr(hwnd, DWMWA_TEXT_COLOR, _colorref(T.TEXT))
        _set_attr(hwnd, DWMWA_BORDER_COLOR, _colorref(T.DIVIDER))
    except Exception:
        pass


def center_on_work_area(win: tk.Misc, width: int, height: int) -> None:
    """按工作区（排除任务栏）居中，并夹在可视范围内。"""
    left, top, area_w, area_h = T.work_area()
    x = left + max(0, (area_w - width) // 2)
    y = top + max(0, (area_h - height) // 2)
    win.geometry(f"{width}x{height}+{x}+{y}")


def center_on_owner(win: tk.Misc, owner: tk.Misc, width: int, height: int) -> None:
    """相对父窗口居中，并夹在工作区内。"""
    left, top, area_w, area_h = T.work_area()
    x = max(left, min(owner.winfo_rootx() + (owner.winfo_width() - width) // 2, left + area_w - width))
    y = max(top, min(owner.winfo_rooty() + (owner.winfo_height() - height) // 2, top + area_h - height))
    win.geometry(f"{width}x{height}+{x}+{y}")


def flash_taskbar(win: tk.Misc) -> None:
    """把窗口提到前台（托盘唤起窗口时使用，符合系统焦点规则）。"""
    try:
        win.deiconify()
        win.lift()
        win.focus_force()
    except Exception:
        pass
