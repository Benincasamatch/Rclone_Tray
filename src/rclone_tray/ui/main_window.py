"""Main Window - 主窗口

功能（对应 wireframe.md）:
- 显示当前配置信息
- 显示挂载状态
- 启动/停止/重启按钮
- 跳转配置管理和设置

界面布局:
    ┌──────────────────────────────┐
    │  Rclone Tray                 │
    ├──────────────────────────────┤
    │  📂 当前配置                  │
    │  [配置名]                     │
    │                              │
    │  🔵 状态: 已挂载              │
    │  📍 挂载路径: Z:              │
    │  🖥 服务器: xxx.xxx.xxx.xxx   │
    │                              │
    │  [▶ 启动] [⏹ 停止] [🔄 重启] │
    ├──────────────────────────────┤
    │  💾 rclone 位置               │
    │  [自动检测] [浏览...] [下载]   │
    ├──────────────────────────────┤
    │  [📋 配置管理] [⚙️ 设置]      │
    └──────────────────────────────┘
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from rclone_tray.domain.models import MountState, Profile
from rclone_tray.infrastructure.logger import get_logger
from rclone_tray.infrastructure.system import SystemService
from rclone_tray.ui.config_dialog import ProfileDialog

logger = get_logger(__name__)

_RCLONE_DOWNLOAD_URL = "https://rclone.org/downloads/"


class MainWindow(QMainWindow):
    """应用程序主窗口。"""

    def __init__(
        self,
        coordinator,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._coordinator = coordinator
        self._current_profile_name: Optional[str] = None

        self._setup_ui()
        self._connect_signals()
        self._refresh_state()

        self.setWindowTitle("Rclone Tray")
        self.setMinimumWidth(480)
        self.setMinimumHeight(520)

    # ── UI 构建 ──────────────────────────────────────────

    def _setup_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setSpacing(12)

        # ── 标题 ──
        title = QLabel("<h2>Rclone Tray</h2>")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        # ── 当前配置区 ──
        profile_group = QGroupBox("📂 当前配置")
        profile_layout = QVBoxLayout(profile_group)

        # 配置选择行
        select_layout = QHBoxLayout()
        self._profile_combo = QComboBox()
        self._profile_combo.setMinimumWidth(200)
        select_layout.addWidget(QLabel("选择配置:"))
        select_layout.addWidget(self._profile_combo)
        select_layout.addStretch()
        profile_layout.addLayout(select_layout)

        # 状态信息
        self._status_label = QLabel("状态: ⚪ 未配置")
        self._status_label.setStyleSheet("font-size: 14px; padding: 4px;")
        profile_layout.addWidget(self._status_label)

        self._mount_path_label = QLabel("挂载路径: -")
        profile_layout.addWidget(self._mount_path_label)

        self._remote_label = QLabel("远程地址: -")
        profile_layout.addWidget(self._remote_label)

        self._message_label = QLabel("")
        self._message_label.setStyleSheet("color: #666; font-style: italic;")
        self._message_label.setWordWrap(True)
        profile_layout.addWidget(self._message_label)

        # 操作按钮
        btn_layout = QHBoxLayout()
        self._start_btn = QPushButton("▶ 启动")
        self._start_btn.setStyleSheet(
            "background-color: #22c55e; color: white; font-weight: bold; padding: 8px 16px;"
        )
        self._stop_btn = QPushButton("⏹ 停止")
        self._stop_btn.setStyleSheet(
            "background-color: #ef4444; color: white; font-weight: bold; padding: 8px 16px;"
        )
        self._stop_btn.setEnabled(False)
        self._restart_btn = QPushButton("🔄 重启")
        self._restart_btn.setStyleSheet("padding: 8px 16px;")
        self._restart_btn.setEnabled(False)

        btn_layout.addWidget(self._start_btn)
        btn_layout.addWidget(self._stop_btn)
        btn_layout.addWidget(self._restart_btn)
        profile_layout.addLayout(btn_layout)

        # 进度条（挂载中显示）
        self._progress_bar = QProgressBar()
        self._progress_bar.setRange(0, 0)  # 无限进度
        self._progress_bar.setVisible(False)
        profile_layout.addWidget(self._progress_bar)

        layout.addWidget(profile_group)

        # ── rclone 位置 ──
        rclone_group = QGroupBox("💾 rclone 位置")
        rclone_layout = QVBoxLayout(rclone_group)

        self._rclone_status_label = QLabel("检测中...")
        rclone_layout.addWidget(self._rclone_status_label)

        rclone_btn_layout = QHBoxLayout()
        self._detect_btn = QPushButton("🔄 自动检测")
        self._browse_btn = QPushButton("📁 浏览...")
        self._download_btn = QPushButton("⬇ 下载 rclone")
        self._download_btn.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(_RCLONE_DOWNLOAD_URL))
        )
        rclone_btn_layout.addWidget(self._detect_btn)
        rclone_btn_layout.addWidget(self._browse_btn)
        rclone_btn_layout.addWidget(self._download_btn)
        rclone_layout.addLayout(rclone_btn_layout)

        layout.addWidget(rclone_group)

        # ── 底部菜单 ──
        bottom_layout = QHBoxLayout()
        self._config_btn = QPushButton("📋 配置管理")
        self._settings_btn = QPushButton("⚙️ 设置")
        self._logs_btn = QPushButton("📋 查看日志")
        bottom_layout.addWidget(self._config_btn)
        bottom_layout.addWidget(self._settings_btn)
        bottom_layout.addWidget(self._logs_btn)
        layout.addLayout(bottom_layout)

        layout.addStretch()

    # ── 信号连接 ─────────────────────────────────────────

    def _connect_signals(self) -> None:
        self._start_btn.clicked.connect(self._on_start)
        self._stop_btn.clicked.connect(self._on_stop)
        self._restart_btn.clicked.connect(self._on_restart)
        self._detect_btn.clicked.connect(self._on_detect_rclone)
        self._browse_btn.clicked.connect(self._on_browse_rclone)
        self._config_btn.clicked.connect(self._on_manage_configs)
        self._settings_btn.clicked.connect(self._on_open_settings)
        self._logs_btn.clicked.connect(self._on_open_logs)
        self._profile_combo.currentTextChanged.connect(self._on_profile_changed)

    # ── UI 回调 ──────────────────────────────────────────

    def update_state(self, profile_name: str, state: MountState) -> None:
        """从 Coordinator 接收状态更新。"""
        self._refresh_state(profile_name)

    def _refresh_state(self, profile_name: Optional[str] = None) -> None:
        """刷新界面状态。"""
        status = self._coordinator.query_rclone_status()

        state_str = status.get("state", "unconfigured")
        message = status.get("message", "")
        state = MountState(state_str) if state_str else MountState.UNCONFIGURED

        # 状态文本
        state_display = {
            MountState.UNCONFIGURED: "⚪ 未配置",
            MountState.CONFIGURED: "🔵 待启动",
            MountState.STARTING: "🟡 启动中...",
            MountState.MOUNTED: "🟢 已挂载",
            MountState.RESTARTING: "🟠 恢复中...",
            MountState.ERROR: "🔴 异常",
            MountState.STOPPING: "🟡 正在停止...",
            MountState.STOPPED: "⚪ 已停止",
        }
        self._status_label.setText(f"状态: {state_display.get(state, '⚪ 未知')}")

        # 状态描述
        self._message_label.setText(message if message else "")

        # 进度条
        is_transition = state in (MountState.STARTING, MountState.RESTARTING, MountState.STOPPING)
        self._progress_bar.setVisible(is_transition)

        # 按钮状态
        self._start_btn.setEnabled(state in (MountState.CONFIGURED, MountState.STOPPED, MountState.ERROR, MountState.UNCONFIGURED))
        self._stop_btn.setEnabled(state == MountState.MOUNTED)
        self._restart_btn.setEnabled(state == MountState.MOUNTED)

        # 刷新 Profile 列表
        self._refresh_profile_list()

        # rclone 状态
        self._refresh_rclone_status()

    def _refresh_profile_list(self) -> None:
        """刷新下拉框中的配置列表。"""
        current = self._profile_combo.currentText()
        self._profile_combo.blockSignals(True)
        self._profile_combo.clear()

        profiles = self._coordinator.profile_manager.profiles
        for name in profiles:
            self._profile_combo.addItem(name)

        # 恢复选中
        active = self._coordinator.profile_manager.active_profile_name
        if active and active in profiles:
            self._profile_combo.setCurrentText(active)
        elif current and current in profiles:
            self._profile_combo.setCurrentText(current)

        self._profile_combo.blockSignals(False)

        # 更新挂载信息
        profile_name = self._profile_combo.currentText()
        if profile_name:
            profile = self._coordinator.profile_manager.get_profile(profile_name)
            if profile and profile.targets:
                target = profile.targets[0]
                self._remote_label.setText(f"远程地址: {target.remote}")
                self._mount_path_label.setText(f"挂载路径: {target.mount_point}")
            else:
                self._remote_label.setText("远程地址: -")
                self._mount_path_label.setText("挂载路径: -")

    def _refresh_rclone_status(self) -> None:
        """刷新 rclone 状态显示。"""
        data = self._coordinator.config_service.read()
        rclone_path = data.get("general", {}).get("rclone_path", "")

        if rclone_path:
            version = self._coordinator.query_check_rclone_version(rclone_path)
            if version:
                self._rclone_status_label.setText(f"✅ {version}")
                self._rclone_status_label.setStyleSheet("color: #22c55e;")
            else:
                self._rclone_status_label.setText(f"⚠️ 路径存在但无法获取版本: {rclone_path}")
                self._rclone_status_label.setStyleSheet("color: #f59e0b;")
        else:
            # 自动检测
            detected = self._coordinator.query_auto_detect_rclone()
            if detected:
                self._rclone_status_label.setText(f"✅ 已自动检测: {detected}")
                self._rclone_status_label.setStyleSheet("color: #22c55e;")
            else:
                self._rclone_status_label.setText("❌ 未检测到 rclone，请指定路径或下载")
                self._rclone_status_label.setStyleSheet("color: #ef4444;")

    # ── 事件处理 ─────────────────────────────────────────

    def _on_start(self) -> None:
        profile_name = self._profile_combo.currentText()
        if not profile_name:
            QMessageBox.warning(self, "提示", "请先创建并选择一个配置")
            return

        # 检查是否有其他 rclone 在运行
        from rclone_tray.infrastructure.system import SystemService
        other_pids = SystemService.is_other_rclone_running()
        if other_pids:
            reply = QMessageBox.question(
                self,
                "检测到其他 rclone",
                f"发现其他 rclone 进程在运行 (PID: {other_pids})。\n是否尝试关闭它们？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.Yes:
                # 用户选择手动关闭
                QMessageBox.information(self, "提示", "请手动关闭其他 rclone 进程后重试")

        self._coordinator.cmd_start_mount(profile_name)

    def _on_stop(self) -> None:
        profile_name = self._profile_combo.currentText()
        if profile_name:
            self._coordinator.cmd_stop_mount(profile_name)

    def _on_restart(self) -> None:
        profile_name = self._profile_combo.currentText()
        if profile_name:
            self._coordinator.cmd_restart_mount(profile_name)

    def _on_detect_rclone(self) -> None:
        detected = self._coordinator.query_auto_detect_rclone()
        if detected:
            self._coordinator.cmd_set_rclone_path(detected)
            self._refresh_rclone_status()
            QMessageBox.information(self, "检测成功", f"已检测到 rclone:\n{detected}")
        else:
            QMessageBox.warning(self, "未检测到", "未找到 rclone，请手动指定路径或下载")

    def _on_browse_rclone(self) -> None:
        """浏览并选择 rclone.exe。"""
        from PySide6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择 rclone 可执行文件",
            "",
            "rclone (rclone.exe);;所有文件 (*.*)",
        )
        if path:
            self._coordinator.cmd_set_rclone_path(path)
            self._refresh_rclone_status()

    def _on_profile_changed(self, name: str) -> None:
        """配置选择变化。"""
        if name:
            self._coordinator.cmd_switch_profile(name)
            self._refresh_state()

    def _on_manage_configs(self) -> None:
        """打开配置管理。"""
        self._show_config_manager()

    def _show_config_manager(self) -> None:
        """显示配置管理对话框（简易版）。"""
        from PySide6.QtWidgets import (
            QDialog,
            QListWidget,
            QHBoxLayout,
            QVBoxLayout,
            QPushButton,
        )

        dialog = QDialog(self)
        dialog.setWindowTitle("配置管理")
        dialog.setMinimumWidth(400)
        dialog.setMinimumHeight(350)

        layout = QHBoxLayout(dialog)

        # 左侧: 配置列表
        list_layout = QVBoxLayout()
        list_widget = QListWidget()
        profiles = self._coordinator.profile_manager.profiles
        for name in profiles:
            list_widget.addItem(name)
        list_layout.addWidget(list_widget)
        layout.addLayout(list_layout, 1)

        # 右侧: 操作按钮
        btn_layout = QVBoxLayout()

        new_btn = QPushButton("➕ 新建")
        edit_btn = QPushButton("✏️ 编辑")
        rename_btn = QPushButton("📝 重命名")
        delete_btn = QPushButton("🗑 删除")
        close_btn = QPushButton("❌ 关闭")

        def on_new():
            dlg = ProfileDialog(parent=dialog)
            if dlg.exec() == ProfileDialog.Accepted and dlg.result_profile:
                try:
                    self._coordinator.cmd_create_profile(dlg.result_profile.name)
                    # 保存后再编辑
                    self._coordinator.profile_manager.delete_profile(dlg.result_profile.name)
                    # 重新用数据创建
                    profile = dlg.result_profile
                    pm = self._coordinator.profile_manager
                    pm._profiles[profile.name] = profile
                    pm.save()
                    list_widget.addItem(profile.name)
                    self._refresh_state()
                except Exception as exc:
                    QMessageBox.warning(dialog, "错误", str(exc))

        def on_edit():
            current = list_widget.currentItem()
            if not current:
                return
            name = current.text()
            profile = self._coordinator.profile_manager.get_profile(name)
            if profile:
                dlg = ProfileDialog(profile=profile, parent=dialog)
                if dlg.exec() == ProfileDialog.Accepted and dlg.result_profile:
                    pm = self._coordinator.profile_manager
                    pm._profiles[name] = dlg.result_profile
                    pm.save()
                    self._refresh_state()

        def on_rename():
            current = list_widget.currentItem()
            if not current:
                return
            old_name = current.text()
            from PySide6.QtWidgets import QInputDialog
            new_name, ok = QInputDialog.getText(dialog, "重命名", "新名称:", text=old_name)
            if ok and new_name:
                try:
                    self._coordinator.cmd_rename_profile(old_name, new_name)
                    current.setText(new_name)
                    self._refresh_state()
                except Exception as exc:
                    QMessageBox.warning(dialog, "错误", str(exc))

        def on_delete():
            current = list_widget.currentItem()
            if not current:
                return
            name = current.text()
            reply = QMessageBox.question(
                dialog, "确认删除", f"确定要删除配置 '{name}' 吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.Yes:
                try:
                    self._coordinator.cmd_delete_profile(name)
                    list_widget.takeItem(list_widget.row(current))
                    self._refresh_state()
                except Exception as exc:
                    QMessageBox.warning(dialog, "错误", str(exc))

        new_btn.clicked.connect(on_new)
        edit_btn.clicked.connect(on_edit)
        rename_btn.clicked.connect(on_rename)
        delete_btn.clicked.connect(on_delete)
        close_btn.clicked.connect(dialog.close)

        btn_layout.addWidget(new_btn)
        btn_layout.addWidget(edit_btn)
        btn_layout.addWidget(rename_btn)
        btn_layout.addWidget(delete_btn)
        btn_layout.addStretch()
        btn_layout.addWidget(close_btn)
        layout.addLayout(btn_layout)

        dialog.exec()

    def _on_open_settings(self) -> None:
        """打开设置窗口。"""
        data = self._coordinator.config_service.read()
        general = data.get("general", {})

        def on_save(settings: dict) -> None:
            self._coordinator.cmd_set_auto_start(settings["auto_start"])
            self._coordinator.cmd_set_minimize_to_tray(settings["minimize_to_tray"])
            self._coordinator.cmd_set_language(settings["language"])
            self._coordinator.cmd_set_theme(settings["theme"])
            self._refresh_state()

        from rclone_tray.ui.settings_window import SettingsWindow
        win = SettingsWindow(
            auto_start=SystemService.is_auto_start_enabled(),
            minimize_to_tray=general.get("minimize_to_tray", True),
            language=general.get("language", "zh-CN"),
            theme=general.get("theme", "system"),
            rclone_path=general.get("rclone_path", ""),
            on_save=on_save,
            parent=self,
        )
        win.exec()

    def _on_open_logs(self) -> None:
        """打开日志查看（用设置窗口的日志标签页）。"""
        data = self._coordinator.config_service.read()
        general = data.get("general", {})

        from rclone_tray.ui.settings_window import SettingsWindow
        win = SettingsWindow(
            auto_start=False,
            minimize_to_tray=True,
            on_save=lambda s: None,
        )
        # 加载日志
        log_path = self._coordinator.config_dir / "logs" / "rclone_tray.log"
        if log_path.exists():
            try:
                lines = log_path.read_text(encoding="utf-8").splitlines()
                win._log_edit.setText("\n".join(lines[-100:]))
            except Exception as exc:
                win._log_edit.setText(f"读取日志失败: {exc}")
        else:
            win._log_edit.setText("暂无日志")
        win.exec()


