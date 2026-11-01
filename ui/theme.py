"""Tern · Stencil design tokens: dark translucent surfaces, purple accent, 4px grid.

Colors, radii, spacing, type scale and motion durations follow the design
reference (Dark Translucent · Minimal Desktop Design v1.0). Glass surfaces are
RGBA tokens composited by ``ui.glass`` over an ambient gradient; the palette
exposes their opaque equivalents (tint over BASE) so text widgets that cannot be
transparent still match the surface they sit on. Dimensions are logical pixels,
converted by ``Theme.px``.
"""
from __future__ import annotations

import ctypes
import sys
import tkinter as tk
import weakref
from tkinter import font as tkfont

# ---------- 间距：4px 基础体系 ----------
SPACE_XS = 4
SPACE_SM = 8
SPACE_MD = 12
SPACE_LG = 16
SPACE_XL = 24
SPACE_XXL = 32

# ---------- 圆角 ----------
RADIUS_CONTROL = 8
RADIUS_CARD = 12
RADIUS_FLYOUT = 10

# ---------- 控件尺寸 ----------
CONTROL_H_SM = 28
CONTROL_H = 32
CONTROL_H_LG = 40
NAV_WIDTH = 216
NAV_COLLAPSED = 64

# ---------- 字号 ----------
FONT_PAGE = 22
FONT_MODULE = 16
FONT_BODY = 13
FONT_CAPTION = 12
FONT_LABEL = 11

# ---------- 动效时长（ms） ----------
MOTION_HOVER = 120
MOTION_MENU = 180

# 字体：中文优先微软雅黑，西文随字体自带设计；图标用 Segoe Fluent Icons
_TEXT_CANDIDATES = ("微软雅黑", "Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI")
_ICON_CANDIDATES = ("Segoe Fluent Icons", "Segoe MDL2 Assets", "Segoe UI Symbol")
# 这些家族没有独立 Semibold，取 semibold 时退回 bold 字重
_NO_SEMIBOLD_FAMILY = ("微软雅黑", "Microsoft YaHei UI", "Microsoft YaHei", "TkDefaultFont")


# ---------- 玻璃面板 RGBA（规范 §2.2 / §3.3） ----------
# 值为 (r, g, b, alpha)。面板越靠上层越不透明；弹出层和提示最不透明。
GLASS_DARK = {
    "SIDEBAR": (20, 22, 26, 0.72),
    "PANEL": (24, 26, 32, 0.76),
    "FLOAT": (35, 37, 45, 0.88),
    "MENU": (32, 34, 41, 0.94),
    "TOOLTIP": (38, 40, 48, 0.96),
    "BORDER": (255, 255, 255, 0.06),
    "BORDER_STRONG": (255, 255, 255, 0.10),
    "BORDER_FOCUS": (167, 139, 250, 0.50),
    "ACCENT_BORDER": (167, 139, 250, 0.40),
    "SELECTED": (167, 139, 250, 0.14),
}
GLASS_LIGHT = {
    "SIDEBAR": (255, 255, 255, 0.55),
    "PANEL": (255, 255, 255, 0.70),
    "FLOAT": (255, 255, 255, 0.88),
    "MENU": (255, 255, 255, 0.94),
    "TOOLTIP": (255, 255, 255, 0.96),
    "BORDER": (20, 16, 40, 0.06),
    "BORDER_STRONG": (20, 16, 40, 0.10),
    "BORDER_FOCUS": (112, 82, 181, 0.50),
    "ACCENT_BORDER": (112, 82, 181, 0.40),
    "SELECTED": (112, 82, 181, 0.14),
}

# ---------- 环境光（规范 §3.4）：(相对 x, 相对 y, 相对半径, (r,g,b), alpha) ----------
# 左侧偏蓝绿、右侧偏紫，中心保持深色；强度刻意很弱，只营造氛围。
AMBIENT_DARK = {
    "base": (16, 17, 20),
    "glows": (
        (0.08, 0.12, 0.62, (76, 150, 190), 0.11),
        (0.96, 0.92, 0.66, (167, 139, 250), 0.13),
        (0.60, -0.10, 0.45, (120, 100, 200), 0.05),
    ),
}
AMBIENT_LIGHT = {
    "base": (244, 243, 248),
    "glows": (
        (0.06, 0.10, 0.60, (120, 170, 230), 0.20),
        (0.96, 0.94, 0.64, (167, 139, 250), 0.18),
    ),
}


def blend(rgba: tuple, over: tuple) -> str:
    """RGBA 叠加在不透明 RGB 上，返回 ``#RRGGBB``。"""
    r, g, b, a = rgba
    return "#{:02X}{:02X}{:02X}".format(
        int(round(over[0] + (r - over[0]) * a)),
        int(round(over[1] + (g - over[1]) * a)),
        int(round(over[2] + (b - over[2]) * a)),
    )


def hex_to_rgb(value: str) -> tuple:
    value = value.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


# ---------- 调色板：深色（规范）/ 浅色（派生） ----------
# 键名与模块级常量一致；T.XXX 通过 __getattr__ 解析当前生效的调色板，
# 切换主题后无需重建控件，重绘即可拿到新颜色。
DARK = {
    "BASE": "#101114",
    "TEXT": "#E6E7EB", "TEXT_SECONDARY": "#B4B6C0", "TEXT_TERTIARY": "#858895",
    "TEXT_DISABLED": "#555863", "TEXT_ON_ACCENT": "#17131F",
    "ACCENT": "#A78BFA", "ACCENT_HOVER": "#B9A5FF", "ACCENT_PRESSED": "#8B72D6",
    "ACCENT_TEXT": "#B9A5FF",
    "CONTROL": "#16181D", "CONTROL_HOVER": "#292B34", "CONTROL_PRESSED": "#22242B",
    "CONTROL_BORDER": "#2C2E37", "CONTROL_BORDER_STRONG": "#4A4D59",
    "CONTROL_DISABLED": "#1B1D24",
    "FOCUS": "#A78BFA",
    "SUCCESS": "#65C99A", "SUCCESS_SOFT": "#1A2E27",
    "CAUTION": "#E7B96A", "CAUTION_SOFT": "#32291C",
    "CRITICAL": "#E87979", "CRITICAL_SOFT": "#342126",
    "INFO": "#75A9E8", "INFO_SOFT": "#1C2733",
    "IDLE": "#9296A3", "IDLE_SOFT": "#22242B",
    "DIVIDER": "#24262E",
    "SWITCH_OFF_TRACK": "#24262E", "SWITCH_OFF_STROKE": "#858895",
    "SCROLLBAR": "#4D505C",
}
LIGHT = {
    "BASE": "#F4F3F8",
    "TEXT": "#25232D", "TEXT_SECONDARY": "#625F70", "TEXT_TERTIARY": "#777382",
    "TEXT_DISABLED": "#A4A0AF", "TEXT_ON_ACCENT": "#FFFFFF",
    "ACCENT": "#7052B5", "ACCENT_HOVER": "#8060C7", "ACCENT_PRESSED": "#5E429E",
    "ACCENT_TEXT": "#64449F",
    "CONTROL": "#FFFFFF", "CONTROL_HOVER": "#F1EEF7", "CONTROL_PRESSED": "#E6E1F0",
    "CONTROL_BORDER": "#DED9E8", "CONTROL_BORDER_STRONG": "#ABA3BA",
    "CONTROL_DISABLED": "#EEECF3",
    "FOCUS": "#7052B5",
    "SUCCESS": "#237D55", "SUCCESS_SOFT": "#E1F1E9",
    "CAUTION": "#95651E", "CAUTION_SOFT": "#F9EFD9",
    "CRITICAL": "#B83E4A", "CRITICAL_SOFT": "#F8E4E7",
    "INFO": "#376DA9", "INFO_SOFT": "#E3EDF8",
    "IDLE": "#777382", "IDLE_SOFT": "#ECEBF2",
    "DIVIDER": "#E2DFE9",
    "SWITCH_OFF_TRACK": "#DAD5E4", "SWITCH_OFF_STROKE": "#777382",
    "SCROLLBAR": "#B8B2C4",
}


def _derive(palette: dict, glass: dict) -> dict:
    """由玻璃令牌推导不透明等价色，保证文字控件与面板底色一致。"""
    base = hex_to_rgb(palette["BASE"])
    derived = dict(palette)
    for key in ("SIDEBAR", "PANEL", "FLOAT", "MENU", "TOOLTIP"):
        derived[key] = blend(glass[key], base)
    panel = hex_to_rgb(derived["PANEL"])
    derived["ACCENT_SUBTLE"] = blend(glass["SELECTED"], panel)
    derived["PANEL_BORDER"] = blend(glass["BORDER"], panel)
    derived["PANEL_HOVER"] = blend((255, 255, 255, 0.04) if palette is DARK else (20, 16, 40, 0.04), panel)
    derived["MENU_BORDER"] = blend(glass["BORDER_STRONG"], hex_to_rgb(derived["MENU"]))
    derived["TOOLTIP_BORDER"] = blend(glass["BORDER_STRONG"], hex_to_rgb(derived["TOOLTIP"]))
    return derived


DARK = _derive(DARK, GLASS_DARK)
LIGHT = _derive(LIGHT, GLASS_LIGHT)

MODES = ("system", "light", "dark")
_mode = "system"
_resolved = "light"


def _read_system_mode() -> str:
    """读取系统「应用模式」：light / dark。"""
    if sys.platform != "win32":
        return "light"
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        )
        try:
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        finally:
            winreg.CloseKey(key)
        return "light" if int(value) else "dark"
    except Exception:
        return "light"


def set_mode(mode: str) -> str:
    """设置外观：``system`` / ``light`` / ``dark``，返回实际生效的模式。"""
    global _mode, _resolved
    _mode = mode if mode in MODES else "system"
    _resolved = _read_system_mode() if _mode == "system" else _mode
    return _resolved


def mode() -> str:
    return _mode


def resolved() -> str:
    """当前实际外观（light / dark）。"""
    return _resolved


def is_dark() -> bool:
    return _resolved == "dark"


def palette() -> dict:
    return DARK if _resolved == "dark" else LIGHT


def glass() -> dict:
    return GLASS_DARK if _resolved == "dark" else GLASS_LIGHT


def ambient() -> dict:
    return AMBIENT_DARK if _resolved == "dark" else AMBIENT_LIGHT


def color(name: str) -> str:
    """按名称取当前调色板的颜色。"""
    return palette()[name]


def __getattr__(name: str) -> str:
    """让 ``T.BASE`` 等模块级常量在运行时解析为当前调色板的值。"""
    table = palette()
    if name in table:
        return table[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


set_mode("system")


def enable_dpi_awareness() -> None:
    """在创建任何窗口前声明进程 DPI 感知，避免系统位图拉伸造成的模糊。"""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_AWARE_V2
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def _system_scale() -> float:
    if sys.platform != "win32":
        return 1.0
    try:
        dc = ctypes.windll.user32.GetDC(0)
        try:
            dpi = ctypes.windll.gdi32.GetDeviceCaps(dc, 88)  # LOGPIXELSX
        finally:
            ctypes.windll.user32.ReleaseDC(0, dc)
        return float(dpi) / 96.0 if dpi else 1.0
    except Exception:
        return 1.0


def work_area() -> tuple[int, int, int, int]:
    """桌面工作区 (left, top, width, height)，单位设备像素。"""
    if sys.platform == "win32":
        class _RECT(ctypes.Structure):
            _fields_ = [("l", ctypes.c_long), ("t", ctypes.c_long),
                        ("r", ctypes.c_long), ("b", ctypes.c_long)]

        rect = _RECT()
        try:
            if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
                return rect.l, rect.t, rect.r - rect.l, rect.b - rect.t
        except Exception:
            pass
    return 0, 0, 1280, 800


class Theme:
    """单个 Tk 顶层窗口的设计系统实例：DPI 比例、字体缓存与画笔。"""

    def __init__(self, root: tk.Misc):
        self.root = root
        # 以系统 DPI 为准：未声明 DPI 感知时 Tk 会报告 96，导致界面被系统放大后发虚
        self.scale = max(1.0, _system_scale())
        families = set(tkfont.families(root))
        self.text_family = self._pick(families, _TEXT_CANDIDATES)
        self.icon_family = self._pick(families, _ICON_CANDIDATES)
        self._fonts: dict[tuple, tkfont.Font] = {}
        self.painter = None  # 由 ui.paint.Painter 在首次使用时挂载

    @staticmethod
    def _pick(families: set, candidates: tuple) -> str:
        for name in candidates:
            if name in families:
                return name
        return "TkDefaultFont"

    def px(self, value: float) -> int:
        return int(round(value * self.scale))

    def font(self, size: float, weight: str = "normal", icon: bool = False) -> tkfont.Font:
        """取字体。``weight``: ``normal`` 400 / ``semibold`` 500-600 / ``bold`` 700。"""
        key = (size, weight, icon)
        f = self._fonts.get(key)
        if f is None:
            if icon:
                family, tk_weight = self.icon_family, "normal"
            else:
                family = self.text_family
                if weight == "semibold":
                    tk_weight = "bold" if family in _NO_SEMIBOLD_FAMILY else "normal"
                else:
                    tk_weight = "bold" if weight == "bold" else "normal"
            f = tkfont.Font(root=self.root, family=family, size=-self.px(size), weight=tk_weight)
            self._fonts[key] = f
        return f

    def measure(self, text: str, size: float, weight: str = "normal", icon: bool = False) -> int:
        return self.font(size, weight, icon).measure(text)

    def elide(self, text: str, max_width: int, size: float, weight: str = "normal") -> str:
        """按像素宽度截断并补省略号，用于长路径。"""
        f = self.font(size, weight)
        if f.measure(text) <= max_width:
            return text
        ellipsis = "…"
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if f.measure(text[:mid] + ellipsis) <= max_width:
                lo = mid
            else:
                hi = mid - 1
        return text[:lo] + ellipsis


_THEMES: "weakref.WeakKeyDictionary[tk.Misc, Theme]" = weakref.WeakKeyDictionary()

enable_dpi_awareness()


def theme(widget: tk.Misc) -> Theme:
    """取（或创建）widget 所属顶层窗口的设计系统实例。"""
    root = widget.winfo_toplevel()
    found = _THEMES.get(root)
    if found is None:
        found = Theme(root)
        _THEMES[root] = found
    return found


def resolve_icon(name: str) -> str:
    """Segoe Fluent Icons 的私有区字符。"""
    return chr(int(name, 16))


class Icons:
    """界面使用的线性图标码位（Segoe Fluent Icons），统一描边。"""

    # 导航
    DRIVE = "E8CE"
    CLOUD = "E753"
    SETTINGS = "E713"
    INFO = "E946"
    # 动作
    ADD = "E710"
    EDIT = "E70F"
    DELETE = "E74D"
    PLAY = "E768"
    STOP = "E71A"
    FOLDER = "E8B7"
    DOC = "E7C3"
    LINK = "E8A7"
    REFRESH = "E72C"
    CLEAR = "E894"
    COPY = "E8C8"
    POWER = "E7E8"
    HIDE = "ED1A"
    CHEVRON_DOWN = "E70D"
    CHEVRON_LEFT = "E76B"
    CHEVRON_RIGHT = "E76C"
    PANEL_RIGHT = "E90D"
    WARNING = "E7BA"
    GLOBE = "E774"
