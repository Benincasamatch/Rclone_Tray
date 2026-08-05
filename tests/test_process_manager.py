"""ProcessManager 的单元测试。

测试使用模拟进程，不会真正启动子进程。

运行方式：
    python -m unittest -v ProcessManager.test_process_manager
    （需要将 src/rclone_tray 加入 sys.path 或 PYTHONPATH）
"""

from __future__ import annotations

import subprocess
import unittest
from typing import Any

from ProcessManager.Process_Manager import (
    ProcessManager,
    ProcessManagerError,
)


class FakeProcess:
    """用于测试的最小 Popen 替身。"""

    _next_pid = 3000

    def __init__(self, return_code: int | None = None) -> None:
        """初始化模拟进程。"""
        self.pid = FakeProcess._next_pid
        FakeProcess._next_pid += 1
        self.returncode = return_code
        self.command: list[str] = []
        self.kwargs: dict[str, Any] = {}
        self.terminate_called = False
        self.kill_called = False
        self.wait_called = False

    def poll(self) -> int | None:
        """返回模拟进程的退出代码，如果尚未退出，则返回 None。"""
        return self.returncode

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
        self.wait_called = True
        if self.returncode is None:
            raise subprocess.TimeoutExpired("fake-process", timeout or 0.0)
        return self.returncode


class ProcessManagerTests(unittest.TestCase):
    """验证 ProcessManager 的核心生命周期功能。"""

    COMMAND = ["rclone.exe", "mount", "remote:/data", "R:"]

    def setUp(self) -> None:
        """设置测试环境。"""
        self.created_processes: list[FakeProcess] = []

    def make_factory(self, *, return_code: int | None = None):
        """创建一个模拟进程工厂函数。"""

        def factory(command: list[str], **kwargs: Any) -> FakeProcess:
            process = FakeProcess(return_code=return_code)
            process.command = command
            process.kwargs = kwargs
            self.created_processes.append(process)
            return process

        return factory

    def make_manager(self, *, return_code: int | None = None) -> ProcessManager:
        """创建一个 ProcessManager 实例，使用模拟进程工厂。"""
        return ProcessManager(
            process_factory=self.make_factory(return_code=return_code)
        )

    def test_initial_state(self) -> None:
        """测试初始状态：无 PID、无退出码、未运行。"""
        manager = self.make_manager()

        self.assertIsNone(manager.pid)
        self.assertIsNone(manager.exit_code)
        self.assertFalse(manager.is_running)

    def test_start_spawns_process_with_command(self) -> None:
        """测试 start 用给定命令创建进程并报告运行状态。"""
        manager = self.make_manager()

        info = manager.start(self.COMMAND)

        process = self.created_processes[0]
        self.assertEqual(process.command, list(self.COMMAND))
        self.assertIsNotNone(process.kwargs.get("stdin"))
        self.assertTrue(process.kwargs.get("text"))
        self.assertEqual(info.pid, process.pid)
        self.assertTrue(info.is_running)
        self.assertIsNone(info.exit_code)
        self.assertEqual(manager.pid, process.pid)
        self.assertTrue(manager.is_running)

    def test_start_rejects_second_running_process(self) -> None:
        """测试已运行时再次 start 会被拒绝。"""
        manager = self.make_manager()
        manager.start(self.COMMAND)

        with self.assertRaisesRegex(ProcessManagerError, "already running"):
            manager.start(self.COMMAND)

        self.assertEqual(len(self.created_processes), 1)

    def test_stop_terminates_and_records_exit_code(self) -> None:
        """测试 stop 优雅终止进程并记录退出码。"""
        manager = self.make_manager()
        manager.start(self.COMMAND)
        process = self.created_processes[0]

        info = manager.stop()

        self.assertTrue(process.terminate_called)
        self.assertTrue(process.wait_called)
        self.assertEqual(info.exit_code, 0)
        self.assertFalse(info.is_running)
        self.assertIsNone(info.pid)
        self.assertEqual(manager.exit_code, 0)

    def test_stop_timeout_falls_back_to_kill(self) -> None:
        """测试 stop 超时后强制 kill。"""
        manager = self.make_manager()
        manager.start(self.COMMAND)
        process = self.created_processes[0]

        # 让 terminate 后进程仍未退出，使 wait 抛出 TimeoutExpired
        def hanging_terminate() -> None:
            process.terminate_called = True  # 不修改 returncode

        process.terminate = hanging_terminate

        info = manager.stop(timeout=0.1)

        self.assertTrue(process.terminate_called)
        self.assertTrue(process.kill_called)
        self.assertEqual(info.exit_code, -9)
        self.assertFalse(info.is_running)

    def test_stop_when_not_running_returns_current_info(self) -> None:
        """测试未运行时调用 stop 不报错并返回当前信息。"""
        manager = self.make_manager()

        info = manager.stop()

        self.assertFalse(info.is_running)
        self.assertEqual(len(self.created_processes), 0)

    def test_refresh_records_exit_code_without_crash_decision(self) -> None:
        """测试 refresh 仅记录退出码，不做崩溃判定。"""
        manager = self.make_manager(return_code=7)
        manager.start(self.COMMAND)

        info = manager.refresh()

        self.assertEqual(info.exit_code, 7)
        self.assertFalse(info.is_running)
        self.assertEqual(manager.exit_code, 7)
        self.assertIsNone(manager.pid)


if __name__ == "__main__":
    unittest.main(verbosity=2)
