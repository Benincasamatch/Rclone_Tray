"""Config Dialog - 配置编辑弹窗

功能：
- 新建/编辑 Profile
- 添加/删除挂载目标
- 测试连接（预留）
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from rclone_tray.domain.models import Profile, RemoteTarget


class TargetEditWidget(QWidget):
    """单个挂载目标编辑器。"""

    def __init__(self, target: Optional[RemoteTarget] = None, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._target = target or RemoteTarget(remote="", mount_point="")
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QFormLayout(self)

        self._remote_edit = QLineEdit(self._target.remote)
        self._remote_edit.setPlaceholderText("例如: remote:path 或 remote:folder")
        layout.addRow("远程地址:", self._remote_edit)

        self._mount_edit = QLineEdit(self._target.mount_point)
        self._mount_edit.setPlaceholderText("例如: Z: 或 C:\\Mount\\rclone")
        layout.addRow("挂载位置:", self._mount_edit)

        self._options_edit = QLineEdit(self._target.options)
        self._options_edit.setPlaceholderText("例如: --vfs-cache-mode writes")
        layout.addRow("额外参数:", self._options_edit)

    def get_target(self) -> RemoteTarget:
        return RemoteTarget(
            remote=self._remote_edit.text().strip(),
            mount_point=self._mount_edit.text().strip(),
            options=self._options_edit.text().strip(),
        )

    def is_valid(self) -> bool:
        return bool(self._remote_edit.text().strip() and self._mount_edit.text().strip())


class ProfileDialog(QDialog):
    """Profile 编辑对话框（新建/编辑）。"""

    def __init__(self, profile: Optional[Profile] = None, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._profile = profile
        self._target_widgets: list[TargetEditWidget] = []
        self._result_profile: Optional[Profile] = None
        self._setup_ui()

        if profile:
            self.setWindowTitle(f"编辑配置 - {profile.name}")
            self._name_edit.setText(profile.name)
            self._rclone_path_edit.setText(profile.rclone_path)
            for target in profile.targets:
                self._add_target(target)
        else:
            self.setWindowTitle("新建配置")

    def _setup_ui(self) -> None:
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)

        # ── 基本信息 ──
        basic_group = QGroupBox("基本信息")
        basic_layout = QFormLayout(basic_group)

        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("配置名称（例如: Office NAS）")
        basic_layout.addRow("配置名称:", self._name_edit)

        self._rclone_path_edit = QLineEdit()
        self._rclone_path_edit.setPlaceholderText("留空则使用全局 rclone 路径")
        basic_layout.addRow("rclone 路径:", self._rclone_path_edit)

        layout.addWidget(basic_group)

        # ── 挂载目标列表 ──
        targets_group = QGroupBox("挂载目标")
        targets_layout = QVBoxLayout(targets_group)

        self._targets_list = QListWidget()
        targets_layout.addWidget(self._targets_list)

        btn_layout = QHBoxLayout()
        self._add_btn = QPushButton("➕ 添加目标")
        self._add_btn.clicked.connect(lambda: self._add_target())
        self._remove_btn = QPushButton("➖ 删除选中")
        self._remove_btn.clicked.connect(self._remove_target)
        btn_layout.addWidget(self._add_btn)
        btn_layout.addWidget(self._remove_btn)
        btn_layout.addStretch()
        targets_layout.addLayout(btn_layout)

        layout.addWidget(targets_group)

        # ── 按钮 ──
        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        button_box.accepted.connect(self._on_accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

    def _add_target(self, target: Optional[RemoteTarget] = None) -> None:
        """添加一个挂载目标编辑区域。"""
        widget = TargetEditWidget(target)
        self._target_widgets.append(widget)

        item = QListWidgetItem()
        item.setSizeHint(widget.sizeHint())
        self._targets_list.addItem(item)
        self._targets_list.setItemWidget(item, widget)

    def _remove_target(self) -> None:
        """删除选中的挂载目标。"""
        current_row = self._targets_list.currentRow()
        if current_row < 0:
            return

        self._targets_list.takeItem(current_row)
        self._target_widgets.pop(current_row)

    def _on_accept(self) -> None:
        """确认保存。"""
        name = self._name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "提示", "请输入配置名称")
            return

        targets = [w.get_target() for w in self._target_widgets if w.is_valid()]
        if not targets:
            QMessageBox.warning(self, "提示", "请至少添加一个有效的挂载目标")
            return

        self._result_profile = Profile(
            name=name,
            targets=targets,
            rclone_path=self._rclone_path_edit.text().strip(),
        )
        self.accept()

    @property
    def result_profile(self) -> Optional[Profile]:
        return self._result_profile
