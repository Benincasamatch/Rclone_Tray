"""Windows desktop entry: one Tk main loop, shared controller, clean owned-process shutdown."""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import logging
import os
import sys
import tkinter as tk
from tkinter import messagebox

from app.controller import Controller
from app.notification import notify
from app.tray import TrayApp
from core.config import logs_dir
from ui.settings_window import SettingsWindow
from ui.theme import enable_dpi_awareness

SINGLE_INSTANCE_MUTEX = "Local\\rclone_tray_single_instance"


def _acquire_single_instance() -> bool:
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    namespace = os.environ.get("RCLONE_TRAY_DATA_DIR", "")
    suffix = "_" + hashlib.sha256(os.path.abspath(namespace).encode()).hexdigest()[:12] if namespace else ""
    handle = kernel32.CreateMutexW(None, False, SINGLE_INSTANCE_MUTEX + suffix)
    if not handle:
        raise OSError("无法创建应用单实例锁。")
    if kernel32.GetLastError() == 183:
        kernel32.CloseHandle(handle)
        return False
    _acquire_single_instance.handle = handle
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="rclone Windows 托盘管理")
    parser.add_argument("--autostart", action="store_true", help="系统登录启动标记")
    parser.add_argument("--show", action="store_true", help="显示主窗口")
    args = parser.parse_args()
    enable_dpi_awareness()
    if not _acquire_single_instance():
        ctypes.windll.user32.MessageBoxW(None, "rclone_tray 已在运行，请从托盘打开挂载管理。", "rclone_tray", 0x40)
        return
    root = tk.Tk()
    root.withdraw()
    controller = None
    tray = None
    try:
        logging.basicConfig(level=logging.INFO, filename=str(logs_dir() / "app.log"),
                            format="%(asctime)s %(levelname)s %(message)s", encoding="utf-8")
        controller = Controller(os.path.abspath(sys.argv[0]))
        window = SettingsWindow(root, controller)
        tray = TrayApp(controller)
        tray.start()
        controller.refresh_environment()
        controller.schedule_startup(args.autostart)
        if args.show or not args.autostart or controller.snapshot()["config_error"]:
            window.show()

        def pump():
            try:
                for event in controller.drain_events():
                    if event.get("kind") == "navigate":
                        page = event.get("page", "mounts")
                        if page == "exit":
                            window.request_exit()
                        else:
                            window.show(page=page, mount_id=event.get("mount_id", ""))
                    else:
                        window.handle_event(event)
                        if not event.get("ok") and root.state() == "withdrawn":
                            notify("rclone_tray", "操作未完成；请打开托盘中的挂载管理查看原因。")
                        if event.get("key") == "shutdown" and event.get("ok"):
                            tray.stop()
                            controller.close_executor()
                            root.destroy()
                            return
                snapshot = controller.snapshot()
                window.refresh(snapshot)
                tray.refresh(snapshot)
                if tray.error and root.state() == "withdrawn":
                    window.show()
            except Exception:
                # 单帧刷新失败不应让事件泵停摆：记录后继续下一帧，避免界面假死。
                logging.exception("UI pump failure")
            try:
                root.after(120, pump)
            except tk.TclError:
                logging.info("UI 循环结束：窗口已销毁。")

        root.after(120, pump)
        root.mainloop()
    except Exception:
        logging.exception("Desktop lifecycle failure")
        messagebox.showerror("rclone_tray", "应用启动或运行失败，请查看应用日志。未覆盖配置。", parent=root)
    finally:
        if controller:
            controller.pm.cancel.set()
            controller.pm.stop_all()
            controller.close_executor()
        if tray:
            tray.stop()
        if hasattr(_acquire_single_instance, "handle"):
            ctypes.windll.kernel32.CloseHandle(_acquire_single_instance.handle)


if __name__ == "__main__":
    main()
