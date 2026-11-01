"""Tern · Stencil 组件：Canvas + PIL 抗锯齿绘制，无 ttk。

设计约定（规范 §5）：
- 控件圆角 8px、卡片 12px、浮层 10px；控件高 28/32/40px；4px 间距体系。
- 按钮 primary/secondary/ghost/danger；输入框深色底、弱描边、聚焦强调色描边。
- 列表无边框，悬浮浅底，选中低透明度强调色；图标线性、默认二级文字色。
- 所有组件实现 ``apply_theme(bg)``：玻璃表面渲染后按采样底色重绘，主题切换无需重建。
"""
from __future__ import annotations

import tkinter as tk
from typing import Callable, Optional, Sequence

from . import theme as T
from .animate import Animation, ease_in_out_cubic, ease_out_cubic, lerp_color, reduce_motion
from .glass import Clear, surface_fill
from .paint import Painter, rgba_hex


def painter_for(widget: tk.Misc) -> Painter:
    th = T.theme(widget)
    if th.painter is None:
        th.painter = Painter(th, th.root)
    return th.painter


def _parent_bg(widget: tk.Misc) -> Optional[str]:
    try:
        return str(widget.master.cget("bg"))
    except Exception:
        return None


class Base(tk.Canvas):
    """统一画布：无边框、按 DPI 缩放、缓存 Painter。"""

    def __init__(self, master, bg: Optional[str] = None, **kw):
        self.th = T.theme(master)
        bg = bg or surface_fill(master)
        super().__init__(master, bg=bg, highlightthickness=0, bd=0, takefocus=0, **kw)
        self.painter = painter_for(self)
        self._bg = bg

    def px(self, v: float) -> int:
        return self.th.px(v)

    def apply_theme(self, bg: Optional[str] = None) -> None:
        bg = bg or _parent_bg(self) or self._bg
        self._bg = bg
        self.configure(bg=bg)
        self.painter = painter_for(self)
        self._refresh_palette()
        self._draw()

    def _refresh_palette(self) -> None:
        """子类覆盖：主题切换时清除悬停/按下残留。"""

    def _draw(self) -> None:
        """子类覆盖；裸画布无需绘制。"""


def _focus_color() -> str:
    return rgba_hex(T.glass()["BORDER_FOCUS"])


# ---------------------------------------------------------------- 文本
class Label(tk.Label):
    """主题化文本：按令牌取色，表面重绘时跟随底色。"""

    def __init__(self, master, text: str = "", *, size: float = T.FONT_BODY,
                 weight: str = "normal", token: str = "TEXT", bg: Optional[str] = None,
                 wrap: int = 0, anchor: str = "w", justify: str = "left",
                 textvariable: Optional[tk.Variable] = None, icon: bool = False, **kw):
        self.th = T.theme(master)
        self.token = token
        self._size = size
        self._weight = weight
        self._icon = icon
        super().__init__(master, text=text, textvariable=textvariable,
                         bg=bg or surface_fill(master), fg=T.color(token),
                         font=self.th.font(size, weight, icon=icon), anchor=anchor, justify=justify,
                         wraplength=wrap, bd=0, padx=0, pady=0, highlightthickness=0, **kw)

    def set_token(self, token: str) -> None:
        if token != self.token:
            self.token = token
            self.configure(fg=T.color(token))

    def apply_theme(self, bg: Optional[str] = None) -> None:
        self.configure(bg=bg or _parent_bg(self) or surface_fill(self), fg=T.color(self.token),
                       font=self.th.font(self._size, self._weight, icon=self._icon))


class Heading(Label):
    """页面 22px / 模块 16px 标题，半粗。"""

    def __init__(self, master, text: str = "", *, size: float = T.FONT_MODULE, **kw):
        super().__init__(master, text, size=size, weight="semibold", **kw)


class Caption(Label):
    """辅助说明：12px、二级文字色。"""

    def __init__(self, master, text: str = "", *, token: str = "TEXT_SECONDARY",
                 size: float = T.FONT_CAPTION, **kw):
        super().__init__(master, text, size=size, token=token, **kw)


class Divider(tk.Frame):
    """1px 微弱分隔线。"""

    def __init__(self, master, *, vertical: bool = False):
        th = T.theme(master)
        super().__init__(master, bg=T.DIVIDER, width=th.px(1) if vertical else 0,
                         height=0 if vertical else th.px(1), bd=0, highlightthickness=0)

    def apply_theme(self, bg: Optional[str] = None) -> None:
        self.configure(bg=T.DIVIDER)


# ---------------------------------------------------------------- 按钮
class Button(Base):
    """按钮（规范 §5.1）。

    variant: ``primary`` 强调色实心 / ``secondary`` 半透明底 + 弱描边 /
    ``ghost`` 无背景 / ``danger`` 红色文字。``set_selected`` 用于导航当前项。
    """

    HEIGHTS = {"sm": T.CONTROL_H_SM, "md": T.CONTROL_H, "lg": T.CONTROL_H_LG}

    def __init__(self, master, text: str, command: Optional[Callable] = None, *,
                 variant: str = "secondary", size: str = "md",
                 width: Optional[float] = None, glyph: str = "", align: str = "center",
                 padding: float = 12, bg: Optional[str] = None,
                 radius: float = T.RADIUS_CONTROL, font_size: float = T.FONT_BODY,
                 tooltip: str = "", name: Optional[str] = None):
        self._text = text
        self._glyph = glyph
        self._font_size = font_size
        self._variant = variant
        self._selected = False
        self._align = align
        self._command = command
        self._enabled = True
        self._hover = False
        self._pressed = False
        self._focused = False
        self._hover_t = 0.0
        self._hover_from = 0.0
        self._radius = radius
        self._padding = padding
        self._fixed_w = width
        th = T.theme(master)
        self._h = th.px(self.HEIGHTS.get(size, T.CONTROL_H))
        w = th.px(width) if width is not None else self._measure_w(th)
        super().__init__(master, bg=bg, width=w, height=self._h, **({"name": name} if name else {}))
        self._hover_anim = Animation(self, duration=T.MOTION_HOVER, easing=ease_out_cubic)
        self.configure(cursor="hand2", takefocus=1)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<FocusIn>", lambda e: self._set_focus(True))
        self.bind("<FocusOut>", lambda e: self._set_focus(False))
        self.bind("<Return>", lambda e: self.invoke())
        self.bind("<space>", lambda e: self.invoke())
        self.bind("<Configure>", lambda e: self._draw())
        if tooltip:
            Tooltip(self, tooltip)
        self._draw()

    # ---------- 状态 ----------
    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if self._enabled == enabled:
            return
        self._enabled = enabled
        self.configure(cursor="hand2" if enabled else "arrow", takefocus=1 if enabled else 0)
        if not enabled:
            self._hover = self._pressed = False
            self._hover_t = 0.0
        self._draw()

    def set_text(self, text: str) -> None:
        if text != self._text:
            self._text = text
            if self._fixed_w is None:
                self.configure(width=self._measure_w(self.th))
            self._draw()

    def set_glyph(self, glyph: str) -> None:
        if glyph != self._glyph:
            self._glyph = glyph
            self._draw()

    def set_selected(self, selected: bool) -> None:
        if self._selected != selected:
            self._selected = selected
            self._draw()

    def invoke(self) -> str:
        if self._enabled and self._command:
            self._command()
        return "break"

    def _measure_w(self, th: T.Theme) -> int:
        pad = th.px(self._padding)
        text_w = th.measure(self._text, self._font_size, "semibold") if self._text else 0
        if self._glyph:
            text_w += th.px(16) + (th.px(8) if self._text else 0)
        return text_w + pad * 2

    def _refresh_palette(self) -> None:
        self._hover = self._pressed = False
        self._hover_t = 0.0

    # ---------- 交互 ----------
    def _set_focus(self, focused: bool) -> None:
        self._focused = focused
        self._draw()

    def _animate_hover(self, target: float) -> None:
        if abs(self._hover_t - target) < 0.01:
            return
        self._hover_from = self._hover_t

        def frame(t: float) -> None:
            self._hover_t = self._hover_from + (target - self._hover_from) * t
            self._draw()

        self._hover_anim.start(on_frame=frame)

    def _on_enter(self, _e=None) -> None:
        if self._enabled:
            self._hover = True
            self._animate_hover(1.0)

    def _on_leave(self, _e=None) -> None:
        self._hover = self._pressed = False
        self._animate_hover(0.0)

    def _on_press(self, _e=None) -> None:
        if self._enabled:
            self._pressed = True
            self.focus_set()
            self._draw()

    def _on_release(self, event=None) -> None:
        if not self._enabled:
            return
        was = self._pressed
        self._pressed = False
        self._draw()
        inside = event is None or (0 <= event.x <= self.winfo_width() and 0 <= event.y <= self.winfo_height())
        if was and inside and self._command:
            self._command()

    # ---------- 绘制 ----------
    def _colors(self) -> tuple:
        """(fill, hover, pressed, text, border, glyph) 按当前调色板生成。"""
        return {
            "primary": (T.ACCENT, T.ACCENT_HOVER, T.ACCENT_PRESSED, T.TEXT_ON_ACCENT, None, T.TEXT_ON_ACCENT),
            "secondary": (T.CONTROL, T.CONTROL_HOVER, T.CONTROL_PRESSED, T.TEXT, T.CONTROL_BORDER, T.TEXT_SECONDARY),
            "ghost": (None, T.CONTROL_HOVER, T.CONTROL_PRESSED, T.TEXT_SECONDARY, None, T.TEXT_SECONDARY),
            "danger": (None, T.CRITICAL_SOFT, T.CRITICAL_SOFT, T.CRITICAL, None, T.CRITICAL),
        }[self._variant]

    def _draw(self) -> None:
        self.delete("all")
        fill, hover, pressed, fg, border, glyph_fg = self._colors()
        if self._selected:
            fill, fg, glyph_fg, border = T.ACCENT_SUBTLE, T.ACCENT_TEXT, T.ACCENT, None
        if not self._enabled:
            if self._variant == "primary":
                fill, fg, glyph_fg, border = T.CONTROL_DISABLED, T.TEXT_DISABLED, T.TEXT_DISABLED, None
            else:
                fill, fg, glyph_fg, border = None, T.TEXT_DISABLED, T.TEXT_DISABLED, None
        elif self._pressed:
            fill = pressed
        elif self._hover_t > 0.01:
            fill = lerp_color(fill or self._bg, hover, self._hover_t)
            if self._variant == "ghost" and not self._selected:
                fg = lerp_color(fg, T.TEXT, self._hover_t)
        w, h = self.winfo_width(), self.winfo_height()
        if w <= 1:
            w, h = int(self["width"]), int(self["height"])
        radius = int(round(self._radius * self.th.scale))
        if fill:
            self.create_image(0, 0, anchor="nw", image=self.painter.shape(w, h, radius, fill, border, 1 if border else 0))
        pad = self.px(self._padding)
        font = self.th.font(self._font_size, "semibold")
        gw = self.px(16)
        available = max(0, w - pad * 2)
        glyph = self._glyph
        if glyph and self._text and gw + self.px(8) + font.measure(self._text) > available:
            glyph = ""              # 宽度不足时优先保留文字，省略图标
        gap = self.px(8) if (glyph and self._text) else 0
        text = self.th.elide(self._text, max(0, available - (gw + gap if glyph else 0)), self._font_size, "semibold")
        tw = font.measure(text) if text else 0
        total = (gw if glyph else 0) + gap + tw
        x = pad if self._align == "left" else (w - total) / 2
        if glyph:
            self.create_text(x + gw / 2, h / 2, text=T.resolve_icon(glyph), font=self.th.font(14, icon=True), fill=glyph_fg)
            x += gw + gap
        if text:
            self.create_text(x, h / 2, text=text, anchor="w", font=font, fill=fg)
        if self._focused and self._enabled:
            inset = self.px(1)
            self.create_image(inset, inset, anchor="nw",
                              image=self.painter.focus_ring(w - inset * 2, h - inset * 2, max(0, radius - inset), self.px(2), _focus_color()))


class IconButton(Base):
    """方形线性图标按钮：默认二级文字色，悬停提亮并出现浅底。"""

    def __init__(self, master, glyph: str, command: Optional[Callable] = None, *,
                 size: float = T.CONTROL_H, icon_size: float = 16, tooltip: str = "",
                 bg: Optional[str] = None, radius: float = T.RADIUS_CONTROL,
                 token: str = "TEXT_SECONDARY"):
        self._glyph = glyph
        self._command = command
        self._icon_size = icon_size
        self._radius = radius
        self._token = token
        self._enabled = True
        self._pressed = False
        self._focused = False
        self._hover_t = 0.0
        th = T.theme(master)
        s = th.px(size)
        super().__init__(master, bg=bg, width=s, height=s)
        self._hover_anim = Animation(self, duration=T.MOTION_HOVER, easing=ease_out_cubic)
        self.configure(cursor="hand2", takefocus=1)
        self.bind("<Enter>", lambda e: self._set_hover(1.0))
        self.bind("<Leave>", lambda e: self._set_hover(0.0))
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<FocusIn>", lambda e: self._set_focus(True))
        self.bind("<FocusOut>", lambda e: self._set_focus(False))
        self.bind("<Return>", lambda e: self.invoke())
        self.bind("<space>", lambda e: self.invoke())
        if tooltip:
            Tooltip(self, tooltip)
        self._draw()

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)
        self.configure(cursor="hand2" if enabled else "arrow", takefocus=1 if enabled else 0)
        self._draw()

    def set_glyph(self, glyph: str) -> None:
        self._glyph = glyph
        self._draw()

    def invoke(self) -> str:
        if self._enabled and self._command:
            self._command()
        return "break"

    def _refresh_palette(self) -> None:
        self._hover_t = 0.0
        self._pressed = False

    def _set_focus(self, focused: bool) -> None:
        self._focused = focused
        self._draw()

    def _set_hover(self, target: float) -> None:
        if not self._enabled and target > 0:
            return
        start = self._hover_t

        def frame(t: float) -> None:
            self._hover_t = start + (target - start) * t
            self._draw()

        self._hover_anim.start(on_frame=frame)

    def _on_press(self, _e=None) -> None:
        if self._enabled:
            self._pressed = True
            self.focus_set()
            self._draw()

    def _on_release(self, event=None) -> None:
        if not self._enabled:
            return
        was = self._pressed
        self._pressed = False
        self._draw()
        inside = event is None or (0 <= event.x <= self.winfo_width() and 0 <= event.y <= self.winfo_height())
        if was and inside and self._command:
            self._command()

    def _draw(self) -> None:
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w <= 1:
            w = h = int(self["width"])
        radius = int(round(self._radius * self.th.scale))
        if self._enabled and (self._hover_t > 0.01 or self._pressed):
            fill = lerp_color(self._bg, T.CONTROL_PRESSED if self._pressed else T.CONTROL_HOVER, max(self._hover_t, 1.0 if self._pressed else 0))
            self.create_image(0, 0, anchor="nw", image=self.painter.shape(w, h, radius, fill))
        fg = lerp_color(T.color(self._token), T.TEXT, self._hover_t) if self._enabled else T.TEXT_DISABLED
        self.create_text(w / 2, h / 2, text=T.resolve_icon(self._glyph), font=self.th.font(self._icon_size, icon=True), fill=fg)
        if self._focused and self._enabled:
            inset = self.px(1)
            self.create_image(inset, inset, anchor="nw",
                              image=self.painter.focus_ring(w - inset * 2, h - inset * 2, max(0, radius - inset), self.px(2), _focus_color()))


# ---------------------------------------------------------------- 开关
class Switch(Base):
    """开关：绑定 BooleanVar 双向同步；40×20 轨道，开=强调色。"""

    W, H = 40, 20

    def __init__(self, master, variable: tk.BooleanVar, *, bg: Optional[str] = None,
                 on_change: Optional[Callable[[bool], None]] = None):
        self.variable = variable
        self._on_change = on_change
        self._enabled = True
        self._hover = False
        self._focused = False
        self._pos = 1.0 if variable.get() else 0.0
        th = T.theme(master)
        super().__init__(master, bg=bg, width=th.px(self.W), height=th.px(self.H))
        self._anim = Animation(self, duration=160, easing=ease_in_out_cubic)
        self.configure(cursor="hand2", takefocus=1)
        self.bind("<Button-1>", lambda e: self.toggle())
        self.bind("<space>", lambda e: self.toggle())
        self.bind("<Return>", lambda e: self.toggle())
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        self.bind("<FocusIn>", lambda e: self._set_focus(True))
        self.bind("<FocusOut>", lambda e: self._set_focus(False))
        self._trace = variable.trace_add("write", lambda *_: self._sync())
        self.bind("<Destroy>", self._on_destroy, add="+")
        self._draw()

    def _on_destroy(self, event) -> None:
        if event.widget is self:
            try:
                self.variable.trace_remove("write", self._trace)
            except Exception:
                pass

    def get(self) -> bool:
        return bool(self.variable.get())

    def toggle(self) -> str:
        if self._enabled:
            self.variable.set(not self.variable.get())
            if self._on_change:
                self._on_change(bool(self.variable.get()))
        return "break"

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)
        self.configure(cursor="hand2" if enabled else "arrow", takefocus=1 if enabled else 0)
        self._draw()

    def _sync(self) -> None:
        target = 1.0 if self.variable.get() else 0.0
        if abs(self._pos - target) < 0.01:
            return
        start = self._pos

        def frame(t: float) -> None:
            self._pos = start + (target - start) * t
            self._draw()

        self._anim.start(on_frame=frame, on_done=lambda: setattr(self, "_pos", target))

    def _set_hover(self, hover: bool) -> None:
        self._hover = hover
        self._draw()

    def _set_focus(self, focused: bool) -> None:
        self._focused = focused
        self._draw()

    def _refresh_palette(self) -> None:
        self._hover = False

    def _draw(self) -> None:
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w <= 1:
            w, h = self.px(self.W), self.px(self.H)
        on = self._pos >= 0.5
        if self._enabled:
            self.create_image(0, 0, anchor="nw", image=self.painter.switch_track(w, h, on, self._hover))
        else:
            self.create_image(0, 0, anchor="nw", image=self.painter.shape(w, h, h / 2, T.CONTROL_DISABLED, T.CONTROL_BORDER, 1))
        inset = self.px(4)
        d = max(self.px(8), int(round(h * 0.6)))
        x = inset + (w - d - inset * 2) * self._pos
        pad = self.painter.knob_pad(d)
        self.create_image(x - pad, (h - d) / 2 - pad, anchor="nw", image=self.painter.knob(d, on, self._hover))
        if self._focused and self._enabled:
            self.create_image(0, 0, anchor="nw", image=self.painter.focus_ring(w, h, h / 2, self.px(2), _focus_color()))


class ToggleRow(Clear):
    """一行开关设置：左侧标题 + 说明，右侧 Switch；点击整行切换。透明表面，不遮挡卡片玻璃。"""

    def __init__(self, master, text: str, variable: tk.BooleanVar, *, hint: str = "",
                 bg: Optional[str] = None, on_change: Optional[Callable[[bool], None]] = None):
        super().__init__(master)
        bg = bg or self.fill
        self.columnconfigure(0, weight=1)
        self.label = Label(self, text, bg=bg)
        self.label.grid(row=0, column=0, sticky="w")
        self.switch = Switch(self, variable, bg=bg, on_change=on_change)
        self.switch.grid(row=0, column=1, sticky="e", padx=(self.th.px(T.SPACE_LG), 0))
        self.hint: Optional[Caption] = None
        if hint:
            self.hint = Caption(self, hint, bg=bg, wrap=self.th.px(520))
            self.hint.grid(row=1, column=0, sticky="we", pady=(self.th.px(2), 0))
        for widget in (self, self.label) + ((self.hint,) if self.hint else ()):
            widget.bind("<Button-1>", lambda e: self.switch.toggle())
            widget.configure(cursor="hand2")
        self.bind("<Configure>", self._wrap, add="+")

    def _wrap(self, event) -> None:
        if self.hint is not None:
            self.hint.configure(wraplength=max(160, event.width - self.th.px(72)))

    def set_enabled(self, enabled: bool) -> None:
        self.switch.set_enabled(enabled)
        self.label.set_token("TEXT" if enabled else "TEXT_DISABLED")


# ---------------------------------------------------------------- 输入
class TextField(tk.Frame):
    """输入框（规范 §5.2）：深色底、弱描边、聚焦强调色描边、错误红色描边。

    ``width`` 为空时跟随容器宽度。``entry`` 为内部 tk.Entry。
    """

    def __init__(self, master, textvariable: tk.StringVar, *, placeholder: str = "",
                 show: str = "", width: Optional[float] = None, height: float = T.CONTROL_H,
                 bg: Optional[str] = None, on_change: Optional[Callable[[str], None]] = None,
                 trailing: float = 0):
        self.th = T.theme(master)
        bg = bg or surface_fill(master)
        self._bg = bg
        self._fixed_w = width
        self._h = self.th.px(height)
        self._trailing = self.th.px(trailing)
        self.var = textvariable
        self._placeholder = placeholder
        self._focused = False
        self._enabled = True
        self._error = False
        self._placeholder_label: Optional[tk.Label] = None
        super().__init__(master, bg=bg, bd=0, highlightthickness=0)
        self.canvas = Base(self, bg=bg, width=self.th.px(width) if width else 10, height=self._h)
        self.canvas.pack(fill="x" if width is None else "none", anchor="w")
        self.entry = tk.Entry(self.canvas, textvariable=textvariable, relief="flat", bd=0,
                              highlightthickness=0, bg=T.CONTROL, fg=T.TEXT,
                              font=self.th.font(T.FONT_BODY), insertbackground=T.TEXT,
                              show=show, disabledbackground=T.CONTROL_DISABLED,
                              disabledforeground=T.TEXT_DISABLED,
                              selectbackground=T.ACCENT_SUBTLE, selectforeground=T.TEXT)
        self.canvas.bind("<Button-1>", self._focus)
        self.canvas.bind("<Configure>", lambda e: self._draw())
        self.entry.bind("<FocusIn>", lambda e: self._set_focus(True))
        self.entry.bind("<FocusOut>", lambda e: self._set_focus(False))
        self.var.trace_add("write", lambda *_: self._sync_placeholder())
        if on_change is not None:
            self.var.trace_add("write", lambda *_: on_change(self.var.get()))
        self._draw()
        self._sync_placeholder()

    # ---------- 公共 ----------
    def focus_set(self) -> None:
        self.entry.focus_set()

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)
        self.entry.configure(state="normal" if enabled else "disabled")
        self._draw()
        self._sync_placeholder()

    def set_error(self, error: bool) -> None:
        if self._error != bool(error):
            self._error = bool(error)
            self._draw()

    def apply_theme(self, bg: Optional[str] = None) -> None:
        bg = bg or _parent_bg(self) or self._bg
        self._bg = bg
        self.configure(bg=bg)
        self.canvas.apply_theme(bg=bg)
        self.entry.configure(bg=T.CONTROL, fg=T.TEXT, insertbackground=T.TEXT,
                             disabledbackground=T.CONTROL_DISABLED, disabledforeground=T.TEXT_DISABLED,
                             selectbackground=T.ACCENT_SUBTLE, selectforeground=T.TEXT,
                             font=self.th.font(T.FONT_BODY))
        self._draw()
        self._sync_placeholder()

    # ---------- 内部 ----------
    def _focus(self, _e=None) -> None:
        if self._enabled:
            self.entry.focus_set()

    def _set_focus(self, focused: bool) -> None:
        self._focused = focused
        self._draw()
        self._sync_placeholder()

    def _sync_placeholder(self) -> None:
        show = bool(self._placeholder) and not self.var.get() and not self._focused
        fill = T.CONTROL if self._enabled else T.CONTROL_DISABLED
        if show and self._placeholder_label is None:
            label = tk.Label(self.canvas, text=self._placeholder, bg=fill, fg=T.TEXT_TERTIARY,
                             font=self.th.font(T.FONT_BODY), anchor="w", bd=0)
            label.bind("<Button-1>", self._focus)
            label.place(x=self.th.px(11), rely=0.5, anchor="w")
            self._placeholder_label = label
        elif not show and self._placeholder_label is not None:
            self._placeholder_label.destroy()
            self._placeholder_label = None
        elif show and self._placeholder_label is not None:
            self._placeholder_label.configure(bg=fill, fg=T.TEXT_TERTIARY)

    def _border_color(self) -> str:
        if self._error:
            return T.CRITICAL
        if self._focused and self._enabled:
            return T.FOCUS
        return T.CONTROL_BORDER

    def _draw(self) -> None:
        c = self.canvas
        c.delete("all")
        w = c.winfo_width() if self._fixed_w is None and c.winfo_width() > 1 else int(c["width"])
        h = self._h
        fill = T.CONTROL if self._enabled else T.CONTROL_DISABLED
        r = self.th.px(T.RADIUS_CONTROL)
        c.create_image(0, 0, anchor="nw", image=c.painter.shape(w, h, r, fill, self._border_color(), 1))
        pad = self.th.px(11)
        line_h = min(h - self.th.px(6), self.th.font(T.FONT_BODY).metrics("linespace"))
        c.create_window(pad, h / 2 - self.th.px(1), anchor="w", window=self.entry,
                        width=max(10, w - pad * 2 - self._trailing), height=line_h)
        self.entry.configure(bg=fill)
        self._draw_trailing(w, h)

    def _draw_trailing(self, w: int, h: int) -> None:
        """子类在右侧绘制附加元素。"""


class ComboField(TextField):
    """可编辑组合框：输入框 + 右侧下拉按钮，建议项由 ``set_values`` 提供。"""

    def __init__(self, master, textvariable: tk.StringVar, *, values: Sequence[str] = (), **kw):
        self._values = list(values)
        self._popup: Optional[Flyout] = None
        super().__init__(master, textvariable, trailing=T.CONTROL_H, **kw)
        self.canvas.tag_bind("chevron", "<Button-1>", lambda e: self.open())
        self.entry.bind("<Alt-Down>", lambda e: self.open())
        self.entry.bind("<Down>", lambda e: self.open())

    def set_values(self, values: Sequence[str]) -> None:
        values = list(values)
        if values != self._values:
            self._values = values
            self._draw()

    def open(self) -> str:
        if self._popup is not None or not self._enabled or not self._values:
            return "break"
        self._popup = Flyout(self.canvas, [(v, v) for v in self._values], self._choose,
                             current=self.var.get(), width=max(self.canvas.winfo_width(), self.th.px(200)))
        self._popup.win.bind("<Destroy>", self._popup_closed, add="+")
        self._popup.show()
        return "break"

    def _popup_closed(self, event) -> None:
        if self._popup is not None and event.widget is self._popup.win:
            self._popup = None
            self.entry.focus_set()

    def _choose(self, value) -> None:
        self._popup = None
        self.var.set(value)
        self.entry.focus_set()
        self.entry.icursor("end")

    def _draw_trailing(self, w: int, h: int) -> None:
        c = self.canvas
        enabled = self._enabled and bool(self._values)
        c.create_text(w - self.px(16), h / 2, text=T.resolve_icon(T.Icons.CHEVRON_DOWN),
                      font=self.th.font(10, icon=True), fill=T.TEXT_SECONDARY if enabled else T.TEXT_DISABLED, tags="chevron")
        c.create_rectangle(w - self._trailing, 0, w, h, fill="", outline="", tags="chevron")
        c.tag_bind("chevron", "<Enter>", lambda e: c.configure(cursor="hand2" if enabled else "arrow"))
        c.tag_bind("chevron", "<Leave>", lambda e: c.configure(cursor="xterm"))

    def px(self, v: float) -> int:
        return self.th.px(v)


class DropDown(Base):
    """只读选择器：显示当前项标签，点击/空格/回车弹出 Flyout；绑定 StringVar。"""

    def __init__(self, master, variable: tk.StringVar, options: Sequence[tuple[str, str]], *,
                 width: Optional[float] = None, height: float = T.CONTROL_H,
                 bg: Optional[str] = None, on_change: Optional[Callable[[str], None]] = None):
        self.variable = variable
        self._choices = list(options)
        self._on_change = on_change
        self._enabled = True
        self._focused = False
        self._hover_t = 0.0
        self._fixed_w = width
        self._popup: Optional[Flyout] = None
        th = T.theme(master)
        super().__init__(master, bg=bg, width=th.px(width) if width else 10, height=th.px(height))
        self._hover_anim = Animation(self, duration=T.MOTION_HOVER, easing=ease_out_cubic)
        self.configure(cursor="hand2", takefocus=1)
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Enter>", lambda e: self._set_hover(1.0))
        self.bind("<Leave>", lambda e: self._set_hover(0.0))
        self.bind("<Button-1>", lambda e: (self.focus_set(), self.open()))
        self.bind("<space>", lambda e: self.open())
        self.bind("<Return>", lambda e: self.open())
        self.bind("<Down>", lambda e: self.open())
        self.bind("<FocusIn>", lambda e: self._set_focus(True))
        self.bind("<FocusOut>", lambda e: self._set_focus(False))
        self._trace = variable.trace_add("write", lambda *_: self._draw())
        self.bind("<Destroy>", self._on_destroy, add="+")
        self._draw()

    def _on_destroy(self, event) -> None:
        if event.widget is self:
            try:
                self.variable.trace_remove("write", self._trace)
            except Exception:
                pass

    def set_options(self, options: Sequence[tuple[str, str]]) -> None:
        self._choices = list(options)
        self._draw()

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)
        self.configure(cursor="hand2" if enabled else "arrow", takefocus=1 if enabled else 0)
        self._draw()

    def label(self) -> str:
        value = self.variable.get()
        for text, candidate in self._choices:
            if candidate == value:
                return text
        return value

    def open(self) -> str:
        if self._popup is not None or not self._enabled or not self._choices:
            return "break"
        self._popup = Flyout(self, self._choices, self._choose, current=self.variable.get(),
                             width=max(self.winfo_width(), self.th.px(160)))
        self._popup.win.bind("<Destroy>", self._popup_closed, add="+")
        self._popup.show()
        return "break"

    def _popup_closed(self, event) -> None:
        if self._popup is not None and event.widget is self._popup.win:
            self._popup = None
            self._draw()

    def _choose(self, value) -> None:
        self._popup = None
        if value != self.variable.get():
            self.variable.set(value)
            if self._on_change:
                self._on_change(value)
        self.focus_set()

    def _refresh_palette(self) -> None:
        self._hover_t = 0.0

    def _set_focus(self, focused: bool) -> None:
        self._focused = focused
        self._draw()

    def _set_hover(self, target: float) -> None:
        if not self._enabled:
            return
        start = self._hover_t

        def frame(t: float) -> None:
            self._hover_t = start + (target - start) * t
            self._draw()

        self._hover_anim.start(on_frame=frame)

    def _draw(self) -> None:
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w <= 1:
            w, h = int(self["width"]), int(self["height"])
        r = self.px(T.RADIUS_CONTROL)
        if self._enabled:
            fill = lerp_color(T.CONTROL, T.CONTROL_HOVER, self._hover_t)
            border = T.FOCUS if self._focused else lerp_color(T.CONTROL_BORDER, T.CONTROL_BORDER_STRONG, self._hover_t)
            fg, chevron = T.TEXT, T.TEXT_SECONDARY
        else:
            fill, border, fg, chevron = T.CONTROL_DISABLED, T.CONTROL_BORDER, T.TEXT_DISABLED, T.TEXT_DISABLED
        self.create_image(0, 0, anchor="nw", image=self.painter.shape(w, h, r, fill, border, 1))
        text = self.th.elide(self.label(), w - self.px(40), T.FONT_BODY)
        self.create_text(self.px(11), h / 2, anchor="w", text=text, font=self.th.font(T.FONT_BODY), fill=fg)
        self.create_text(w - self.px(16), h / 2, text=T.resolve_icon(T.Icons.CHEVRON_DOWN), font=self.th.font(10, icon=True), fill=chevron)


# ---------------------------------------------------------------- 浮出层
class Flyout:
    """弹出菜单（规范 §5.5）：比面板更不透明、柔和阴影；Esc / 点击外部关闭。"""

    def __init__(self, anchor: tk.Misc, options: Sequence[tuple[str, object]],
                 on_select: Callable[[object], None], *, current=None, width: Optional[int] = None):
        self.th = th = T.theme(anchor)
        self._anchor = anchor
        self.on_select = on_select
        self.option_h = th.px(T.CONTROL_H)
        self.pad = th.px(T.SPACE_XS)
        self._choices = list(options)
        self._current = current
        self._hover = -1
        self.w = max(width or 0, max(th.measure(label, T.FONT_BODY) for label, _v in self._choices) + th.px(56))
        self.h = min(len(self._choices), 10) * self.option_h + self.pad * 2
        self.win = tk.Toplevel(anchor)
        self.win.withdraw()
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.canvas = Base(self.win, bg=T.MENU)
        self.canvas.pack()
        self._photo, self._shadow_pad = self.canvas.painter.elevated(
            self.w, self.h, th.px(T.RADIUS_FLYOUT), T.MENU, T.MENU_BORDER,
            blur=th.px(12), dy=th.px(4), opacity=70)
        self._box_w = self.w + self._shadow_pad * 2
        self._box_h = self.h + self._shadow_pad * 2
        self.canvas.configure(width=self._box_w, height=self._box_h)
        self._scroll = 0
        self._anim = Animation(self.win, duration=T.MOTION_MENU, easing=ease_out_cubic)
        self.canvas.bind("<Motion>", lambda e: self._set_hover(self._row_at(e.y)))
        self.canvas.bind("<Leave>", lambda e: self._set_hover(-1))
        self.canvas.bind("<Button-1>", self._on_click)
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.win.bind("<Escape>", lambda e: self.close())
        self.win.bind("<Up>", lambda e: self._move(-1))
        self.win.bind("<Down>", lambda e: self._move(1))
        self.win.bind("<Return>", lambda e: self._pick(self._hover))
        self.win.bind("<FocusOut>", self._on_focus_out)
        self.win.bind("<ButtonPress-1>", self._on_outside_press, add="+")
        self._draw()

    def _draw(self) -> None:
        c, th, pad = self.canvas, self.th, self._shadow_pad
        c.delete("all")
        c.create_image(0, 0, anchor="nw", image=self._photo)
        r = th.px(T.RADIUS_CONTROL - 2)
        visible = self._choices[self._scroll:self._scroll + 10]
        for offset, (label, value) in enumerate(visible):
            i = offset + self._scroll
            y = pad + self.pad + offset * self.option_h
            selected = value == self._current
            if selected or i == self._hover:
                fill = T.ACCENT_SUBTLE if selected else T.CONTROL_HOVER
                c.create_image(pad + self.pad, y, anchor="nw", image=c.painter.shape(self.w - self.pad * 2, self.option_h, r, fill))
            if selected:
                bar_w, bar_h = th.px(3), th.px(16)
                c.create_image(pad + self.pad + th.px(6), y + (self.option_h - bar_h) / 2, anchor="nw",
                               image=c.painter.shape(bar_w, bar_h, bar_w / 2, T.ACCENT))
            text = th.elide(str(label), self.w - th.px(40), T.FONT_BODY)
            c.create_text(pad + self.pad + th.px(18 if selected else 12), y + self.option_h / 2, anchor="w", text=text,
                          font=th.font(T.FONT_BODY, "semibold" if selected else "normal"),
                          fill=T.ACCENT_TEXT if selected else T.TEXT)

    def _row_at(self, y: int) -> int:
        local = y - self._shadow_pad - self.pad
        if local < 0:
            return -1
        i = int(local // self.option_h) + self._scroll
        return i if self._scroll <= i < min(len(self._choices), self._scroll + 10) else -1

    def _set_hover(self, i: int) -> None:
        if i != self._hover:
            self._hover = i
            self.canvas.configure(cursor="hand2" if i >= 0 else "arrow")
            self._draw()

    def _move(self, delta: int) -> str:
        i = max(0, min(len(self._choices) - 1, (self._hover if self._hover >= 0 else -1) + delta))
        if i < self._scroll:
            self._scroll = i
        elif i >= self._scroll + 10:
            self._scroll = i - 9
        self._set_hover(i)
        return "break"

    def _on_wheel(self, event) -> str:
        if len(self._choices) > 10:
            self._scroll = max(0, min(len(self._choices) - 10, self._scroll - int(event.delta / 120)))
            self._draw()
        return "break"

    def _on_click(self, event) -> None:
        self._pick(self._row_at(event.y))

    def _pick(self, i: int) -> str:
        if i < 0:
            self.close()
            return "break"
        _label, value = self._choices[i]
        self.close()
        self.on_select(value)
        return "break"

    def _on_outside_press(self, event) -> None:
        """局部 grab 会把窗口外的按下事件交给本窗口：坐标超出即关闭。"""
        x, y = event.x_root - self.win.winfo_rootx(), event.y_root - self.win.winfo_rooty()
        if not (0 <= x < self._box_w and 0 <= y < self._box_h):
            self.close()

    def _on_focus_out(self, _event) -> None:
        self.win.after(60, self._close_if_unfocused)

    def _close_if_unfocused(self) -> None:
        try:
            if self.win.winfo_exists() and self.win.focus_displayof() is None:
                self.close()
        except tk.TclError:
            pass

    def show(self) -> None:
        anchor = self._anchor
        self.win.update_idletasks()
        x = anchor.winfo_rootx() - self._shadow_pad
        y = anchor.winfo_rooty() + anchor.winfo_height() + self.th.px(4) - self._shadow_pad
        left, top, aw, ah = T.work_area()
        if y + self._box_h > top + ah:
            y = anchor.winfo_rooty() - self._box_h - self.th.px(4) + self._shadow_pad
        x = max(left, min(x, left + aw - self._box_w))
        y = max(top, min(y, top + ah - self._box_h))
        self._target = (x, y)
        if self._current is not None:
            for i, (_l, v) in enumerate(self._choices):
                if v == self._current:
                    self._hover = i
                    self._scroll = max(0, min(len(self._choices) - 10, i - 4)) if len(self._choices) > 10 else 0
                    break
        self._draw()
        if reduce_motion():
            self.win.geometry(f"{self._box_w}x{self._box_h}+{x}+{y}")
            self.win.attributes("-alpha", 1.0)
        else:
            self.win.geometry(f"{self._box_w}x{self._box_h}+{x}+{y + self.th.px(6)}")
            self.win.attributes("-alpha", 0.0)
        self.win.deiconify()
        self.win.lift()
        self.win.focus_force()
        self.win.grab_set()
        if not reduce_motion():
            def frame(t: float) -> None:
                try:
                    self.win.geometry(f"{self._box_w}x{self._box_h}+{x}+{y + int(round(self.th.px(6) * (1 - t)))}")
                    self.win.attributes("-alpha", t)
                except tk.TclError:
                    pass
            self._anim.start(on_frame=frame)

    def close(self) -> None:
        try:
            self.win.grab_release()
            self.win.destroy()
        except tk.TclError:
            pass


# ---------------------------------------------------------------- 提示
class Tooltip:
    """悬停 500ms 后出现的提示：最不透明的表面 + 弱描边。"""

    DELAY_MS = 500

    def __init__(self, widget: tk.Misc, text: str):
        self.widget = widget
        self.text = text
        self.th = T.theme(widget)
        self._job = None
        self._win: Optional[tk.Toplevel] = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")
        widget.bind("<Destroy>", self._hide, add="+")

    def set_text(self, text: str) -> None:
        self.text = text

    def _schedule(self, _e=None) -> None:
        self._cancel()
        self._job = self.widget.after(self.DELAY_MS, self._show)

    def _cancel(self) -> None:
        if self._job is not None:
            try:
                self.widget.after_cancel(self._job)
            except Exception:
                pass
            self._job = None

    def _show(self) -> None:
        self._job = None
        if self._win is not None or not self.text:
            return
        th = self.th
        win = tk.Toplevel(self.widget)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        pad_x, pad_y = th.px(10), th.px(6)
        w = th.measure(self.text, T.FONT_CAPTION) + pad_x * 2
        h = th.font(T.FONT_CAPTION).metrics("linespace") + pad_y * 2
        canvas = Base(win, bg=T.TOOLTIP)
        canvas.pack()
        photo, pad = canvas.painter.elevated(w, h, th.px(6), T.TOOLTIP, T.TOOLTIP_BORDER, blur=th.px(8), dy=th.px(2), opacity=60)
        canvas.configure(width=w + pad * 2, height=h + pad * 2)
        canvas.create_image(0, 0, anchor="nw", image=photo)
        canvas.create_text(pad + w / 2, pad + h / 2, text=self.text, font=th.font(T.FONT_CAPTION), fill=T.TEXT)
        x = self.widget.winfo_rootx() + self.widget.winfo_width() // 2 - w // 2 - pad
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + th.px(4) - pad
        left, top, aw, ah = T.work_area()
        x = max(left, min(x, left + aw - w - pad * 2))
        if y + h + pad * 2 > top + ah:
            y = self.widget.winfo_rooty() - h - th.px(4) - pad
        win.geometry(f"{w + pad * 2}x{h + pad * 2}+{x}+{y}")
        self._win = win

    def _hide(self, _e=None) -> None:
        self._cancel()
        if self._win is not None:
            try:
                self._win.destroy()
            except tk.TclError:
                pass
            self._win = None


# ---------------------------------------------------------------- 滚动容器
class ScrollFrame(tk.Frame):
    """滚动区：透明主体露出背景，自动隐藏的细滚动条；滚动后重绘内部玻璃。"""

    def __init__(self, master, bg: Optional[str] = None):
        self.th = T.theme(master)
        bg = bg or surface_fill(master)
        super().__init__(master, bg=bg, bd=0, highlightthickness=0)
        self.canvas = Clear(self)
        self.canvas.pack(fill="both", expand=True)
        self.body = Clear(self.canvas)
        self._window = self.canvas.create_window(0, 0, anchor="nw", window=self.body)
        self._indicator_job = None
        self._render_job = None
        self._hover_bar = False
        self._drag_from = 0.0
        self._drag_start = 0.0
        self.body.bind("<Configure>", self._on_body_configure, add="+")
        self.canvas.bind("<Configure>", self._on_canvas_configure, add="+")
        self._top = self.winfo_toplevel()
        self._bindings = [(seq, self._top.bind(seq, cb, add="+")) for seq, cb in (
            ("<MouseWheel>", self._on_wheel),
            ("<Prior>", lambda e: self._on_page_key(e, -1)),
            ("<Next>", lambda e: self._on_page_key(e, 1)))]
        self.bind("<Destroy>", self._cleanup, add="+")
        self.canvas.tag_bind("scrollbar", "<Enter>", lambda e: self._set_bar_hover(True))
        self.canvas.tag_bind("scrollbar", "<Leave>", lambda e: self._set_bar_hover(False))
        self.canvas.tag_bind("scrollbar", "<ButtonPress-1>", self._on_bar_press)
        self.canvas.tag_bind("scrollbar", "<B1-Motion>", self._on_bar_drag)
        self.canvas.tag_bind("scrollbar", "<ButtonRelease-1>", lambda e: self._set_bar_hover(False))

    def apply_theme(self, bg: Optional[str] = None) -> None:
        bg = bg or _parent_bg(self) or surface_fill(self)
        self.configure(bg=bg)

    def _owns(self, event) -> bool:
        widget = event.widget
        try:
            return (self.winfo_ismapped() and str(widget).startswith(str(self) + ".")
                    and widget.winfo_class() != "Text")
        except tk.TclError:
            return False

    def _cleanup(self, event) -> None:
        if event.widget is not self:
            return
        for seq, binding in self._bindings:
            try:
                self._top.unbind(seq, binding)
            except tk.TclError:
                pass
        for job in (self._indicator_job, self._render_job):
            if job:
                try:
                    self.after_cancel(job)
                except Exception:
                    pass

    def _on_body_configure(self, _e=None) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        self._show_indicator(persist=True)

    def _on_canvas_configure(self, event) -> None:
        self.canvas.itemconfigure(self._window, width=event.width)
        self._show_indicator(persist=True)

    def _scrolled(self) -> None:
        self._show_indicator()
        if self._render_job is None:
            self._render_job = self.after_idle(self._render_body)

    def _render_body(self) -> None:
        self._render_job = None
        try:
            self.canvas.request_render()
        except tk.TclError:
            pass

    def _on_wheel(self, event):
        if not self._owns(event) or self.canvas.yview() == (0.0, 1.0):
            return
        steps = int(event.delta / 120) or (1 if event.delta > 0 else -1)
        self.canvas.yview_scroll(-steps * 3, "units")
        self._scrolled()
        return "break"

    def _on_page_key(self, event, direction: int):
        if self._owns(event):
            self.canvas.yview_scroll(direction, "pages")
            self._scrolled()
            return "break"

    def _set_bar_hover(self, hover: bool) -> None:
        if self._hover_bar != hover:
            self._hover_bar = hover
            self.canvas.configure(cursor="hand2" if hover else "")
            self._show_indicator(persist=True)

    def _on_bar_press(self, event) -> None:
        self._drag_from = event.y_root
        self._drag_start = self.canvas.yview()[0]

    def _on_bar_drag(self, event) -> None:
        first, last = self.canvas.yview()
        span = last - first
        if span >= 1.0:
            return
        delta = (event.y_root - self._drag_from) / max(1, self.canvas.winfo_height())
        self.canvas.yview_moveto(max(0.0, min(1.0 - span, self._drag_start + delta)))
        self._scrolled()

    def _show_indicator(self, persist: bool = False) -> None:
        self.canvas.delete("scrollbar")
        first, last = self.canvas.yview()
        h, w = self.canvas.winfo_height(), self.canvas.winfo_width()
        if last - first < 0.999:
            bar_h = max(self.th.px(32), int(h * (last - first)))
            y = int(h * first)
            width = self.th.px(6 if self._hover_bar else 3)
            x = w - self.th.px(4) - width / 2
            self.canvas.create_line(x, y, x, y + bar_h, fill=T.SCROLLBAR, width=width, capstyle="round", tags="scrollbar")
        if self._indicator_job:
            try:
                self.after_cancel(self._indicator_job)
            except Exception:
                pass
            self._indicator_job = None
        if not persist:
            self._indicator_job = self.after(1200, self._hide_indicator)

    def _hide_indicator(self) -> None:
        self._indicator_job = None
        if not self._hover_bar:
            self.canvas.delete("scrollbar")

    def scroll_to_top(self) -> None:
        self.canvas.yview_moveto(0)
        self._scrolled()

    def ensure_visible(self, widget: tk.Misc) -> None:
        """把 widget 滚进可视范围（表单字段聚焦时）。"""
        self.update_idletasks()
        top = widget.winfo_rooty() - self.body.winfo_rooty()
        bottom = top + widget.winfo_height()
        visible_top = self.canvas.canvasy(0)
        height = max(1, self.body.winfo_height())
        view_h = self.canvas.winfo_height()
        if top < visible_top:
            self.canvas.yview_moveto(max(0, top - self.th.px(12)) / height)
        elif bottom > visible_top + view_h:
            self.canvas.yview_moveto((bottom - view_h + self.th.px(12)) / height)
        else:
            return
        self._scrolled()


# ---------------------------------------------------------------- 状态标签
TONES = {
    "success": ("SUCCESS", "SUCCESS_SOFT"),
    "caution": ("CAUTION", "CAUTION_SOFT"),
    "critical": ("CRITICAL", "CRITICAL_SOFT"),
    "info": ("INFO", "INFO_SOFT"),
    "idle": ("IDLE", "IDLE_SOFT"),
}


# ---------------------------------------------------------------- 文本区
class TextArea(tk.Frame):
    """只读多行文本：平底、可选择复制、保持阅读位置；供详情与诊断使用。"""

    def __init__(self, master, *, height: int = 6, bg: Optional[str] = None, size: float = T.FONT_CAPTION):
        self.th = T.theme(master)
        bg = bg or surface_fill(master)
        self._size = size
        self._content = ""
        super().__init__(master, bg=bg, bd=0, highlightthickness=0)
        self.text = tk.Text(self, height=height, wrap="word", relief="flat", bd=0, highlightthickness=0,
                            padx=0, pady=0, font=self.th.font(size), state="disabled", takefocus=1,
                            bg=bg, fg=T.TEXT_SECONDARY, insertbackground=T.TEXT,
                            selectbackground=T.ACCENT_SUBTLE, selectforeground=T.TEXT, cursor="arrow")
        self.text.pack(fill="both", expand=True)
        self.text.bind("<MouseWheel>", self._wheel)
        self.text.bind("<Control-c>", lambda e: self.copy_selection())

    def set_text(self, content: str) -> None:
        if content == self._content:
            return
        self._content = content
        yview = self.text.yview()[0]
        selection = self.text.tag_ranges("sel")
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", content)
        if selection:
            try:
                self.text.tag_add("sel", *selection)
            except tk.TclError:
                pass
        self.text.configure(state="disabled")
        self.text.yview_moveto(yview)

    def content(self) -> str:
        return self._content

    def copy_selection(self) -> str:
        try:
            content = self.text.get("sel.first", "sel.last")
        except tk.TclError:
            content = self._content
        self.clipboard_clear()
        self.clipboard_append(content)
        return "break"

    def _wheel(self, event) -> str:
        self.text.yview_scroll(-int(event.delta / 120) * 3, "units")
        return "break"

    def apply_theme(self, bg: Optional[str] = None) -> None:
        bg = bg or _parent_bg(self) or surface_fill(self)
        self.configure(bg=bg)
        self.text.configure(bg=bg, fg=T.TEXT_SECONDARY, insertbackground=T.TEXT,
                            selectbackground=T.ACCENT_SUBTLE, selectforeground=T.TEXT, font=self.th.font(self._size))


# ---------------------------------------------------------------- 挂载列表
class MountList(tk.Frame):
    """挂载列表（规范 §5.3）：无边框行，悬浮浅底，选中低透明度强调色 + 左侧强调条。

    行数据：``{"id", "name", "remote", "point", "state", "tone"}``。选择以稳定 ID
    保持；``exists`` 对所有已知 ID 成立（含被筛选隐藏的），``get_children`` 只含可见行。
    """

    ROW_H = 56
    HEADER_H = 28

    def __init__(self, master, *, on_select: Optional[Callable[[str], None]] = None,
                 on_activate: Optional[Callable[[str], None]] = None, bg: Optional[str] = None):
        self.th = T.theme(master)
        bg = bg or surface_fill(master)
        super().__init__(master, bg=bg, bd=0, highlightthickness=0)
        # 显式尺寸：Canvas 每次 configure 都会按自身 width/height 重新请求几何，默认 378×265 会扰动布局
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0, takefocus=1, cursor="arrow", width=10, height=10)
        self.canvas.pack(fill="both", expand=True)
        self.painter = painter_for(self)
        self._bg = bg
        self._rows: list[dict] = []
        self._known: set[str] = set()
        self._selected = ""
        self._hover = -1
        self._focused = False
        self._on_select = on_select
        self._on_activate = on_activate
        self._indicator_job = None
        c = self.canvas
        c.bind("<Configure>", lambda e: self._draw())
        c.bind("<Motion>", self._on_motion)
        c.bind("<Leave>", lambda e: self._set_hover(-1))
        c.bind("<Button-1>", self._on_click)
        c.bind("<Double-1>", self._on_double)
        c.bind("<Return>", lambda e: self._activate())
        c.bind("<Up>", lambda e: self._move(-1))
        c.bind("<Down>", lambda e: self._move(1))
        c.bind("<Home>", lambda e: self._move(-len(self._rows)))
        c.bind("<End>", lambda e: self._move(len(self._rows)))
        c.bind("<MouseWheel>", self._on_wheel)
        c.bind("<FocusIn>", lambda e: self._set_focus(True))
        c.bind("<FocusOut>", lambda e: self._set_focus(False))

    # ---------- 兼容 Treeview 的自动化接口 ----------
    def set_rows(self, rows: Sequence[dict], known_ids: Optional[set] = None) -> None:
        rows = list(rows)
        self._known = set(known_ids) if known_ids is not None else {row["id"] for row in rows}
        if rows != self._rows:
            self._rows = rows
            self._draw()

    def exists(self, iid: str) -> bool:
        return iid in self._known

    def get_children(self) -> tuple:
        return tuple(row["id"] for row in self._rows)

    def selection(self) -> tuple:
        return (self._selected,) if self._selected else ()

    def selection_set(self, iid: str) -> None:
        if iid != self._selected:
            self._selected = iid
            self._draw()
            if self._on_select:
                self._on_select(iid)

    def see(self, iid: str) -> None:
        index = self._index_of(iid)
        if index < 0:
            return
        top = self.HEADER_H * self.th.scale + index * self.ROW_H * self.th.scale
        bottom = top + self.ROW_H * self.th.scale
        total = max(1, self._content_height())
        view_top = self.canvas.canvasy(0)
        view_h = self.canvas.winfo_height()
        if top < view_top:
            self.canvas.yview_moveto(top / total)
        elif bottom > view_top + view_h:
            self.canvas.yview_moveto((bottom - view_h) / total)
        self._draw()

    def focus_set(self) -> None:
        self.canvas.focus_set()

    def apply_theme(self, bg: Optional[str] = None) -> None:
        bg = bg or _parent_bg(self) or self._bg
        self._bg = bg
        self.configure(bg=bg)
        self.canvas.configure(bg=bg)
        self.painter = painter_for(self)
        self._draw()

    # ---------- 内部 ----------
    def _index_of(self, iid: str) -> int:
        for index, row in enumerate(self._rows):
            if row["id"] == iid:
                return index
        return -1

    def _content_height(self) -> int:
        return int((self.HEADER_H + len(self._rows) * self.ROW_H) * self.th.scale)

    def _row_at(self, y_canvas: float) -> int:
        i = int((y_canvas - self.HEADER_H * self.th.scale) // (self.ROW_H * self.th.scale))
        return i if 0 <= i < len(self._rows) else -1

    def _set_hover(self, i: int) -> None:
        if i != self._hover:
            self._hover = i
            self.canvas.configure(cursor="hand2" if i >= 0 else "arrow")
            self._draw()

    def _set_focus(self, focused: bool) -> None:
        self._focused = focused
        self._draw()

    def _on_motion(self, event) -> None:
        self._set_hover(self._row_at(self.canvas.canvasy(event.y)))

    def _on_click(self, event) -> None:
        self.canvas.focus_set()
        i = self._row_at(self.canvas.canvasy(event.y))
        if i >= 0:
            self.selection_set(self._rows[i]["id"])

    def _on_double(self, event) -> None:
        i = self._row_at(self.canvas.canvasy(event.y))
        if i >= 0:
            self.selection_set(self._rows[i]["id"])
            self._activate()

    def _activate(self) -> str:
        if self._selected and self._on_activate:
            self._on_activate(self._selected)
        return "break"

    def _move(self, delta: int) -> str:
        if not self._rows:
            return "break"
        index = self._index_of(self._selected)
        index = max(0, min(len(self._rows) - 1, (index if index >= 0 else -1 if delta > 0 else len(self._rows)) + delta))
        self.selection_set(self._rows[index]["id"])
        self.see(self._rows[index]["id"])
        return "break"

    def _on_wheel(self, event) -> str:
        if self._content_height() > self.canvas.winfo_height():
            self.canvas.yview_scroll(-int(event.delta / 120) * 3, "units")
            self._draw()
        return "break"

    def _draw(self) -> None:
        c, th = self.canvas, self.th
        c.delete("all")
        w = c.winfo_width()
        if w <= 1:
            return
        c.configure(scrollregion=(0, 0, w, max(self._content_height(), c.winfo_height())))
        pad = th.px(T.SPACE_LG)
        pill_w = th.px(104)
        name_w = int((w - pad * 2 - pill_w) * 0.48)
        point_x = pad + name_w + th.px(16)
        point_w = max(th.px(80), w - point_x - pill_w - pad)
        header_y = th.px(self.HEADER_H) / 2
        for text, x in (("名称 · Remote", pad), ("挂载位置", point_x), ("状态", w - pad - pill_w + th.px(10))):
            c.create_text(x, header_y, anchor="w", text=text, font=th.font(T.FONT_LABEL), fill=T.TEXT_TERTIARY)
        row_h = th.px(self.ROW_H)
        r = th.px(T.RADIUS_CONTROL)
        for i, row in enumerate(self._rows):
            y = th.px(self.HEADER_H) + i * row_h
            selected = row["id"] == self._selected
            if selected or i == self._hover:
                c.create_image(th.px(4), y + th.px(2), anchor="nw",
                               image=self.painter.shape(w - th.px(8), row_h - th.px(4), r, T.ACCENT_SUBTLE if selected else T.PANEL_HOVER))
            if selected:
                bar_h = th.px(20)
                c.create_image(th.px(4), y + (row_h - bar_h) / 2, anchor="nw", image=self.painter.shape(th.px(3), bar_h, th.px(1.5), T.ACCENT))
                if self._focused:
                    c.create_image(th.px(4), y + th.px(2), anchor="nw",
                                   image=self.painter.focus_ring(w - th.px(8), row_h - th.px(4), r, th.px(1), _focus_color()))
            name = th.elide(row["name"], name_w, T.FONT_BODY, "semibold")
            c.create_text(pad, y + th.px(19), anchor="w", text=name, font=th.font(T.FONT_BODY, "semibold"),
                          fill=T.ACCENT_TEXT if selected else T.TEXT)
            c.create_text(pad, y + th.px(38), anchor="w", text=th.elide(row["remote"], name_w, T.FONT_CAPTION),
                          font=th.font(T.FONT_CAPTION), fill=T.TEXT_SECONDARY)
            c.create_text(point_x, y + row_h / 2, anchor="w", text=th.elide(row["point"], point_w, T.FONT_CAPTION),
                          font=th.font(T.FONT_CAPTION), fill=T.TEXT_SECONDARY)
            fg_token, soft_token = TONES.get(row.get("tone", "idle"), TONES["idle"])
            fg, soft = T.color(fg_token), T.color(soft_token)
            ph = th.px(22)
            pw = th.measure(row["state"], T.FONT_LABEL, "semibold") + th.px(30)
            px0 = w - pad - pw
            c.create_image(px0, y + (row_h - ph) / 2, anchor="nw", image=self.painter.shape(pw, ph, ph / 2, soft))
            d = th.px(6)
            c.create_image(px0 + th.px(10), y + (row_h - d) / 2, anchor="nw", image=self.painter.shape(d, d, d / 2, fg))
            c.create_text(px0 + th.px(10) + d + th.px(6), y + row_h / 2, anchor="w", text=row["state"],
                          font=th.font(T.FONT_LABEL, "semibold"), fill=fg)
        first, last = c.yview()
        if last - first < 0.999:
            h = c.winfo_height()
            bar_h = max(th.px(32), int(h * (last - first)))
            x = w - th.px(4)
            c.create_line(x, c.canvasy(int(h * first)), x, c.canvasy(int(h * first) + bar_h),
                          fill=T.SCROLLBAR, width=th.px(3), capstyle="round")
