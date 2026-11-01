"""Windows tray: cached snapshots and controller intents; never calls Tk or processes."""
from __future__ import annotations

import logging
import threading

import pystray
from PIL import Image, ImageDraw
from pystray import Menu, MenuItem

from app.controller import Controller
from core.config import config_path, find_rclone_conf
from core.resources import icon_path

STATE_TEXT = {"stopped": "已停止", "starting": "启动中", "ready": "就绪",
              "stopping": "停止中", "failed": "失败"}


def _status_badge(base, color):
    image = base.copy()
    draw = ImageDraw.Draw(image)
    draw.ellipse((43, 43, 62, 62), fill=color, outline="white", width=2)
    return image


class TrayApp:
    def __init__(self, controller: Controller):
        self.controller = controller
        self._lock = threading.Lock()
        self._snapshot = controller.snapshot()
        self._signature = None
        self._thread: threading.Thread | None = None
        self._error = ""
        try:
            with Image.open(icon_path()) as source:
                base = source.convert("RGBA").resize((64, 64), Image.Resampling.LANCZOS)
        except (OSError, ValueError):
            from ui.brand import app_mark
            base = app_mark(64)
        # 状态色与 ui.theme 深色调色板一致（成功 / 普通 / 错误）
        self._images = {"ready": _status_badge(base, "#65C99A"),
                        "stopped": _status_badge(base, "#9296A3"),
                        "failed": _status_badge(base, "#E87979")}
        self.icon = pystray.Icon("rclone_tray", self._images["stopped"], "rclone_tray", self._menu())

    @property
    def error(self):
        return self._error

    def start(self):
        def run():
            try:
                self.icon.run()
            except Exception:
                self._error = "托盘图标无法启动；主窗口仍可管理挂载和退出。"
                self.controller.queue_navigation("mounts")
        self._thread = threading.Thread(target=run, daemon=True, name="windows-tray")
        self._thread.start()

    def refresh(self, snapshot):
        gui = snapshot["gui"]
        config = snapshot["config"]
        signature = (snapshot["revision"], snapshot["version"], snapshot["config_error"],
                     snapshot["environment_error"], snapshot["closing"], tuple(sorted(snapshot["busy"])),
                     gui, tuple(sorted(snapshot["mounts"].items())))
        if signature == self._signature:
            return
        self._signature = signature
        with self._lock:
            self._snapshot = snapshot
        statuses = [gui, *snapshot["mounts"].values()]
        failed = snapshot["config_error"] or snapshot["environment_error"] or any(item.state == "failed" for item in statuses)
        state = "failed" if failed else "ready" if any(item.state == "ready" for item in statuses) else "stopped"
        ready = sum(item.state == "ready" for item in snapshot["mounts"].values())
        self.icon.icon = self._images[state]
        self.icon.title = f"rclone_tray · GUI {STATE_TEXT.get(gui.state, gui.state)} · 挂载 {ready}/{len(config.mounts)}"
        try:
            self.icon.menu = self._menu()
            self.icon.update_menu()
        except Exception:
            logging.warning("Tray menu refresh unavailable")

    def _menu(self):
        snapshot = self._snapshot
        config = snapshot["config"]
        gui = snapshot["gui"]
        enabled = not snapshot["closing"]
        gui_running = gui.pid is not None
        mount_items = []
        for mount in config.mounts:
            status = snapshot["mounts"].get(mount.id)
            running = status and status.pid is not None
            def mount_action(mid, active):
                def toggle(icon, item):
                    (self.controller.stop_mount if active else self.controller.start_mount)(mid)
                return toggle
            mount_items.append(MenuItem(f"{mount.name} → {mount.mount_point} · {STATE_TEXT.get(status.state, '未配置') if status else '未配置'}",
                                        mount_action(mount.id, running), enabled=enabled and mount.id not in snapshot["busy"], checked=lambda item, active=running: bool(active)))
        if not mount_items:
            mount_items.append(MenuItem("暂无挂载", None, enabled=False))
        mount_items.extend([Menu.SEPARATOR, MenuItem("添加挂载…", lambda icon, item: self.controller.queue_navigation("add_mount"), enabled=enabled)])
        return Menu(
            MenuItem("打开挂载管理", lambda icon, item: self.controller.queue_navigation("mounts"), default=True),
            MenuItem("停止 rclone GUI" if gui_running else "启动 rclone GUI",
                     lambda icon, item: (self.controller.stop_gui if gui_running else self.controller.start_gui)(),
                     enabled=enabled and "gui" not in snapshot["busy"]),
            MenuItem("打开 rclone GUI 网页", lambda icon, item: self.controller.open_gui(), enabled=enabled and gui.state == "ready"),
            MenuItem("挂载", Menu(*mount_items)), Menu.SEPARATOR,
            MenuItem(f"rclone v{snapshot['version']}" if snapshot["version"] else "未检测到有效 rclone", None, enabled=False),
            MenuItem("打开 rclone.conf", lambda icon, item: self.controller.open_rclone_config(), enabled=enabled),
            MenuItem("打开配置目录", lambda icon, item: self.controller.open_path((find_rclone_conf(config.rclone_conf) or config_path()).parent), enabled=enabled),
            Menu.SEPARATOR,
            MenuItem("系统自启", lambda icon, item: self.controller.set_startup(not config.startup_enabled), checked=lambda item: config.startup_enabled, enabled=enabled and "settings" not in snapshot["busy"]),
            MenuItem("开机自动挂载", lambda icon, item: self.controller.set_auto_mount(not config.auto_mount_on_boot), checked=lambda item: config.auto_mount_on_boot, enabled=enabled and "settings" not in snapshot["busy"]),
            MenuItem("设置…", lambda icon, item: self.controller.queue_navigation("settings"), enabled=enabled),
            MenuItem("退出…", lambda icon, item: self.controller.queue_navigation("exit"), enabled=enabled))

    def stop(self):
        self.icon.stop()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
