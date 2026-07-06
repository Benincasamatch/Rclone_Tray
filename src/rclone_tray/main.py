"""Rclone Tray 主入口

启动流程:
    1. 初始化日志系统
    2. 确定配置目录
    3. 创建 ApplicationCoordinator
    4. 初始化 Coordinator
    5. 创建 QApplication
    6. 初始化托盘服务
    7. 将托盘图标注入 Coordinator（通知依赖）
    8. 创建主窗口
    9. 进入事件循环
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from rclone_tray import __app_name__, __version__
from rclone_tray.application.coordinator import ApplicationCoordinator
from rclone_tray.infrastructure.logger import get_logger, setup_logging

logger = get_logger(__name__)

# 默认配置目录
_DEFAULT_CONFIG_DIR = Path.home() / ".config" / "rclone-tray"


def determine_config_dir() -> Path:
    """确定配置目录（优先使用当前目录，否则用用户目录）。"""
    local_config = Path.cwd() / "config.toml"
    if local_config.exists():
        return Path.cwd()
    return _DEFAULT_CONFIG_DIR


def create_app() -> QApplication:
    """创建 QApplication 实例。"""
    app = QApplication(sys.argv)
    app.setApplicationName(__app_name__)
    app.setApplicationVersion(__version__)
    app.setOrganizationName("Benincasamatch")

    # 设置应用样式
    app.setStyle("Fusion")

    return app


def run() -> None:
    """启动 Rclone Tray 应用。"""
    # ── 1. 确定配置目录 & 初始化日志 ──
    config_dir = determine_config_dir()
    setup_logging(config_dir / "logs")
    logger.info("配置目录: %s", config_dir)

    # ── 2. 创建 Coordinator ──
    coordinator = ApplicationCoordinator(config_dir)
    try:
        coordinator.initialize()
    except Exception as exc:
        logger.critical("初始化失败: %s", exc, exc_info=True)
        _show_fatal_error(f"应用初始化失败:\n{exc}")
        sys.exit(1)

    # ── 3. 创建 QApplication ──
    app = create_app()

    # ── 4. 延迟创建 UI（确保 QApplication 就绪） ──
    def _start_ui() -> None:
        try:
            _create_ui(app, coordinator, config_dir)
        except Exception as exc:
            logger.critical("UI 初始化失败: %s", exc, exc_info=True)

    QTimer.singleShot(0, _start_ui)

    # ── 5. 进入事件循环 ──
    sys.exit(app.exec())


def _create_ui(app: QApplication, coordinator: ApplicationCoordinator, config_dir: Path) -> None:
    """创建所有 UI 组件。"""
    # 导入 UI 模块（延迟导入避免循环依赖）
    from rclone_tray.ui.tray import TrayService
    from rclone_tray.ui.main_window import MainWindow

    # ── 托盘 ──
    tray_service = TrayService()
    tray_icon = tray_service.initialize()

    # 将托盘图标注入 Coordinator（用于通知）
    coordinator.set_notification_tray(tray_icon)

    # ── 主窗口 ──
    main_window = MainWindow(coordinator)

    # ── 连接信号 ──
    coordinator.on_state_change(
        lambda pname, state: main_window.update_state(pname, state)
    )
    coordinator.on_state_change(
        lambda pname, state: tray_service.update_state(state, pname)
    )
    coordinator.on_state_change(
        lambda pname, state: tray_service.update_profile_list(
            coordinator.profile_manager.profile_names,
            coordinator.profile_manager.active_profile_name,
        )
    )

    # 托盘信号 → Coordinator
    tray_service.show_main_window_requested.connect(main_window.show)
    tray_service.show_main_window_requested.connect(main_window.raise_)
    tray_service.quit_requested.connect(lambda: _quit_app(app, coordinator))
    tray_service.mount_toggled.connect(coordinator.cmd_start_mount)
    tray_service.stop_requested.connect(coordinator.cmd_stop_mount)

    # ── 从配置读取窗口设置 ──
    config = coordinator.config_service.read()
    minimize_to_tray = config.get("general", {}).get("minimize_to_tray", True)

    # ── 窗口关闭行为 ──
    def _on_close(event) -> None:
        if minimize_to_tray:
            main_window.hide()
            event.ignore()  # 不关闭，隐藏到托盘
        else:
            _quit_app(app, coordinator)

    main_window.closeEvent = _on_close  # type: ignore[method-assign]

    # ── 初始刷新 ──
    main_window._refresh_state()
    tray_service.update_profile_list(
        coordinator.profile_manager.profile_names,
        coordinator.profile_manager.active_profile_name,
    )

    # 如果设置了开机启动或上次有挂载，自动恢复状态
    if coordinator.profile_manager.active_profile:
        main_window.show()
    else:
        # 首次启动显示窗口
        main_window.show()


def _quit_app(app: QApplication, coordinator: ApplicationCoordinator) -> None:
    """安全退出应用。"""
    logger.info("用户请求退出")
    coordinator.shutdown()
    app.quit()


def _show_fatal_error(message: str) -> None:
    """显示致命错误对话框。"""
    app = QApplication.instance() or QApplication(sys.argv)
    QMessageBox.critical(None, "Rclone Tray - 启动失败", message)


if __name__ == "__main__":
    run()
