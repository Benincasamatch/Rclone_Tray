"""pystray 托盘图标与动态菜单。"""
from __future__ import annotations

import os
import sys
import threading
import webbrowser
from typing import Optional

import pystray
from PIL import Image, ImageDraw
from pystray import Menu, MenuItem

try:  # Pillow 9.1+ 枚举写法，兼容旧版本
    _RESAMPLE = Image.Resampling.LANCZOS
except AttributeError:
    _RESAMPLE = Image.LANCZOS

from core import startup as startup_mod
from core.config import AppConfig, config_path, find_rclone_conf, save_config
from core.processes import ProcessManager
from core.rclone import get_version

from app.notification import notify

GREEN = (46, 160, 67)
GRAY = (120, 120, 120)
ORANGE = (217, 119, 6)


def _resource_path(rel: str) -> str:
    """打包解包目录（sys._MEIPASS）或源码根目录下的资源绝对路径。"""
    base = getattr(sys, "_MEIPASS", None)
    if base is None:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


def _load_base_icon() -> Image.Image:
    """加载 assets/icon.ico 作为托盘图标基底（放缩到 64x64 保持清晰）。"""
    img = Image.open(_resource_path(os.path.join("assets", "icon.ico"))).convert("RGBA")
    return img.resize((64, 64), _RESAMPLE)


def _status_badge(img: Image.Image, color: tuple) -> Image.Image:
    """在图标右下角叠加状态圆点：绿=运行 / 灰=停止 / 橙=警告。"""
    out = img.copy()
    d = ImageDraw.Draw(out)
    size = out.width
    r = max(6, size // 9)
    pad = max(2, size // 16)
    x1, y1 = size - 2 * r - pad, size - 2 * r - pad
    x2, y2 = size - pad, size - pad
    d.ellipse(
        [x1, y1, x2, y2],
        fill=color,
        outline=(255, 255, 255, 255),
        width=max(2, size // 24),
    )
    return out


def _make_icon(color: tuple) -> Image.Image:
    """回退方案：图标资源缺失时绘制圆角方块 + 状态圆点。"""
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([2, 2, size - 2, size - 2], radius=16, fill=(80, 80, 80))
    return _status_badge(img, color)


class TrayApp:
    """托盘应用：管理图标、动态菜单与刷新。"""

    def __init__(self, cfg: AppConfig, pm: ProcessManager, monitor, notify_on_start: bool = True):
        self.cfg = cfg
        self.pm = pm
        self.monitor = monitor
        self.notify_on_start = notify_on_start
        self.icon: Optional[pystray.Icon] = None
        self.script = os.path.abspath(sys.argv[0])
        self.version = get_version(cfg.rclone_path) if cfg.rclone_path else None
        try:
            base = _load_base_icon()
            self._img_run = _status_badge(base, GREEN)
            self._img_stop = _status_badge(base, GRAY)
            self._img_warn = _status_badge(base, ORANGE)
        except Exception:
            self._img_run = _make_icon(GREEN)
            self._img_stop = _make_icon(GRAY)
            self._img_warn = _make_icon(ORANGE)
        self._last_sig: Optional[tuple] = None

    # ---------- 生命周期 ----------
    def run(self) -> None:
        self.icon = pystray.Icon(
            "rclone_tray",
            icon=self._img_stop,
            title="rclone_tray",
            menu=self._build_menu(),
        )
        self.monitor.on_update = self.refresh
        self.monitor.start()
        self.refresh(force=True)
        # 仅在用户手动打开时提示，开机自启时保持安静
        if self.notify_on_start:
            notify("rclone_tray", "程序已在托盘中启动，点击托盘图标进行管理。")
        self.icon.run()
        self.monitor.stop()

    def refresh(self, force: bool = False) -> None:
        if self.icon is None:
            return
        sig = (self.pm.state_signature(), self.version)
        if not force and sig == self._last_sig:
            return
        self._last_sig = sig
        try:
            self.icon.menu = self._build_menu()
            self.icon.icon = self._status_image()
            self.icon.title = self.pm.summary()
            self.icon.update_menu()
        except Exception:
            pass

    def _status_image(self) -> Image.Image:
        if not self.cfg.rclone_path:
            return self._img_warn
        if self.pm.gui_running() or any(
            self.pm.mount_running(mid) for mid in self.pm.mounts
        ):
            return self._img_run
        return self._img_stop

    # ---------- 菜单 ----------
    def _version_text(self) -> str:
        if self.version:
            return f"rclone v{self.version}"
        return "未找到 rclone（设置中指定路径）"

    def _build_menu(self) -> Menu:
        pm = self.pm
        has_rclone = bool(pm.gui)
        gui_text = "停止 rclone GUI" if pm.gui_running() else "启动 rclone GUI"
        return Menu(
            MenuItem(
                gui_text,
                self._toggle_gui,
                enabled=lambda item: has_rclone,
                checked=lambda item: pm.gui_running(),
            ),
            MenuItem(
                "打开 rclone GUI 网页",
                self._open_gui_web,
                enabled=lambda item: pm.gui_running() and pm.gui is not None,
            ),
            Menu.SEPARATOR,
            MenuItem("挂载管理", self._build_mount_menu()),
            Menu.SEPARATOR,
            MenuItem(self._version_text(), None, enabled=False),
            MenuItem("打开 rclone 配置目录", self._open_conf_dir),
            MenuItem("打开 rclone.conf", self._open_conf_file),
            Menu.SEPARATOR,
            MenuItem(
                "开机自启",
                self._toggle_startup,
                checked=lambda item: startup_mod.is_enabled(),
            ),
            MenuItem(
                "开机自动挂载",
                self._toggle_auto_mount,
                checked=lambda item: self.cfg.auto_mount_on_boot,
            ),
            Menu.SEPARATOR,
            MenuItem("设置...", self._open_settings),
            MenuItem("退出", self._quit),
        )

    def _build_mount_menu(self) -> Menu:
        pm = self.pm

        def make_toggle(mid: str):
            def toggle(icon, item) -> None:
                self._toggle_mount(mid)

            return toggle

        items = []
        if not pm.mounts:
            items.append(MenuItem("（暂无挂载，点击下方添加）", None, enabled=False))
        else:
            for mid, mp in pm.mounts.items():
                m = mp.mount
                mark = "●" if mp.running() else "○"
                items.append(
                    MenuItem(
                        f"{mark} {m.name} → {m.mount_point}",
                        make_toggle(mid),
                        checked=lambda item, mid=mid: pm.mount_running(mid),
                    )
                )
        items.append(Menu.SEPARATOR)
        items.append(MenuItem("添加挂载...", self._open_settings_add_mount))
        return Menu(*items)

    # ---------- 动作 ----------
    def _toggle_gui(self, icon, item) -> None:
        if self.pm.gui_running():
            self.pm.stop_gui()
        else:
            self.pm.start_gui()
        self.refresh(force=True)

    def _toggle_mount(self, mid: str) -> None:
        if self.pm.mount_running(mid):
            self.pm.stop_mount(mid)
        else:
            self.pm.start_mount(mid)
        self.refresh(force=True)

    def _open_gui_web(self, icon, item) -> None:
        gui = self.pm.gui
        if gui is None:
            return
        if not gui.running():
            notify("rclone_tray", "rclone GUI 未运行，请先启动。")
            return
        # url() 会等待 rclone 写出本次监听地址，避免阻塞菜单线程时放到子线程执行
        threading.Thread(
            target=lambda: webbrowser.open(gui.url()),
            daemon=True,
            name="open-gui-web",
        ).start()

    def _open_conf_dir(self, icon, item) -> None:
        conf = find_rclone_conf()
        target = str(conf.parent) if conf else str(config_path().parent)
        os.startfile(target)

    def _open_conf_file(self, icon, item) -> None:
        conf = find_rclone_conf()
        if conf:
            os.startfile(str(conf))
        else:
            self._open_conf_dir(icon, item)

    def _toggle_startup(self, icon, item) -> None:
        if startup_mod.is_enabled():
            startup_mod.disable()
            self.cfg.startup_enabled = False
        else:
            startup_mod.enable(self.script)
            self.cfg.startup_enabled = True
        save_config(self.cfg)
        self.refresh(force=True)

    def _toggle_auto_mount(self, icon, item) -> None:
        self.cfg.auto_mount_on_boot = not self.cfg.auto_mount_on_boot
        save_config(self.cfg)
        self.refresh(force=True)

    def _open_settings(self, icon, item) -> None:
        self._launch_settings(add_mount=False)

    def _open_settings_add_mount(self, icon, item) -> None:
        self._launch_settings(add_mount=True)

    def _launch_settings(self, add_mount: bool) -> None:
        from app.settings_dialog import open_settings

        open_settings(
            self.cfg, self.pm, on_saved=self._on_settings_saved, add_mount=add_mount
        )

    def _on_settings_saved(self) -> None:
        self.version = get_version(self.cfg.rclone_path) if self.cfg.rclone_path else None
        self.pm.rebuild()
        self.refresh(force=True)

    def _quit(self, icon, item) -> None:
        self.monitor.stop()
        self.pm.stop_all()
        if self.icon is not None:
            self.icon.stop()
