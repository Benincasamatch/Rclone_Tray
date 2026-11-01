"""动效：缓动曲线与基于 ``after`` 的帧驱动（规范 §7）。

- 时长 100–250ms，Ease-out / Ease-in-out；只动画颜色、位置、透明度。
- 新动画打断旧动画时由调用方从当前值继续，避免跳变。
- 「减少动态效果」开启后所有过渡立即完成。
"""
from __future__ import annotations

import tkinter as tk
from typing import Callable, Optional

# 活动动画注册表：减少动态效果切换时统一结束
_LIVE: list = []
_REDUCE_MOTION = False
FRAME_MS = 16


def set_reduce_motion(enabled: bool) -> None:
    """开启时立即完成所有进行中的过渡。"""
    global _REDUCE_MOTION
    if _REDUCE_MOTION == bool(enabled):
        return
    _REDUCE_MOTION = bool(enabled)
    if _REDUCE_MOTION:
        for anim in list(_LIVE):
            anim.cancel()
            if anim._on_frame:
                anim._on_frame(1.0)
            if anim._on_done:
                callback, anim._on_done = anim._on_done, None
                callback()


def reduce_motion() -> bool:
    return _REDUCE_MOTION


def ease_out_cubic(t: float) -> float:
    t = min(1.0, max(0.0, t))
    return 1.0 - (1.0 - t) ** 3


def ease_in_out_cubic(t: float) -> float:
    t = min(1.0, max(0.0, t))
    return 4 * t ** 3 if t < 0.5 else 1 - (-2 * t + 2) ** 3 / 2


def lerp_color(c1: str, c2: str, t: float) -> str:
    """在两个 ``#RRGGBB`` 之间插值，返回十六进制串。"""
    t = min(1.0, max(0.0, t))
    a = c1.lstrip("#")
    b = c2.lstrip("#")
    out = []
    for i in (0, 2, 4):
        av, bv = int(a[i:i + 2], 16), int(b[i:i + 2], 16)
        out.append(int(round(av + (bv - av) * t)))
    return "#{:02X}{:02X}{:02X}".format(*out)


class Animation:
    """单个可打断的属性动画；``on_frame(t)`` 收到 0→1 的缓动后进度。"""

    def __init__(self, widget: tk.Misc, duration: int = 160,
                 easing: Callable[[float], float] = ease_out_cubic):
        self.widget = widget
        self.duration = max(1, duration)
        self.easing = easing
        self._job: Optional[str] = None
        self._on_frame: Optional[Callable[[float], None]] = None
        self._on_done: Optional[Callable[[], None]] = None
        try:
            widget.bind("<Destroy>", self._on_destroy, add="+")
        except Exception:
            pass

    def start(self, on_frame: Optional[Callable[[float], None]] = None,
              on_done: Optional[Callable[[], None]] = None,
              duration: Optional[int] = None) -> None:
        """播放一次 0→1；每次从 0 开始，续播由调用方用当前值插值。"""
        if on_frame is not None:
            self._on_frame = on_frame
        self._on_done = on_done
        self.cancel()
        if _REDUCE_MOTION:
            if self._on_frame:
                self._on_frame(1.0)
            if self._on_done:
                callback, self._on_done = self._on_done, None
                callback()
            return
        _LIVE.append(self)
        total = max(1, duration or self.duration)
        steps = max(1, int(round(total / FRAME_MS)))
        step_ms = max(8, total // steps)

        def tick(i: int) -> None:
            try:
                if not self.widget.winfo_exists():
                    self._job = None
                    return
            except Exception:
                self._job = None
                return
            progress = min(1.0, i / steps)
            if self._on_frame:
                self._on_frame(self.easing(progress))
            if i >= steps:
                self._job = None
                try:
                    _LIVE.remove(self)
                except ValueError:
                    pass
                if self._on_done:
                    callback, self._on_done = self._on_done, None
                    callback()
                return
            try:
                self._job = self.widget.after(step_ms, tick, i + 1)
            except Exception:
                self._job = None

        try:
            self._job = self.widget.after(step_ms, tick, 1)
        except Exception:
            self._job = None

    def _on_destroy(self, event) -> None:
        # <Destroy> 会沿 bindtags 冒泡，必须确认是自己的控件
        if event.widget is self.widget:
            self.cancel()

    def cancel(self) -> None:
        if self._job is not None:
            try:
                self.widget.after_cancel(self._job)
            except Exception:
                pass
            self._job = None
        try:
            _LIVE.remove(self)
        except ValueError:
            pass
