"""Logger - 日志系统

职责：
- 提供统一的日志记录接口
- 支持运行日志 (runtime) 和调试日志 (debug) 分离
- 日志轮转，避免单文件过大
- 支持按级别过滤

使用方式:
    from rclone_tray.infrastructure.logger import get_logger
    logger = get_logger(__name__)
    logger.info("挂载成功")
    logger.debug("启动参数: %s", args)
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

# 模块级状态
_loggers: dict[str, logging.Logger] = {}
_initialized = False

# 日志目录
LOG_DIR = "logs"
RUNTIME_LOG = "rclone_tray.log"
DEBUG_LOG = "rclone_tray_debug.log"

# 格式
_RUNTIME_FORMAT = "%(asctime)s [%(levelname)-7s] %(message)s"
_DEBUG_FORMAT = (
    "%(asctime)s [%(levelname)-7s] %(name)s(%(filename)s:%(lineno)d) - %(message)s"
)
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging(
    log_dir: str | Path = LOG_DIR,
    runtime_level: int = logging.INFO,
    debug_level: int = logging.DEBUG,
    max_bytes: int = 5 * 1024 * 1024,
    backup_count: int = 3,
) -> None:
    """初始化日志系统。

    Args:
        log_dir: 日志文件目录
        runtime_level: 运行日志级别
        debug_level: 调试日志级别
        max_bytes: 单日志文件最大字节数
        backup_count: 轮转备份文件数
    """
    global _initialized
    if _initialized:
        return

    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)  # 由 handler 控制级别

    # 清除已有 handler（防止重复初始化）
    root_logger.handlers.clear()

    # ── Runtime 日志 (INFO 及以上) ──
    runtime_handler = RotatingFileHandler(
        log_path / RUNTIME_LOG,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    runtime_handler.setLevel(runtime_level)
    runtime_handler.setFormatter(logging.Formatter(_RUNTIME_FORMAT, _DATE_FORMAT))
    root_logger.addHandler(runtime_handler)

    # ── Debug 日志 (DEBUG 及以上) ──
    debug_handler = RotatingFileHandler(
        log_path / DEBUG_LOG,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    debug_handler.setLevel(debug_level)
    debug_handler.setFormatter(logging.Formatter(_DEBUG_FORMAT, _DATE_FORMAT))
    root_logger.addHandler(debug_handler)

    # ── 控制台输出 (仅 debug 构建) ──
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.WARNING)
    console_handler.setFormatter(logging.Formatter(_DEBUG_FORMAT, _DATE_FORMAT))
    root_logger.addHandler(console_handler)

    _initialized = True
    logging.info("日志系统初始化完成 (runtime=%s, debug=%s)",
                  logging.getLevelName(runtime_level),
                  logging.getLevelName(debug_level))


def get_logger(name: str) -> logging.Logger:
    """获取命名日志器。"""
    return logging.getLogger(name)


def get_runtime_log_path(log_dir: str | Path = LOG_DIR) -> Path:
    """返回运行日志文件的路径。"""
    return Path(log_dir) / RUNTIME_LOG


def get_debug_log_path(log_dir: str | Path = LOG_DIR) -> Path:
    """返回调试日志文件的路径。"""
    return Path(log_dir) / DEBUG_LOG
