"""Settings Window - 设置窗口

功能：
- 开机自启动
- 最小化到托盘
- 多语言切换
- 主题切换
- 查看日志
- 关于信息
"""

from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from rclone_tray import __version__


class SettingsWindow(QDialog):
    """应用设置窗口。"""

    def __init__(
        self,
        *,
        auto_start: bool = False,
        minimize_to_tray: bool = True,
        language: str = "zh-CN",
        theme: str = "system",
        rclone_path: str = "",
        on_save: Optional[Callable] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._on_save = on_save
        self._setup_ui()

        # 初始化值
        self._auto_start_cb.setChecked(auto_start)
        self._minimize_cb.setChecked(minimize_to_tray)
        self._lang_combo.setCurrentText(self._lang_to_display(language))
        self._theme_combo.setCurrentText(self._theme_to_display(theme))
        self._rclone_path_label.setText(rclone_path or "未设置（将自动检测）")

        self.setWindowTitle("设置")
        self.setMinimumWidth(550)
        self.setMinimumHeight(400)

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        tabs = QTabWidget()

        # ── 常规 ──
        general_tab = QWidget()
        general_layout = QVBoxLayout(general_tab)

        general_group = QGroupBox("启动与窗口")
        general_form = QFormLayout(general_group)

        self._auto_start_cb = QCheckBox("开机自动启动 Rclone Tray")
        general_form.addRow("", self._auto_start_cb)

        self._minimize_cb = QCheckBox("关闭窗口时最小化到托盘（而非退出）")
        general_form.addRow("", self._minimize_cb)

        general_layout.addWidget(general_group)

        appearance_group = QGroupBox("外观")
        appearance_form = QFormLayout(appearance_group)

        self._lang_combo = QComboBox()
        self._lang_combo.addItems(["中文", "English"])
        appearance_form.addRow("语言:", self._lang_combo)

        self._theme_combo = QComboBox()
        self._theme_combo.addItems(["跟随系统", "浅色", "深色"])
        appearance_form.addRow("主题:", self._theme_combo)

        general_layout.addWidget(appearance_group)
        general_layout.addStretch()
        tabs.addTab(general_tab, "⚙️ 常规")

        # ── rclone ──
        rclone_tab = QWidget()
        rclone_layout = QVBoxLayout(rclone_tab)

        rclone_group = QGroupBox("rclone 位置")
        rclone_form = QFormLayout(rclone_group)

        self._rclone_path_label = QLabel("检测中...")
        rclone_form.addRow("当前路径:", self._rclone_path_label)

        auto_detect_btn = QPushButton("🔄 自动检测")
        auto_detect_btn.clicked.connect(lambda: self._on_auto_detect())
        rclone_form.addRow("", auto_detect_btn)

        rclone_layout.addWidget(rclone_group)
        rclone_layout.addStretch()
        tabs.addTab(rclone_tab, "💾 rclone")

        # ── 日志 ──
        log_tab = QWidget()
        log_layout = QVBoxLayout(log_tab)

        self._log_edit = QTextEdit()
        self._log_edit.setReadOnly(True)
        self._log_edit.setStyleSheet("font-family: Consolas, 'Courier New', monospace; font-size: 11px;")
        log_layout.addWidget(QLabel("运行日志（最近 100 行）:"))

        btn_layout = QHBoxLayout()
        refresh_btn = QPushButton("🔄 刷新日志")
        refresh_btn.clicked.connect(self._load_logs)
        clear_btn = QPushButton("🗑 清空显示")
        clear_btn.clicked.connect(self._log_edit.clear)
        btn_layout.addWidget(refresh_btn)
        btn_layout.addWidget(clear_btn)
        btn_layout.addStretch()
        log_layout.addLayout(btn_layout)
        log_layout.addWidget(self._log_edit)

        tabs.addTab(log_tab, "📋 日志")

        # ── 关于 ──
        about_tab = QWidget()
        about_layout = QVBoxLayout(about_tab)
        about_layout.addWidget(QLabel(f"<h2>Rclone Tray</h2>"))
        about_layout.addWidget(QLabel(f"版本: {__version__}"))
        about_layout.addWidget(QLabel("一个用于管理 rclone 挂载的 Windows 系统托盘工具。"))
        about_layout.addWidget(QLabel(
            '<a href="https://github.com/Benincasamatch/Rclone_Tray">GitHub 仓库</a>'
        ))
        about_layout.addStretch()
        tabs.addTab(about_tab, "ℹ️ 关于")

        layout.addWidget(tabs)

        # ── 底部按钮 ──
        btn_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        btn_box.accepted.connect(self._on_save_settings)
        btn_box.rejected.connect(self.reject)
        layout.addWidget(btn_box)

    def _on_auto_detect(self) -> None:
        """自动检测 rclone（通过回调）。"""
        if self._on_save:
            self._on_save(self._get_settings())

    def _on_save_settings(self) -> None:
        """保存设置。"""
        if self._on_save:
            self._on_save(self._get_settings())
        self.accept()

    def _get_settings(self) -> dict:
        return {
            "auto_start": self._auto_start_cb.isChecked(),
            "minimize_to_tray": self._minimize_cb.isChecked(),
            "language": self._display_to_lang(self._lang_combo.currentText()),
            "theme": self._display_to_theme(self._theme_combo.currentText()),
        }

    @staticmethod
    def _lang_to_display(lang: str) -> str:
        return {"zh-CN": "中文", "en": "English"}.get(lang, "中文")

    @staticmethod
    def _display_to_lang(display: str) -> str:
        return {"中文": "zh-CN", "English": "en"}.get(display, "zh-CN")

    @staticmethod
    def _theme_to_display(theme: str) -> str:
        return {"system": "跟随系统", "light": "浅色", "dark": "深色"}.get(theme, "跟随系统")

    @staticmethod
    def _display_to_theme(display: str) -> str:
        return {"跟随系统": "system", "浅色": "light", "深色": "dark"}.get(display, "system")
