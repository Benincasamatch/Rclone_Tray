"""Notification Service - Windows 通知服务

职责：
- 使用 Windows 原生通知（通过 PySide6 QSystemTrayIcon）
- 显示挂载成功/失败/异常信息
- 长时间挂载中状态提醒

注意：
- 通知依赖于 QSystemTrayIcon 实例
- 应先创建 QSystemTrayIcon 再使用此服务
- 纯基础设施层，不含业务逻辑
"""

from __future__ import annotations

from enum import Enum, auto
from typing import Optional

from PySide6.QtWidgets import QSystemTrayIcon
from PySide6.QtCore import QObject, QTimer
from PySide6.QtGui import QIcon

from rclone_tray.infrastructure.logger import get_logger

logger = get_logger(__name__)


class NotificationLevel(str, Enum):
    """通知级别。"""
    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"


class NotificationService(QObject):
    """Windows 通知服务。

    封装 QSystemTrayIcon.showMessage，提供统一的通知接口。
    """

    # 通知超时（毫秒）
    _DEFAULT_DURATION = 5000
    _STUCK_TIMEOUT = 30_000  # 30秒后提醒"挂载中"

    def __init__(self, tray_icon: Optional[QSystemTrayIcon] = None, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._tray_icon = tray_icon
        self._stuck_timers: dict[str, QTimer] = {}

    # ── 设置托盘图标 ─────────────────────────────────────

    def set_tray_icon(self, tray_icon: QSystemTrayIcon) -> None:
        """设置托盘图标实例（通知依赖此对象）。"""
        self._tray_icon = tray_icon

    # ── 通知方法 ─────────────────────────────────────────

    def notify(
        self,
        title: str,
        message: str,
        level: NotificationLevel = NotificationLevel.INFO,
        duration_ms: int = _DEFAULT_DURATION,
    ) -> None:
        """发送通知。

        Args:
            title: 通知标题
            message: 通知内容
            level: 通知级别
            duration_ms: 显示时长（毫秒）
        """
        if not self._tray_icon or not self._tray_icon.supportsMessages():
            logger.debug("通知被跳过（无托盘图标支持）: [%s] %s - %s", level.value, title, message)
            return

        icon_map = {
            NotificationLevel.INFO: QSystemTrayIcon.MessageIcon.Information,
            NotificationLevel.SUCCESS: QSystemTrayIcon.MessageIcon.Information,
            NotificationLevel.WARNING: QSystemTrayIcon.MessageIcon.Warning,
            NotificationLevel.ERROR: QSystemTrayIcon.MessageIcon.Critical,
        }

        self._tray_icon.showMessage(
            title,
            message,
            icon_map.get(level, QSystemTrayIcon.MessageIcon.Information),
            duration_ms,
        )
        logger.info("通知: [%s] %s - %s", level.value, title, message)

    def notify_mount_success(self, profile_name: str, mount_point: str) -> None:
        """挂载成功通知。"""
        self._clear_stuck_timer(profile_name)
        self.notify(
            "✅ 挂载成功",
            f"{profile_name} 已挂载到 {mount_point}",
            NotificationLevel.SUCCESS,
        )

    def notify_mount_failed(self, profile_name: str, reason: str) -> None:
        """挂载失败通知。"""
        self._clear_stuck_timer(profile_name)
        self.notify(
            "⚠️ 挂载失败",
            f"{profile_name}: {reason}",
            NotificationLevel.ERROR,
        )

    def notify_crash_recovered(self, profile_name: str, retry_count: int) -> None:
        """崩溃恢复成功通知。"""
        self.notify(
            "🔄 自动恢复成功",
            f"{profile_name} 已重新挂载 (第 {retry_count} 次恢复)",
            NotificationLevel.WARNING,
        )

    def notify_max_retries_exceeded(self, profile_name: str, retry_count: int) -> None:
        """超过最大重试次数通知。"""
        self.notify(
            "❌ 自动恢复失败",
            f"{profile_name} 连续 {retry_count} 次启动失败，请查看日志",
            NotificationLevel.ERROR,
        )

    def notify_stuck_mounting(self, profile_name: str) -> None:
        """长时间处于挂载中状态的通知。"""
        self.notify(
            "⏳ 挂载耗时较长",
            f"{profile_name} 长时间处于挂载中状态，请耐心等待或查看日志",
            NotificationLevel.WARNING,
            duration_ms=8000,
        )

    def start_stuck_timer(self, profile_name: str) -> None:
        """启动"挂载中"超时提醒计时器。"""
        self._clear_stuck_timer(profile_name)
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(lambda: self.notify_stuck_mounting(profile_name))
        timer.start(self._STUCK_TIMEOUT)
        self._stuck_timers[profile_name] = timer
        logger.debug("已启动挂载超时提醒: %s", profile_name)

    def _clear_stuck_timer(self, profile_name: str) -> None:
        """清除指定配置的超时提醒计时器。"""
        timer = self._stuck_timers.pop(profile_name, None)
        if timer:
            timer.stop()
            timer.deleteLater()
