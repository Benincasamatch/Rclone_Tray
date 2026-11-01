"""抗锯齿绘制：PIL 生成圆角形状，Tk Canvas 作为显示表面。

规范要求极细描边 + 柔和阴影，而 Tk Canvas 没有抗锯齿圆角；这里用 PIL 超采样
生成位图，并按 (尺寸, 圆角, 填充, 描边, 阴影) 缓存为 PhotoImage。
"""
from __future__ import annotations

from typing import Optional

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageTk

from .theme import Theme

_SS = 4  # 圆角超采样倍数


def _quarter_disc(radius: int, ss: int = _SS) -> Image.Image:
    """半径 radius 的 1/4 圆盘（左上象限），边缘已抗锯齿。"""
    r = max(1, radius)
    big = Image.new("L", (r * 2 * ss, r * 2 * ss), 0)
    ImageDraw.Draw(big).ellipse([0, 0, r * 2 * ss - 1, r * 2 * ss - 1], fill=255)
    return big.crop((0, 0, r * ss, r * ss)).resize((r, r), Image.LANCZOS)


def rounded_mask(w: int, h: int, radius: int) -> Image.Image:
    """圆角矩形 alpha 蒙版，四角抗锯齿，中段纯色。"""
    if w <= 0 or h <= 0:
        return Image.new("L", (max(w, 1), max(h, 1)), 0)
    r = max(0, min(radius, w // 2, h // 2))
    mask = Image.new("L", (w, h), 255)
    if r == 0:
        return mask
    tile = _quarter_disc(r)
    mask.paste(tile, (0, 0))
    mask.paste(tile.transpose(Image.FLIP_LEFT_RIGHT), (w - r, 0))
    mask.paste(tile.transpose(Image.FLIP_TOP_BOTTOM), (0, h - r))
    mask.paste(tile.transpose(Image.ROTATE_180), (w - r, h - r))
    return mask


def _alpha(hex_color: str, alpha: int = 255) -> int:
    """取颜色的最终 alpha（#RRGGBBAA 的 alpha 与入参相乘）。"""
    value = hex_color.lstrip("#")
    if len(value) == 8:
        return alpha * int(value[6:8], 16) // 255
    return alpha


def _color(hex_color: str, alpha: int = 255) -> tuple:
    """支持 #RGB / #RRGGBB / #RRGGBBAA；8 位写法中的 alpha 与入参相乘。"""
    value = hex_color.lstrip("#")
    if len(value) == 3:
        value = "".join(c * 2 for c in value)
    return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16),
            _alpha(hex_color, alpha))


def rgba_hex(rgba: tuple) -> str:
    """(r, g, b, alpha 0-1) → ``#RRGGBBAA``，供需要 8 位颜色的绘制接口使用。"""
    r, g, b, a = rgba
    return "#{:02X}{:02X}{:02X}{:02X}".format(r, g, b, int(round(a * 255)))


class Painter:
    """位图工厂：把设计令牌渲染成可复用的 PhotoImage。"""

    def __init__(self, theme: Theme, master) -> None:
        self.theme = theme
        self.master = master
        self._cache: dict[tuple, ImageTk.PhotoImage] = {}

    def shape(self, w: int, h: int, radius: float, fill: str,
              border: Optional[str] = None, border_width: float = 0,
              fill_alpha: int = 255) -> ImageTk.PhotoImage:
        """圆角矩形（radius 取 h/2 即胶囊）。返回可直接放到 Canvas 的图片。"""
        w, h = max(1, int(w)), max(1, int(h))
        radius = int(round(radius))
        bw = int(round(border_width))
        key = ("shape", w, h, radius, fill, border, bw, fill_alpha)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        img = self._composite_shape(w, h, radius, fill, border, bw, fill_alpha)
        photo = ImageTk.PhotoImage(img, master=self.master)
        self._cache[key] = photo
        return photo

    def _composite_shape(self, w, h, radius, fill, border, bw,
                         fill_alpha: int = 255) -> Image.Image:
        mask = rounded_mask(w, h, radius)
        img = Image.new("RGBA", (w, h), _color(fill))
        img.putalpha(mask.point(lambda v: v * _alpha(fill, fill_alpha) // 255))
        if border and bw > 0:
            inner = Image.new("L", (w, h), 0)
            inner.paste(rounded_mask(max(1, w - 2 * bw), max(1, h - 2 * bw),
                                     max(0, radius - bw)), (bw, bw))
            ring_img = Image.new("RGBA", (w, h), _color(border))
            ring_img.putalpha(ImageChops.subtract(mask, inner).point(
                lambda v: v * _alpha(border) // 255))
            img = Image.alpha_composite(img, ring_img)
        return img

    def elevated(self, w: int, h: int, radius: float, fill: str,
                 border: Optional[str] = None, blur: float = 6,
                 dy: float = 2, opacity: int = 26,
                 ) -> tuple[ImageTk.PhotoImage, int]:
        """带柔和投影的浮层（规范 §3.3：小范围、低透明度、大模糊半径）。

        返回 ``(图片, 边距)``；图片四周已含 ``边距`` 像素留白，贴在画布 ``(0, 0)`` 即可。
        """
        w, h = max(1, int(w)), max(1, int(h))
        radius = int(round(radius))
        blur = max(1.0, blur)
        pad = int(round(blur * 1.5 + abs(dy))) + 1
        key = ("elevated", w, h, radius, fill, border, blur, dy, opacity, pad)
        cached = self._cache.get(key)
        if cached is not None:
            return cached, pad
        total_w, total_h = w + pad * 2, h + pad * 2
        shadow = Image.new("L", (total_w, total_h), 0)
        shadow.paste(rounded_mask(w, h, radius), (pad, pad))
        shadow = shadow.filter(ImageFilter.GaussianBlur(blur))
        if dy:
            shifted = Image.new("L", (total_w, total_h), 0)
            shifted.paste(shadow, (0, int(round(dy))))
            shadow = shifted
        shadow = shadow.point(lambda v: v * opacity // 255)
        canvas = Image.new("RGBA", (total_w, total_h), (0, 0, 0, 0))
        dark = Image.new("RGBA", (total_w, total_h), _color("#000000"))
        dark.putalpha(shadow)
        canvas = Image.alpha_composite(canvas, dark)
        body = self._composite_shape(w, h, radius, fill, border, 1)
        canvas.alpha_composite(body, (pad, pad))
        photo = ImageTk.PhotoImage(canvas, master=self.master)
        self._cache[key] = photo
        return photo, pad

    def focus_ring(self, w: int, h: int, radius: float, thickness: int = 2,
                   color: str = "#A78BFA") -> ImageTk.PhotoImage:
        """焦点框：仅描边（内部透明），可直接叠在控件上。"""
        w, h = max(2, int(w)), max(2, int(h))
        radius = int(round(radius))
        t = max(1, int(thickness))
        key = ("focusring", w, h, radius, t, color)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        outer = rounded_mask(w, h, radius)
        inner = Image.new("L", (w, h), 0)
        inner.paste(rounded_mask(max(1, w - 2 * t), max(1, h - 2 * t),
                                 max(0, radius - t)), (t, t))
        ring = ImageChops.subtract(outer, inner)
        img = Image.new("RGBA", (w, h), _color(color))
        img.putalpha(ring.point(lambda v: v * _alpha(color) // 255))
        photo = ImageTk.PhotoImage(img, master=self.master)
        self._cache[key] = photo
        return photo

    def switch_track(self, w: int, h: int, on: bool, hover: bool) -> ImageTk.PhotoImage:
        """开关轨道：开=强调色实心，关=深底 + 描边。"""
        from . import theme as T

        if on:
            return self.shape(w, h, h / 2, T.ACCENT_HOVER if hover else T.ACCENT)
        return self.shape(w, h, h / 2, T.CONTROL_HOVER if hover else T.SWITCH_OFF_TRACK,
                          T.SWITCH_OFF_STROKE, 1)

    @staticmethod
    def knob_pad(d: int) -> int:
        """旋钮位图四周的留白（放置时需减去）。"""
        return 1

    def knob(self, d: int, on: bool = True, hover: bool = False) -> ImageTk.PhotoImage:
        """开关旋钮：圆形；开=强调色上的深色，关=次级文字色。"""
        from . import theme as T

        d = max(2, int(d))
        fill = T.TEXT_ON_ACCENT if on else (T.TEXT if hover else T.TEXT_SECONDARY)
        key = ("knob", d, fill)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        pad = self.knob_pad(d)
        size = d + pad * 2
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        body = Image.new("RGBA", (d, d), _color(fill))
        body.putalpha(rounded_mask(d, d, d // 2))
        img.alpha_composite(body, (pad, pad))
        photo = ImageTk.PhotoImage(img, master=self.master)
        self._cache[key] = photo
        return photo
