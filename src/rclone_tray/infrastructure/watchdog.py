"""Watchdog - rclone 进程监控与自动恢复

职责：
- 周期性检查 rclone 进程状态
- 检测崩溃并触发自动重启
- 重试计数，超过上限后转为 ERROR 状态
- 发出状态变更事件

事件:
    "mount_crashed"    - 挂载进程崩溃
    "mount_recovered"  - 自动恢复成功
    "mount_failed"     - 超过重试上限
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Callable, Optional

from rclone_tray.infrastructure.logger import get_logger
from rclone_tray.infrastructure.rclone_service import RcloneService

logger = get_logger(__name__)

# 默认监控间隔（秒）
_DEFAULT_INTERVAL = 5.0
_DEFAULT_MAX_RETRIES = 3


class WatchdogEvent(str, Enum):
    """Watchdog 事件类型。"""
    CRASH_DETECTED = "crash_detected"
    RECOVERY_STARTED = "recovery_started"
    RECOVERY_SUCCEEDED = "recovery_succeeded"
    RECOVERY_FAILED = "recovery_failed"
    MAX_RETRIES_EXCEEDED = "max_retries_exceeded"
    PROCESS_STOPPED = "process_stopped"


@dataclass
class WatchdogState:
    """Watchdog 状态。"""
    mount_point: str
    running: bool = False
    crash_count: int = 0
    retry_count: int = 0
    max_retries: int = _DEFAULT_MAX_RETRIES
    last_error: str = ""


class Watchdog:
    """进程看门狗。

    启动后定期检查所有受监控的 rclone 进程。
    检测到崩溃后自动执行恢复流程。
    """

    def __init__(
        self,
        rclone_service: RcloneService,
        check_interval: float = _DEFAULT_INTERVAL,
        event_callback: Optional[Callable[[WatchdogEvent, str], None]] = None,
    ) -> None:
        self._rclone = rclone_service
        self._interval = check_interval
        self._event_callback = event_callback
        self._watched: dict[str, WatchdogState] = {}
        self._lock = threading.Lock()
        self._timer: Optional[threading.Timer] = None
        self._running = False

    # ── 属性 ──────────────────────────────────────────────

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def watched_mount_points(self) -> list[str]:
        with self._lock:
            return list(self._watched.keys())

    @property
    def states(self) -> dict[str, WatchdogState]:
        with self._lock:
            return dict(self._watched)

    # ── 生命周期 ──────────────────────────────────────────

    def watch(self, mount_point: str, max_retries: int = _DEFAULT_MAX_RETRIES) -> None:
        """开始监控指定挂载点。"""
        with self._lock:
            if mount_point not in self._watched:
                self._watched[mount_point] = WatchdogState(
                    mount_point=mount_point,
                    max_retries=max_retries,
                    running=True,
                )
                logger.info("Watchdog 开始监控: %s (最大重试: %d)", mount_point, max_retries)

    def unwatch(self, mount_point: str) -> None:
        """停止监控指定挂载点。"""
        with self._lock:
            self._watched.pop(mount_point, None)
            logger.info("Watchdog 停止监控: %s", mount_point)

    def start(self) -> None:
        """启动监控循环。"""
        if self._running:
            return
        self._running = True
        logger.info("Watchdog 已启动 (检查间隔: %.1fs)", self._interval)
        self._check_loop()

    def stop(self) -> None:
        """停止监控循环。"""
        self._running = False
        if self._timer:
            self._timer.cancel()
            self._timer = None
        logger.info("Watchdog 已停止")

    def reset_retry_count(self, mount_point: str) -> None:
        """重置指定挂载点的重试计数。"""
        with self._lock:
            state = self._watched.get(mount_point)
            if state:
                state.retry_count = 0
                state.crash_count = 0
                state.last_error = ""

    # ── 内部: 检查循环 ───────────────────────────────────

    def _check_loop(self) -> None:
        if not self._running:
            return

        try:
            self._check_all()
        except Exception as exc:
            logger.error("Watchdog 检查异常: %s", exc, exc_info=True)

        self._timer = threading.Timer(self._interval, self._check_loop)
        self._timer.daemon = True
        self._timer.start()

    def _check_all(self) -> None:
        """检查所有受监控进程的状态。"""
        with self._lock:
            mount_points = list(self._watched.keys())

        for mp in mount_points:
            self._check_process(mp)

    def _check_process(self, mount_point: str) -> None:
        """检查单个挂载点进程。"""
        pid = self._rclone.get_pid(mount_point)
        with self._lock:
            state = self._watched.get(mount_point)
            if not state:
                return

            if pid is not None:
                # 进程正常运行
                state.running = True
                return

            # 进程不在了
            if not state.running:
                return  # 已经在处理中

            # 检测到崩溃
            state.running = False
            exit_code = self._rclone.get_exit_code(mount_point)
            state.crash_count += 1
            state.retry_count += 1
            state.last_error = f"进程退出 (exit_code: {exit_code})"

            logger.warning(
                "检测到进程崩溃: %s (退出码: %s, 重试: %d/%d)",
                mount_point, exit_code, state.retry_count, state.max_retries,
            )

            self._emit_event(WatchdogEvent.CRASH_DETECTED, mount_point)

            if state.retry_count > state.max_retries:
                logger.error("超过最大重试次数: %s", mount_point)
                self._emit_event(WatchdogEvent.MAX_RETRIES_EXCEEDED, mount_point)
                return

            # 触发自动恢复
            self._emit_event(WatchdogEvent.RECOVERY_STARTED, mount_point)

    def _emit_event(self, event: WatchdogEvent, mount_point: str) -> None:
        """发出 Watchdog 事件。"""
        if self._event_callback:
            try:
                self._event_callback(event, mount_point)
            except Exception as exc:
                logger.error("Watchdog 事件回调异常: %s", exc)
