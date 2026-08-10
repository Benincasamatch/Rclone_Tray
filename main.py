"""rclone_tray 入口：引导、单实例锁、自动挂载 / 自动启动 GUI。"""
from __future__ import annotations

import logging
import threading

from app.tray import TrayApp
from core import startup as startup_mod
from core.config import load_config, logs_dir, save_config
from core.monitor import Monitor
from core.processes import ProcessManager
from core.rclone import find_rclone, get_version

SINGLE_INSTANCE_MUTEX = "Local\\rclone_tray_single_instance"


def _acquire_single_instance() -> bool:
    """命名互斥体防止双实例（返回 False 表示已有实例在运行）。"""
    import ctypes

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, False, SINGLE_INSTANCE_MUTEX)
    if not handle:
        return False
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        return False
    _acquire_single_instance.handle = handle  # 保持引用防止被回收
    return True


def main() -> None:
    if not _acquire_single_instance():
        print("rclone_tray 已在运行。")
        return

    logging.basicConfig(
        level=logging.INFO,
        filename=str(logs_dir() / "app.log"),
        format="%(asctime)s %(levelname)s %(message)s",
        encoding="utf-8",
    )

    cfg = load_config()

    # 定位 rclone 并缓存
    exe = find_rclone(cfg.rclone_path)
    if exe:
        cfg.rclone_path = exe
        version = get_version(exe)
        logging.info("rclone: %s (v%s)", exe, version or "?")
        save_config(cfg)
    else:
        logging.warning("未找到 rclone，请在设置中指定路径")

    cfg.startup_enabled = startup_mod.is_enabled()

    pm = ProcessManager(cfg)
    monitor = Monitor(poll_interval=3.0)
    tray = TrayApp(cfg, pm, monitor)

    # 开机自动挂载（延迟等待网络/服务就绪）
    if cfg.auto_mount_on_boot and pm.mounts:
        delay = max(int(cfg.mount_delay_seconds), 0)
        timer = threading.Timer(delay, pm.start_all_mounts)
        timer.name = "auto-mount"
        timer.start()
        logging.info("已安排自动挂载（延迟 %ds）", delay)

    # 自动启动 GUI
    if cfg.gui_auto_start:
        pm.start_gui()
        logging.info("已自动启动 rclone GUI")

    tray.run()


if __name__ == "__main__":
    main()
