"""Tray - 系统托盘

职责：
- 提供系统托盘入口
- 管理托盘图标状态（已挂载/未挂载/错误）
- 显示托盘菜单
- 订阅状态变化并更新 UI

遵循架构:
    不做业务判断
    不直接调用 rclone
    所有操作通过 Coordinator
"""

from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QAction, QIcon, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from rclone_tray.domain.models import MountState
from rclone_tray.infrastructure.logger import get_logger

logger = get_logger(__name__)

# SVG 图标的 Base64 数据（内置，无外部文件依赖）
_TRAY_ICON_MOUNTED = """
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32" width="32" height="32">
  <circle cx="16" cy="16" r="14" fill="#22c55e" stroke="#15803d" stroke-width="2"/>
  <path d="M10 18 L16 12 L22 18" stroke="white" stroke-width="2.5" fill="none" stroke-linecap="round" stroke-linejoin="round"/>
</svg>
"""

_TRAY_ICON_DISCONNECTED = """
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32" width="32" height="32">
  <circle cx="16" cy="16" r="14" fill="#6b7280" stroke="#374151" stroke-width="2"/>
  <circle cx="16" cy="16" r="4" fill="white"/>
</svg>
"""

_TRAY_ICON_ERROR = """
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32" width="32" height="32">
  <circle cx="16" cy="16" r="14" fill="#ef4444" stroke="#dc2626" stroke-width="2"/>
  <line x1="11" y1="11" x2="21" y2="21" stroke="white" stroke-width="2.5" stroke-linecap="round"/>
  <line x1="21" y1="11" x2="11" y2="21" stroke="white" stroke-width="2.5" stroke-linecap="round"/>
</svg>
"""

_TRAY_ICON_STARTING = """
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32" width="32" height="32">
  <circle cx="16" cy="16" r="14" fill="#f59e0b" stroke="#d97706" stroke-width="2"/>
  <path d="M16 8 L16 16 L22 19" stroke="white" stroke-width="2.5" fill="none" stroke-linecap="round" stroke-linejoin="round"/>
</svg>
"""


def _make_icon(svg_data: str) -> QIcon:
    """从 SVG 数据创建 QIcon。"""
    pixmap = QPixmap()
    pixmap.loadFromData(svg_data.encode("utf-8"), "SVG")
    return QIcon(pixmap)


class TrayService(QObject):
    """系统托盘服务。"""

    # 信号
    show_main_window_requested = Signal()
    show_settings_requested = Signal()
    show_logs_requested = Signal()
    quit_requested = Signal()
    mount_toggled = Signal(str)  # profile_name
    stop_requested = Signal(str)
    switch_profile_requested = Signal(str)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._tray_icon: Optional[QSystemTrayIcon] = None
        self._menu: Optional[QMenu] = None
        self._status_action: Optional[QAction] = None
        self._profile_submenu: Optional[QMenu] = None
        self._profile_names: list[str] = []
        self._current_state: MountState = MountState.UNCONFIGURED

        # 内置图标
        self._icons = {
            "mounted": _make_icon(_TRAY_ICON_MOUNTED),
            "disconnected": _make_icon(_TRAY_ICON_DISCONNECTED),
            "error": _make_icon(_TRAY_ICON_ERROR),
            "starting": _make_icon(_TRAY_ICON_STARTING),
        }

    # ── 初始化 ────────────────────────────────────────────

    def initialize(self) -> QSystemTrayIcon:
        """初始化托盘并返回 QSystemTrayIcon 实例。"""
        self._tray_icon = QSystemTrayIcon(self)
        self._tray_icon.setIcon(self._icons["disconnected"])
        self._tray_icon.setToolTip("Rclone Tray - 未连接")

        # 创建托盘菜单
        self._build_menu()

        self._tray_icon.setContextMenu(self._menu)
        self._tray_icon.activated.connect(self._on_tray_activated)
        self._tray_icon.show()

        logger.info("系统托盘已初始化")
        return self._tray_icon

    @property
    def tray_icon(self) -> Optional[QSystemTrayIcon]:
        return self._tray_icon

    # ── 更新方法 ─────────────────────────────────────────

    def update_profile_list(self, profile_names: list[str], active: Optional[str] = None) -> None:
        """更新托盘菜单中的 Profile 列表。"""
        self._profile_names = profile_names
        if not self._profile_submenu:
            return

        self._profile_submenu.clear()
        for name in profile_names:
            action = QAction(name, self._profile_submenu)
            action.setCheckable(True)
            action.setChecked(name == active)
            action.triggered.connect(lambda checked, n=name: self.switch_profile_requested.emit(n))
            self._profile_submenu.addAction(action)

    def update_state(self, state: MountState, profile_name: Optional[str] = None) -> None:
        """更新托盘图标和状态显示。"""
        self._current_state = state

        # 更新图标
        icon_key = "disconnected"
        tooltip = "Rclone Tray"

        if state == MountState.MOUNTED:
            icon_key = "mounted"
            tooltip = f"Rclone Tray - 已挂载"
            if profile_name:
                tooltip += f" ({profile_name})"
        elif state == MountState.ERROR:
            icon_key = "error"
            tooltip = f"Rclone Tray - 异常"
        elif state in (MountState.STARTING, MountState.RESTARTING):
            icon_key = "starting"
            tooltip = f"Rclone Tray - 处理中"

        if self._tray_icon:
            self._tray_icon.setIcon(self._icons[icon_key])
            self._tray_icon.setToolTip(tooltip)

        # 更新状态文本
        if self._status_action:
            state_names = {
                MountState.UNCONFIGURED: "⚪ 未配置",
                MountState.CONFIGURED: "🔵 待启动",
                MountState.STARTING: "🟡 启动中...",
                MountState.MOUNTED: "🟢 已挂载",
                MountState.RESTARTING: "🟠 恢复中...",
                MountState.ERROR: "🔴 异常",
                MountState.STOPPING: "🟡 正在停止...",
                MountState.STOPPED: "⚪ 已停止",
            }
            self._status_action.setText(state_names.get(state, "⚪ 未知"))

    # ── 内部方法 ─────────────────────────────────────────

    def _build_menu(self) -> None:
        """构建托盘菜单。"""
        self._menu = QMenu()

        # 状态显示
        self._status_action = QAction("⚪ 未配置")
        self._status_action.setEnabled(False)
        self._menu.addAction(self._status_action)
        self._menu.addSeparator()

        # 挂载控制
        self._menu.addAction("▶ 启动挂载", self._on_mount_clicked)
        self._menu.addAction("⏹ 停止挂载", lambda: self.stop_requested.emit(""))
        self._menu.addAction("🔄 重启服务", self._on_mount_clicked)
        self._menu.addSeparator()

        # Profile 切换
        self._profile_submenu = QMenu("📂 切换配置", self._menu)
        self._menu.addMenu(self._profile_submenu)
        self._menu.addSeparator()

        # 窗口
        self._menu.addAction("📟 打开主窗口", self.show_main_window_requested.emit)
        self._menu.addAction("⚙️ 设置", self.show_settings_requested.emit)
        self._menu.addAction("📋 查看日志", self.show_logs_requested.emit)
        self._menu.addSeparator()

        # 退出
        self._menu.addAction("🚪 退出", self.quit_requested.emit)

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        """托盘图标点击事件。"""
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.show_main_window_requested.emit()

    def _on_mount_clicked(self) -> None:
        """挂载按钮点击。"""
        if self._profile_names:
            self.mount_toggled.emit(self._profile_names[0])
