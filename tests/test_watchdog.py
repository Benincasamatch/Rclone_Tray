"""Watchdog 的单元测试。

测试使用模拟进程，不会真正启动 rclone。

运行方式：
    python -m unittest -v RcloneService.Watchdog.test_watchdog
"""

from __future__ import annotations

import subprocess
import threading
import time
import unittest
from typing import Any

from ProcessManager.Process_Manager import ProcessManager
from RcloneService.Rclone_Service import LifecycleState, RcloneService
from RcloneService.Watchdog.Watchdog import Watchdog, WatchdogError


class FakeProcess:
    """用于测试的最小 Popen 替身。"""

    _next_pid = 2000

    def __init__(self, return_code: int | None = None) -> None:
        """初始化模拟进程。"""
        self.pid = FakeProcess._next_pid
        FakeProcess._next_pid += 1
        self.returncode = return_code
        self.terminate_called = False
        self.kill_called = False

    def poll(self) -> int | None:
        """返回模拟进程的退出代码，如果尚未退出，则返回 None。"""
        return self.returncode

    def crash(self, code: int = 7) -> None:
        """模拟进程意外崩溃。"""
        self.returncode = code

    def terminate(self) -> None:
        """模拟终止进程。"""
        self.terminate_called = True
        self.returncode = 0

    def kill(self) -> None:
        """模拟杀死进程。"""
        self.kill_called = True
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        """模拟等待进程退出。"""
        if self.returncode is None:
            raise subprocess.TimeoutExpired("fake-rclone", timeout or 0.0)
        return self.returncode


class WatchdogTests(unittest.TestCase):
    """验证 Watchdog 的崩溃检测、自动重启与重试计数。"""

    PROFILE = {
        "id": "home",
        "rclone_route": "remote:/data",
        "mount-drive": "R:",
    }

    def setUp(self) -> None:
        """设置测试环境。"""
        self.created_processes: list[FakeProcess] = []

    def make_factory(self, *, return_code: int | None = None):
        """创建一个模拟进程工厂函数。"""

        def factory(command: list[str], **kwargs: Any) -> FakeProcess:
            process = FakeProcess(return_code=return_code)
            self.created_processes.append(process)
            return process

        return factory

    def make_watchdog(
        self,
        *,
        max_retries: int = 3,
        retry_delay: float = 0.0,
        check_interval: float = 0.01,
        process_return_code: int | None = None,
    ) -> tuple[Watchdog, RcloneService]:
        """创建 Watchdog 与包装的 RcloneService（使用模拟进程工厂）。"""
        service = RcloneService(
            executable="rclone.exe",
            profile_provider=lambda: self.PROFILE,
            process_manager=ProcessManager(
                process_factory=self.make_factory(  # type: ignore[arg-type]
                    return_code=process_return_code
                )
            ),
        )
        watchdog = Watchdog(
            service,
            max_retries=max_retries,
            retry_delay=retry_delay,
            check_interval=check_interval,
        )
        return watchdog, service

    def test_check_crash_detects_unexpected_exit(self) -> None:
        """测试 check_crash 检测到意外退出。"""
        watchdog, service = self.make_watchdog(process_return_code=7)
        service.start()

        self.assertTrue(watchdog.check_crash())

    def test_check_crash_false_while_running(self) -> None:
        """测试进程正常运行时 check_crash 返回 False。"""
        watchdog, service = self.make_watchdog()
        service.start()

        self.assertFalse(watchdog.check_crash())

    def test_check_crash_false_after_manual_stop(self) -> None:
        """测试主动停止后不视为崩溃。"""
        watchdog, service = self.make_watchdog()
        service.start()
        service.stop()

        self.assertFalse(watchdog.check_crash())
        self.assertEqual(watchdog.retry_count, 0)
        self.assertEqual(service.state, LifecycleState.STOPPED)

    def test_restart_starts_new_process_and_increments_retry(self) -> None:
        """测试 restart 启动新进程并递增重试次数。"""
        watchdog, service = self.make_watchdog()
        service.start()
        first_process = self.created_processes[0]

        info = watchdog.restart()

        self.assertEqual(len(self.created_processes), 2)
        self.assertNotEqual(info.pid, first_process.pid)
        self.assertEqual(watchdog.retry_count, 1)
        self.assertEqual(info.state, LifecycleState.MOUNTED)

    def test_max_retries_reached_raises(self) -> None:
        """测试超过最大重试次数后抛出 WatchdogError。"""
        watchdog, service = self.make_watchdog(max_retries=2)
        watchdog.restart()
        watchdog.restart()

        with self.assertRaisesRegex(WatchdogError, "giving up"):
            watchdog.restart()

        self.assertEqual(watchdog.retry_count, 2)

    def test_run_auto_restarts_until_stop(self) -> None:
        """测试 run() 监控循环检测崩溃并自动重启，直到 stop()。"""
        watchdog, service = self.make_watchdog()
        service.start()
        first_process = self.created_processes[0]

        errors: list[Exception] = []

        def target() -> None:
            try:
                watchdog.run()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        try:
            first_process.crash(7)  # 模拟崩溃
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and watchdog.retry_count < 1:
                time.sleep(0.01)

            self.assertEqual(watchdog.retry_count, 1)
            self.assertEqual(len(self.created_processes), 2)
        finally:
            watchdog.stop()
            thread.join(timeout=2.0)

        self.assertFalse(watchdog.is_monitoring)
        self.assertEqual(errors, [])

    def test_run_stops_after_max_retries(self) -> None:
        """测试 run() 超过重试上限后抛出 WatchdogError 并停止监控。"""
        watchdog, service = self.make_watchdog(max_retries=1, process_return_code=7)
        service.start()  # 启动即崩溃

        errors: list[Exception] = []

        def target() -> None:
            try:
                watchdog.run()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and len(errors) == 0:
                time.sleep(0.01)
        finally:
            watchdog.stop()
            thread.join(timeout=2.0)

        self.assertFalse(watchdog.is_monitoring)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], WatchdogError)
        self.assertIn("giving up", str(errors[0]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
