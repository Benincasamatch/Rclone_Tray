"""模拟玻璃：应用内环境渐变背景 + 半透明面板。

Tkinter 没有 CSS ``backdrop-filter``，无法对单个控件做真实背景模糊。这里按
规范 §3.1/§3.4 的做法：窗口底层是一张极弱的环境光渐变（左边偏蓝、右边偏紫、
中心深色），每个 ``GlassPanel`` 按自身在**最近的外层表面**中的位置裁剪其位图，
再叠加 RGBA 面板令牌与 0.06 白描边，得到「隐约透出背后内容」的玻璃层次；
面板嵌套即按层叠加。

子控件无法透明，所以表面渲染后按子控件实际中心位置采样像素作为其底色；
渐变幅度很小，采样误差低于 1 级，文字区域与面板颜色一致。

重绘由 Backdrop 统一调度：所有请求合并为一次自顶向下的子树遍历（父表面先于
子表面各渲染一次），避免尺寸变化风暴中按旧位图裁剪或重复渲染。
"""
from __future__ import annotations

import time
import tkinter as tk
from typing import Optional

from PIL import Image, ImageChops, ImageTk

from . import theme as T
from .paint import rounded_mask

_AMBIENT_CACHE: dict = {}
PASS_DELAY_MS = 20
PASS_MAX_WAIT_MS = 80


def ambient_image(w: int, h: int, dark: bool) -> Image.Image:
    """生成 w×h 的环境光渐变（低分辨率构建后放大，避免逐像素 Python 开销）。"""
    w, h = max(2, int(w)), max(2, int(h))
    key = (w, h, dark)
    cached = _AMBIENT_CACHE.get(key)
    if cached is not None:
        return cached
    spec = T.AMBIENT_DARK if dark else T.AMBIENT_LIGHT
    scale = 6
    lw, lh = max(8, w // scale), max(8, h // scale)
    small = Image.new("RGB", (lw, lh), spec["base"])
    for rx, ry, rr, color, alpha in spec["glows"]:
        d = max(6, int(min(lw, lh) * rr * 2))
        gradient = Image.radial_gradient("L").resize((d, d), Image.BILINEAR)
        # radial_gradient 中心为 0：取其反相得到中心最亮的 alpha 蒙版
        layer = ImageChops.invert(gradient).point(lambda v: int(v * alpha))
        mask = Image.new("L", (lw, lh), 0)
        mask.paste(layer, (int(lw * rx) - d // 2, int(lh * ry) - d // 2))
        small.paste(Image.new("RGB", (lw, lh), color), (0, 0), mask)
    image = small.resize((w, h), Image.BICUBIC) if (lw, lh) != (w, h) else small
    if len(_AMBIENT_CACHE) > 8:
        _AMBIENT_CACHE.clear()
    _AMBIENT_CACHE[key] = image
    return image


def backdrop_for(widget: tk.Misc) -> Optional["Backdrop"]:
    """取 widget 所在顶层窗口的 Backdrop（没有则返回 None）。"""
    return getattr(widget.winfo_toplevel(), "_backdrop", None)


def surface_for(widget: tk.Misc) -> Optional["_Surface"]:
    """widget 自身（若是表面）或最近的外层玻璃表面。"""
    node = widget
    while node is not None:
        if isinstance(node, _Surface):
            return node
        node = getattr(node, "master", None)
    return None


def surface_fill(widget: tk.Misc) -> str:
    """最近的玻璃表面的不透明等价色，供新建子控件作初始底色。"""
    surface = surface_for(widget)
    return surface.fill if surface is not None else T.BASE


def rerender_within(widget: tk.Misc) -> None:
    """重绘 widget 子树内的玻璃表面（滚动或局部布局变化后调用）。"""
    if isinstance(widget, _Surface):
        widget.request_render()
        return
    for child in _nested_surfaces(widget):
        child.request_render()


def _nested_surfaces(widget: tk.Misc):
    """widget 之下第一层的玻璃表面（不再向其内部深入），按创建顺序。"""
    stack = list(reversed(widget.winfo_children()))
    while stack:
        child = stack.pop()
        if isinstance(child, _Surface):
            yield child
        else:
            stack.extend(reversed(child.winfo_children()))


def _is_within(widget: tk.Misc, ancestor: tk.Misc) -> bool:
    node = getattr(widget, "master", None)
    while node is not None:
        if node is ancestor:
            return True
        node = getattr(node, "master", None)
    return False


def render_subtree(surface: "_Surface") -> None:
    """自顶向下渲染 surface 及其内部所有可见表面，各一次。"""
    try:
        if not surface.winfo_exists() or not surface.winfo_viewable():
            return
    except tk.TclError:
        return
    surface.render()
    for child in _nested_surfaces(surface):
        render_subtree(child)


class _Surface(tk.Canvas):
    """带可采样位图的画布：子控件底色按其中心位置的像素校正。"""

    fill: str

    def __init__(self, master: tk.Misc, **kw):
        self._sample: Optional[Image.Image] = None
        self._sample_origin = (0, 0)
        self._recolor_job = None
        self._recolor_retries = 0
        self._render_job = None
        super().__init__(master, highlightthickness=0, bd=0, takefocus=0, **kw)
        # 由 withdraw 状态显示的窗口：MapNotify 异步到达，映射后再渲染一次
        self.bind("<Map>", self._on_map, add="+")
        self.bind("<Destroy>", self._on_destroy, add="+")

    # ---------- 调度 ----------
    def request_render(self) -> None:
        """请求重绘本表面子树；由 Backdrop 合并为一次自顶向下遍历。"""
        backdrop = backdrop_for(self)
        if backdrop is not None:
            backdrop.request_pass(self)
        elif self._render_job is None:
            self._render_job = self.after(PASS_DELAY_MS, self._run_render)

    def _run_render(self) -> None:
        self._render_job = None
        render_subtree(self)

    def _on_map(self, _event) -> None:
        # 隐藏页面内的控件仍带有 mapped 标志，pack 重排会反复触发其 <Map>；只响应真正可见的
        if not self.winfo_viewable():
            return
        self.request_render()
        # 子控件的 MapNotify 在本事件之后才派发；稍后再校正一次底色
        self._schedule_recolor(delay=32)

    def _on_destroy(self, event) -> None:
        if event.widget is not self:
            return
        for job in (self._recolor_job, self._render_job):
            if job is not None:
                try:
                    self.after_cancel(job)
                except tk.TclError:
                    pass
        self._recolor_job = self._render_job = None

    # ---------- 采样 ----------
    def sample(self, x: int, y: int) -> str:
        """表面内 (x, y) 处的颜色；越界时返回 fill。"""
        image = self._sample
        if image is not None:
            x -= self._sample_origin[0]
            y -= self._sample_origin[1]
            if 0 <= x < image.width and 0 <= y < image.height:
                r, g, b = image.getpixel((x, y))[:3]
                return "#{:02X}{:02X}{:02X}".format(r, g, b)
        return self.fill

    def crop_for(self, child: tk.Misc, w: int, h: int, base: tuple) -> Image.Image:
        """child（位于本表面之上）所占区域的位图；超出部分用 base 补齐。"""
        image = self._sample
        if image is None:
            return Image.new("RGB", (w, h), base)
        x = child.winfo_rootx() - self.winfo_rootx() - self._sample_origin[0]
        y = child.winfo_rooty() - self.winfo_rooty() - self._sample_origin[1]
        left, top = max(0, x), max(0, y)
        right, bottom = min(image.width, x + w), min(image.height, y + h)
        if left >= right or top >= bottom:
            return Image.new("RGB", (w, h), base)
        if (left, top, right, bottom) == (x, y, x + w, y + h):
            return image.crop((x, y, x + w, y + h)).convert("RGB")
        crop = Image.new("RGB", (w, h), base)
        crop.paste(image.crop((left, top, right, bottom)).convert("RGB"), (left - x, top - y))
        return crop

    # ---------- 子控件底色 ----------
    def _schedule_recolor(self, delay: int = 0) -> None:
        if self._recolor_job is None:
            self._recolor_job = self.after(delay, self._recolor) if delay else self.after_idle(self._recolor)

    def _awaiting_map(self, widget) -> bool:
        """widget 自身未映射、但到本表面为止的祖先都已映射：它的 MapNotify 仍在队列里。"""
        if not widget.winfo_manager() or widget.winfo_ismapped():
            return False
        node = widget.master
        while node is not None and node is not self:
            if not node.winfo_ismapped():
                return False        # 处于隐藏子树（如未显示的页面），不会很快映射
            node = node.master
        return True

    def _recolor(self) -> None:
        self._recolor_job = None
        if self._sample is None:
            return
        try:
            if not self.winfo_exists() or not self.winfo_viewable():
                return
        except tk.TclError:
            return
        pending = False
        stack = list(self.winfo_children())
        while stack:
            widget = stack.pop()
            if isinstance(widget, _Surface):
                continue
            try:
                if self._awaiting_map(widget):
                    pending = True
                    continue
                if not widget.winfo_ismapped():
                    continue
            except tk.TclError:
                continue
            if self._apply_fill(widget):
                stack.extend(widget.winfo_children())
        if pending and self._recolor_retries < 8:
            self._recolor_retries += 1
            self._recolor_job = self.after(16, self._recolor)
        else:
            self._recolor_retries = 0

    def _apply_fill(self, widget) -> bool:
        """校正 widget 底色；返回是否需要继续深入其子控件（复合组件自行处理子树）。"""
        try:
            w, h = widget.winfo_width(), widget.winfo_height()
            x = widget.winfo_rootx() + w // 2 - self.winfo_rootx()
            y = widget.winfo_rooty() + h // 2 - self.winfo_rooty()
            fill = self.sample(x, y)
            apply = getattr(widget, "apply_theme", None)
            if callable(apply):
                apply(bg=fill)
                return False
            widget.configure(bg=fill)
            return True
        except (tk.TclError, AttributeError):
            return False

    def render(self) -> None:  # pragma: no cover - 子类实现
        raise NotImplementedError


class Backdrop(_Surface):
    """窗口底层：绘制环境光渐变，并统一调度其上所有玻璃面板的重绘。"""

    def __init__(self, master: tk.Misc):
        self.fill = T.BASE
        super().__init__(master, bg=T.BASE)
        self.th = T.theme(master)
        self._photo: Optional[ImageTk.PhotoImage] = None
        self._size = (0, 0)
        self._pass_job = None
        self._pass_roots: list = []
        self._pass_since = 0.0
        master.winfo_toplevel()._backdrop = self
        self.bind("<Configure>", lambda e: self.request_pass(self), add="+")

    def request_pass(self, root_surface: Optional[_Surface] = None) -> None:
        """登记需要重绘的子树根；尾随防抖合并一次布局风暴，最长推迟 PASS_MAX_WAIT_MS。

        Tk 的 pack/grid 布局以多个 idle 回调逐层完成，深层表面的 <Configure>
        会在之后的若干轮才到达；每次请求都重置短定时器，风暴结束后只渲染一遍。
        """
        root_surface = root_surface or self
        if root_surface not in self._pass_roots:
            self._pass_roots.append(root_surface)
        now = time.monotonic()
        if self._pass_job is not None:
            if now - self._pass_since < PASS_MAX_WAIT_MS / 1000:
                try:
                    self.after_cancel(self._pass_job)
                except tk.TclError:
                    pass
                self._pass_job = self.after(PASS_DELAY_MS, self._run_pass)
            return
        self._pass_since = now
        self._pass_job = self.after(PASS_DELAY_MS, self._run_pass)

    def _run_pass(self) -> None:
        self._pass_job = None
        roots, self._pass_roots = self._pass_roots, []
        if self in roots:
            roots = [self]
        else:
            roots = [r for r in roots if not any(o is not r and _is_within(r, o) for o in roots)]
        for root_surface in roots:
            render_subtree(root_surface)

    def _on_destroy(self, event) -> None:
        super()._on_destroy(event)
        if event.widget is self and self._pass_job is not None:
            try:
                self.after_cancel(self._pass_job)
            except tk.TclError:
                pass
            self._pass_job = None

    def render(self) -> None:
        w, h = self.winfo_width(), self.winfo_height()
        if w <= 1 or h <= 1:
            return
        if (w, h) != self._size:
            self._size = (w, h)
            self._sample = ambient_image(w, h, T.is_dark())
            self._photo = ImageTk.PhotoImage(self._sample, master=self)
            self.delete("ambient")
            self.create_image(0, 0, anchor="nw", image=self._photo, tags="ambient")
            self.tag_lower("ambient")
        self._schedule_recolor()

    def rerender(self) -> None:
        """布局变化（位置变了但尺寸没变）后重绘全部面板并校正子控件底色。"""
        self.request_pass(self)

    def apply_theme(self, bg: Optional[str] = None) -> None:
        """主题切换：重建渐变并立即自顶向下重绘。"""
        self.fill = T.BASE
        self.configure(bg=T.BASE)
        self._size = (0, 0)
        render_subtree(self)


class GlassPanel(_Surface):
    """半透明面板：裁剪外层表面并叠加 RGBA 令牌，子控件直接 pack/grid 进来。

    ``surface`` 取 ``ui.theme`` 玻璃令牌名（SIDEBAR/PANEL/FLOAT/MENU/TOOLTIP），
    或 ``CLEAR`` 表示完全透明（布局容器、滚动页面主体）。
    """

    def __init__(self, master: tk.Misc, surface: str = "PANEL",
                 radius: float = T.RADIUS_CARD, border: bool = True, **kw):
        self.th = T.theme(master)
        self.parent_surface = surface_for(master)
        self.surface = surface
        self.radius = 0 if surface == "CLEAR" else radius
        self._border = border and surface != "CLEAR"
        if surface == "CLEAR":
            self.fill = self.parent_surface.fill if self.parent_surface is not None else T.BASE
        else:
            self.fill = T.color(surface)
        self._photo: Optional[ImageTk.PhotoImage] = None
        super().__init__(master, bg=self.fill, **kw)
        self.bind("<Configure>", self._on_configure, add="+")

    def _on_configure(self, _event) -> None:
        if self.winfo_viewable():       # 隐藏页面的表面等 <Map> 再渲染
            self.request_render()

    def _rgba(self) -> tuple:
        if self.surface == "CLEAR":
            return (0, 0, 0, 0.0)
        return T.glass()[self.surface]

    def _visible_region(self, w: int, h: int) -> tuple[int, int, int, int]:
        """透明容器只渲染与外层表面相交的部分（滚动主体可能高达数千像素）。"""
        parent = self.parent_surface
        if parent is None:
            return 0, 0, w, h
        try:
            px = parent.winfo_rootx() - self.winfo_rootx()
            py = parent.winfo_rooty() - self.winfo_rooty()
            pw, ph = parent.winfo_width(), parent.winfo_height()
        except tk.TclError:
            return 0, 0, w, h
        left, top = max(0, px), max(0, py)
        right, bottom = min(w, px + pw), min(h, py + ph)
        if left >= right or top >= bottom:
            return 0, 0, 0, 0
        return left, top, right - left, bottom - top

    def render(self) -> None:
        w, h = self.winfo_width(), self.winfo_height()
        if w <= 1 or h <= 1 or not self.winfo_viewable():
            return                      # 隐藏页面的表面在 <Map> 时再渲染
        r, g, b, a = self._rgba()
        base = T.hex_to_rgb(T.BASE)
        parent = self.parent_surface
        self.fill = (parent.fill if parent is not None else T.BASE) if a <= 0 else T.blend((r, g, b, a), base)
        if a <= 0:
            ox, oy, vw, vh = self._visible_region(w, h)
            if vw <= 0 or vh <= 0:
                return
            if parent is not None:
                comp = parent.crop_for(self, w, h, base).crop((ox, oy, ox + vw, oy + vh))
            else:
                comp = Image.new("RGB", (vw, vh), base)
            self._sample_origin = (ox, oy)
        else:
            crop = (parent.crop_for(self, w, h, base) if parent is not None
                    else Image.new("RGB", (w, h), base)).convert("RGBA")
            radius = int(round(self.radius * self.th.scale))
            mask = rounded_mask(w, h, radius)
            tint = Image.new("RGBA", (w, h), (r, g, b, int(round(a * 255))))
            comp = Image.composite(Image.alpha_composite(crop, tint), crop, mask)
            if self._border:
                br, bg, bb, ba = T.glass()["BORDER"]
                inner = Image.new("L", (w, h), 0)
                inner.paste(rounded_mask(max(1, w - 2), max(1, h - 2), max(0, radius - 1)), (1, 1))
                ring = ImageChops.subtract(mask, inner)
                border_layer = Image.new("RGBA", (w, h), (br, bg, bb, 0))
                border_layer.putalpha(ring.point(lambda v: v * int(round(ba * 255)) // 255))
                comp = Image.alpha_composite(comp, border_layer)
            self._sample_origin = (0, 0)
        self._sample = comp
        self._photo = ImageTk.PhotoImage(comp, master=self)
        self.delete("glass")
        self.create_image(self.canvasx(0) + self._sample_origin[0], self.canvasy(0) + self._sample_origin[1],
                          anchor="nw", image=self._photo, tags="glass")
        self.tag_lower("glass")
        self._schedule_recolor()

    def apply_theme(self, bg: Optional[str] = None) -> None:
        """外层表面重绘或主题切换：请求重绘自身子树。"""
        self.request_render()


class Clear(GlassPanel):
    """完全透明的布局容器：直接放在背景上时露出渐变，而不是一块实色矩形。"""

    def __init__(self, master: tk.Misc, **kw):
        super().__init__(master, surface="CLEAR", **kw)
