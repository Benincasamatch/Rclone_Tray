"""System Service - Windows 系统接口

职责：
- 开机自启动管理（注册表）
- 获取系统信息（盘符、可用分区等）
- 检测挂载点是否已被占用
- 纯基础设施层，不含业务逻辑
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import winreg
from pathlib import Path
from typing import Optional

from rclone_tray.infrastructure.logger import get_logger

logger = get_logger(__name__)

# 注册表自启动路径
REGISTRY_RUN_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
REGISTRY_APP_NAME = "RcloneTray"


class SystemService:
    """Windows 系统接口服务。"""

    # ── 开机自启动 ───────────────────────────────────────

    @staticmethod
    def is_auto_start_enabled() -> bool:
        """检查开机自启动是否已启用。"""
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, REGISTRY_RUN_PATH, 0, winreg.KEY_READ
            ) as key:
                value, _ = winreg.QueryValueEx(key, REGISTRY_APP_NAME)
                return bool(value)
        except (FileNotFoundError, OSError):
            return False

    @staticmethod
    def set_auto_start(enabled: bool) -> bool:
        """设置开机自启动。"""
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, REGISTRY_RUN_PATH, 0, winreg.KEY_SET_VALUE
            ) as key:
                if enabled:
                    # 使用当前可执行文件路径
                    exe_path = SystemService._get_app_exe_path()
                    winreg.SetValueEx(key, REGISTRY_APP_NAME, 0, winreg.REG_SZ, exe_path)
                    logger.info("开机自启动已启用: %s", exe_path)
                else:
                    try:
                        winreg.DeleteValue(key, REGISTRY_APP_NAME)
                        logger.info("开机自启动已禁用")
                    except FileNotFoundError:
                        pass
                return True
        except OSError as exc:
            logger.error("设置开机自启动失败: %s", exc)
            return False

    # ── 挂载点检测 ───────────────────────────────────────

    @staticmethod
    def get_available_drives() -> list[str]:
        """获取所有可用盘符（A-Z）。"""
        drives = []
        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            drive = f"{letter}:\\"
            if os.path.exists(drive):
                drives.append(drive)
        return drives

    @staticmethod
    def is_mount_point_available(mount_point: str) -> bool:
        """检查挂载点是否可用（未被占用）。

        支持两种格式:
        - 盘符: "Z:" 或 "Z:\\"
        - 目录: "C:\\Mount\\rclone"
        """
        mp = mount_point.rstrip("\\")

        # 盘符格式
        if re.match(r"^[A-Za-z]:$", mp) or re.match(r"^[A-Za-z]:\\?$", mount_point):
            drive_letter = mp[0].upper()
            return not os.path.exists(f"{drive_letter}:\\")

        # 目录格式
        path = Path(mp)
        if path.exists():
            # 路径已存在，检查是否为空
            return not any(path.iterdir())
        return True

    @staticmethod
    def get_mounted_drives() -> list[str]:
        """获取已挂载的驱动器列表（含网络驱动器）。"""
        try:
            result = subprocess.run(
                ["wmic", "logicaldisk", "get", "name"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                drives = re.findall(r"([A-Z]:)", result.stdout, re.IGNORECASE)
                return [d.upper() for d in drives]
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.debug("获取驱动器列表失败: %s", exc)

        # 降级方案
        return [
            f"{d}:\\" for d in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
            if os.path.exists(f"{d}:\\")
        ]

    @staticmethod
    def is_other_rclone_running() -> list[int]:
        """检测是否有其他 rclone 进程在运行。

        Returns:
            其他 rclone 进程的 PID 列表
        """
        try:
            result = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq rclone.exe", "/FO", "CSV"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                pids = re.findall(r'"(\d+)"', result.stdout)
                current_pid = os.getpid()
                return [int(pid) for pid in pids if int(pid) != current_pid]
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.debug("检测 rclone 进程失败: %s", exc)
        return []

    # ── 内部方法 ──────────────────────────────────────────

    @staticmethod
    def _get_app_exe_path() -> str:
        """获取当前应用的可执行文件路径。"""
        if getattr(sys, "frozen", False):
            # PyInstaller/Nuitka 打包后
            return sys.executable
        # 开发模式：返回 python 脚本路径
        main_script = Path(sys.argv[0]) if sys.argv[0] else Path(__file__)
        return str(main_script.resolve())
