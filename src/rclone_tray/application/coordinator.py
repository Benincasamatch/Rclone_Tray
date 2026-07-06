"""Application Coordinator - 应用协调中心

职责（来自架构设计）:
- 唯一协调中心
- 生命周期编排
- 业务流程控制
- 请求路由
- 事件分发

通信规则:
    所有模块之间不直接通信，全部通过 Coordinator 中转。
    UI → Coordinator → Service → Coordinator → UI
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable, Optional

from rclone_tray import __app_name__, __version__
from rclone_tray.domain.models import MountState, Profile
from rclone_tray.domain.mount_manager import MountManager
from rclone_tray.domain.profile_manager import ProfileManager
from rclone_tray.infrastructure.config_service import ConfigService
from rclone_tray.infrastructure.logger import get_logger, setup_logging
from rclone_tray.infrastructure.notification import (
    NotificationLevel,
    NotificationService,
)
from rclone_tray.infrastructure.rclone_service import (
    RcloneNotFoundError,
    RcloneService,
)
from rclone_tray.infrastructure.system import SystemService
from rclone_tray.infrastructure.watchdog import Watchdog, WatchdogEvent

logger = get_logger(__name__)


class ApplicationCoordinator:
    """应用协调中心 - 系统的唯一入口和枢纽。

    所有 UI 请求都发到这里，由它编排各个服务完成工作。
    """

    def __init__(self, config_dir: str | Path) -> None:
        self._config_dir = Path(config_dir)
        self._running = False

        # ── 基础设施 ──
        self._config_service = ConfigService(self._config_dir)
        self._rclone_service = RcloneService()

        # ── Watchdog ──
        self._watchdog = Watchdog(
            rclone_service=self._rclone_service,
            event_callback=self._on_watchdog_event,
        )

        # ── 领域服务 ──
        self._profile_manager = ProfileManager(self._config_service)
        self._mount_manager = MountManager(
            rclone_service=self._rclone_service,
            watchdog=self._watchdog,
        )

        # ── 通知 ──
        self._notification = NotificationService()

        # ── UI 回调 ──
        self._on_state_change_callbacks: list[Callable] = []
        self._on_log_callbacks: list[Callable] = []

        # 将状态变更从 MountManager 传到 UI
        self._mount_manager.set_on_state_change(self._on_mount_state_change)

    # ── 属性 ──────────────────────────────────────────────

    @property
    def config_dir(self) -> Path:
        return self._config_dir

    @property
    def config_service(self) -> ConfigService:
        return self._config_service

    @property
    def profile_manager(self) -> ProfileManager:
        return self._profile_manager

    @property
    def mount_manager(self) -> MountManager:
        return self._mount_manager

    @property
    def notification(self) -> NotificationService:
        return self._notification

    @property
    def system(self) -> type[SystemService]:
        return SystemService

    @property
    def app_name(self) -> str:
        return __app_name__

    @property
    def app_version(self) -> str:
        return __version__

    @property
    def is_running(self) -> bool:
        return self._running

    # ── 生命周期 ──────────────────────────────────────────

    def initialize(self) -> None:
        """初始化应用（加载配置、检测环境）。"""
        logger.info("=" * 50)
        logger.info("%s v%s 启动中...", __app_name__, __version__)
        logger.info("=" * 50)

        # 加载配置
        self._profile_manager.load()
        logger.info("配置加载完成 (%d 个 Profile)", len(self._profile_manager.profiles))

        # 检测 rclone
        rclone_path = self._detect_rclone()
        if rclone_path:
            logger.info("rclone 已检测到: %s", rclone_path)
        else:
            logger.warning("未检测到 rclone，请手动设置路径")

        self._running = True

    def shutdown(self) -> None:
        """安全关闭应用。"""
        logger.info("正在关闭应用...")

        # 停止 Watchdog
        self._watchdog.stop()

        # 停止所有挂载
        if self._mount_manager.is_any_mounted:
            self._mount_manager.stop("*")

        logger.info("应用已关闭")

    # ── 用户命令 ──────────────────────────────────────────

    def cmd_start_mount(self, profile_name: str) -> None:
        """命令: 启动挂载。"""
        profile = self._profile_manager.get_profile(profile_name)
        if not profile:
            logger.error("启动失败: 配置 '%s' 不存在", profile_name)
            return

        logger.info("执行命令: 启动挂载 '%s'", profile_name)
        self._notification.start_stuck_timer(profile_name)
        self._mount_manager.start(profile)

    def cmd_stop_mount(self, profile_name: str) -> None:
        """命令: 停止挂载。"""
        logger.info("执行命令: 停止挂载 '%s'", profile_name)
        self._mount_manager.stop(profile_name)

    def cmd_restart_mount(self, profile_name: str) -> None:
        """命令: 重启挂载。"""
        profile = self._profile_manager.get_profile(profile_name)
        if not profile:
            logger.error("重启失败: 配置 '%s' 不存在", profile_name)
            return
        logger.info("执行命令: 重启挂载 '%s'", profile_name)
        self._mount_manager.restart(profile)

    def cmd_create_profile(self, name: str) -> Profile:
        """命令: 创建新配置。"""
        profile = self._profile_manager.create_profile(name)
        self._profile_manager.save()
        return profile

    def cmd_delete_profile(self, name: str) -> None:
        """命令: 删除配置。"""
        # 先停止挂载
        info = self._mount_manager.get_mount_info(name)
        if info and info.state == MountState.MOUNTED:
            self._mount_manager.stop(name)
        self._profile_manager.delete_profile(name)
        self._profile_manager.save()

    def cmd_rename_profile(self, old_name: str, new_name: str) -> Profile:
        """命令: 重命名配置。"""
        profile = self._profile_manager.rename_profile(old_name, new_name)
        self._profile_manager.save()
        return profile

    def cmd_switch_profile(self, name: str) -> Profile:
        """命令: 切换配置。"""
        profile = self._profile_manager.switch_profile(name)
        self._profile_manager.save()
        return profile

    def cmd_save_profile(self, profile: Profile) -> None:
        """命令: 保存配置。"""
        self._profile_manager.save()

    def cmd_set_rclone_path(self, path: str) -> None:
        """命令: 设置 rclone 路径。"""
        data = self._config_service.read()
        data["general"]["rclone_path"] = path
        self._config_service.write(data)
        logger.info("rclone 路径已设置: %s", path)

    def cmd_set_auto_start(self, enabled: bool) -> None:
        """命令: 设置开机自启动。"""
        SystemService.set_auto_start(enabled)

    def cmd_set_minimize_to_tray(self, enabled: bool) -> None:
        """命令: 设置最小化到托盘。"""
        data = self._config_service.read()
        data["general"]["minimize_to_tray"] = enabled
        self._config_service.write(data)

    def cmd_set_language(self, lang: str) -> None:
        """命令: 设置语言。"""
        data = self._config_service.read()
        data["general"]["language"] = lang
        self._config_service.write(data)

    def cmd_set_theme(self, theme: str) -> None:
        """命令: 设置主题。"""
        data = self._config_service.read()
        data["general"]["theme"] = theme
        self._config_service.write(data)

    def cmd_export_config(self) -> dict[str, Any]:
        """命令: 导出配置。"""
        return self._profile_manager.export_profiles()

    def cmd_import_config(self, data: dict[str, Any], merge: bool = False) -> int:
        """命令: 导入配置。"""
        count = self._profile_manager.import_profiles(data, merge)
        self._profile_manager.save()
        return count

    def cmd_backup_config(self) -> str:
        """命令: 备份配置。"""
        return str(self._config_service.backup())

    def cmd_restore_config(self, backup_path: str) -> None:
        """命令: 恢复配置。"""
        self._config_service.restore(backup_path)
        self._profile_manager.load()

    # ── 查询接口 ──────────────────────────────────────────

    def query_rclone_status(self) -> dict[str, Any]:
        """查询 rclone 状态。"""
        if self._profile_manager.active_profile:
            info = self._mount_manager.get_mount_info(
                self._profile_manager.active_profile_name or ""
            )
            if info:
                return {
                    "profile_name": info.profile_name,
                    "state": info.state.value,
                    "message": info.message,
                    "pids": info.pids,
                    "retry_count": info.retry_count,
                }
        return {"state": MountState.UNCONFIGURED.value, "message": "未配置"}

    def query_auto_detect_rclone(self) -> Optional[str]:
        """自动检测 rclone。"""
        return RcloneService.auto_detect_rclone()

    def query_check_rclone_version(self, path: str) -> Optional[str]:
        """检查 rclone 版本。"""
        return RcloneService.check_rclone_version(path)

    def query_available_drives(self) -> list[str]:
        """查询可用盘符。"""
        return SystemService.get_available_drives()

    # ── UI 回调注册 ──────────────────────────────────────

    def set_notification_tray(self, tray_icon) -> None:
        """设置通知所需的托盘图标（延迟注入，因为 UI 创建较晚）。"""
        self._notification.set_tray_icon(tray_icon)

    def on_state_change(self, callback: Callable) -> None:
        """注册状态变更回调。"""
        self._on_state_change_callbacks.append(callback)

    def on_log(self, callback: Callable) -> None:
        """注册日志回调。"""
        self._on_log_callbacks.append(callback)

    # ── 内部事件处理 ─────────────────────────────────────

    def _on_watchdog_event(self, event: WatchdogEvent, mount_point: str) -> None:
        """Watchdog 事件处理。"""
        logger.debug("Watchdog 事件: %s (%s)", event.value, mount_point)
        self._mount_manager.handle_watchdog_event(event, mount_point)

    def _on_mount_state_change(self, profile_name: str, state: MountState) -> None:
        """挂载状态变更处理。"""
        logger.info("状态变更: %s -> %s", profile_name, state.value)

        # 通知
        if state == MountState.MOUNTED:
            self._notification.notify_mount_success(profile_name, "")
        elif state == MountState.ERROR:
            info = self._mount_manager.get_mount_info(profile_name)
            msg = info.message if info else "未知错误"
            self._notification.notify_mount_failed(profile_name, msg)
        elif state == MountState.STARTING:
            self._notification.start_stuck_timer(profile_name)

        # 通知 UI
        for cb in self._on_state_change_callbacks:
            try:
                cb(profile_name, state)
            except Exception as exc:
                logger.error("UI 回调异常: %s", exc)

    def _detect_rclone(self) -> Optional[str]:
        """检测 rclone。"""
        # 先从配置读取
        data = self._config_service.read()
        configured_path = data.get("general", {}).get("rclone_path", "")
        if configured_path:
            version = RcloneService.check_rclone_version(configured_path)
            if version:
                return configured_path

        # 自动检测
        detected = RcloneService.auto_detect_rclone()
        if detected:
            version = RcloneService.check_rclone_version(detected)
            if version:
                logger.info("自动检测到 rclone: %s (%s)", detected, version)
                # 保存到配置
                data["general"]["rclone_path"] = detected
                self._config_service.write(data)
                return detected

        return None
