"""应用标识（app mark）：程序化绘制的圆角方形渐变底 + 单色云形前景。

项目自带的 ``assets/icon.ico`` 仅 32×32 纯色，放大后发虚；这里按任意尺寸绘制，
并保留 ico 作为 exe/窗口图标。渐变取规范强调色（#A78BFA 家族）。
"""
from __future__ import annotations

from functools import lru_cache

from PIL import Image, ImageDraw

_SS = 4  # 超采样倍数

# 强调色渐变：顶部为强调色悬浮态，底部为按下态
_TOP = (0xB9, 0xA5, 0xFF)
_BOTTOM = (0x8B, 0x72, 0xD6)


def _lerp(a: tuple, b: tuple, t: float) -> tuple:
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def _squircle_mask(size: int, radius_ratio: float = 0.18) -> Image.Image:
    """圆角方形蒙版（Win11 图标为 18% 圆角，非 macOS 的连续曲率）。"""
    big = size * _SS
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, big - 1, big - 1], radius=int(big * radius_ratio), fill=255
    )
    return mask.resize((size, size), Image.LANCZOS)


def _cloud_mask(size: int) -> Image.Image:
    """云形前景：三瓣 + 平底（Win11 系统图标的云是平底，不做圆角底座）。"""
    big = size * _SS
    layer = Image.new("L", (big, big), 0)
    draw = ImageDraw.Draw(layer)
    w = big
    # 底座：只圆上方两角，下方保持平直
    draw.rounded_rectangle(
        [w * 0.255, w * 0.470, w * 0.745, w * 0.700],
        radius=int(w * 0.10), fill=255,
    )
    for cx, cy, r in (
        (0.360, 0.520, 0.098),
        (0.500, 0.455, 0.132),
        (0.640, 0.520, 0.098),
    ):
        draw.ellipse(
            [w * (cx - r), w * (cy - r), w * (cx + r), w * (cy + r)], fill=255
        )
    # 抹平底边
    cut = int(big * 0.700)
    draw.rectangle([0, cut, big, big], fill=0)
    return layer.resize((size, size), Image.LANCZOS)


@lru_cache(maxsize=16)
def app_mark(size: int = 64) -> Image.Image:
    """返回 size×size 的 RGBA 应用图标。"""
    size = max(8, int(size))
    body = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    gradient = Image.new("RGB", (size, size))
    pixels = gradient.load()
    for y in range(size):
        color = _lerp(_TOP, _BOTTOM, y / max(1, size - 1))
        for x in range(size):
            pixels[x, y] = color
    body.paste(gradient, (0, 0), _squircle_mask(size))

    cloud = Image.new("RGBA", (size, size), (255, 255, 255, 255))
    cloud.putalpha(_cloud_mask(size))
    body = Image.alpha_composite(body, cloud)
    return body
