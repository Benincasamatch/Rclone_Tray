"""RcloneService 的单元测试。

测试使用模拟进程，不会真正启动 rclone，也不会修改真实挂载点。

运行方式：
    python -m unittest -v RcloneService.test_rclone_service
"""

from __future__ import annotations

import subprocess
import unittest
from typing import Any


from ProcessManager.Process_Manager import ProcessManager
from RcloneService.Rclone_Service import (
    LifecycleState,
    RcloneService,
    RcloneServiceError,
)


class FakeProcess:
    """用于测试的最小 Popen 替身。"""

    _next_pid = 1000

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
            raise subprocess.TimeoutExpired("fake-rclone", timeout or 0.0)
        return self.returncode


class RcloneServiceTests(unittest.TestCase):
    """验证 RcloneService 的核心生命周期功能。"""

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
            process.command = command
            process.kwargs = kwargs
            self.created_processes.append(process)
            return process

        return factory

    def make_service(self, *, profile: dict[str, str] | None = None, **kwargs: Any) -> RcloneService:
        """创建一个 RcloneService 实例，注入使用模拟进程工厂的 ProcessManager。"""
        process_manager = kwargs.pop("process_manager", None) or ProcessManager(
            process_factory=kwargs.pop("process_factory", self.make_factory())
        )
        return RcloneService(
            executable="rclone.exe",
            profile_provider=lambda: profile if profile is not None else self.PROFILE,
            process_manager=process_manager,
            **kwargs,
        )

    def test_initial_state_is_stopped(self) -> None:
        """测试初始状态是已停止。"""
        service = self.make_service()

        self.assertEqual(service.state, LifecycleState.STOPPED)
        self.assertIsNone(service.get_pid())
        self.assertIsNone(service.get_exit_code())

    def test_start_builds_command_and_reports_mounted(self) -> None:
        """测试启动服务时构建命令并报告已挂载状态。"""
        states: list[LifecycleState] = []
        service = self.make_service(extra_args=("--vfs-cache-mode", "full"))
        service.add_state_listener(lambda info: states.append(info.state))

        info = service.start()

        process = self.created_processes[0]
        self.assertEqual(
            process.command,
            [
                "rclone.exe",
                "mount",
                "remote:/data",
                "R:",
                "--vfs-cache-mode",
                "full",
            ],
        )
        self.assertEqual(info.state, LifecycleState.MOUNTED)
        self.assertEqual(info.profile_id, "home")
        self.assertEqual(info.remote, "remote:/data")
        self.assertEqual(info.mount_point, "R:")
        self.assertEqual(info.pid, process.pid)
        self.assertEqual(states, [LifecycleState.STARTING, LifecycleState.MOUNTED])

    def test_start_rejects_second_running_process(self) -> None:
        """测试启动第二个运行中的进程会被拒绝。"""
        service = self.make_service()
        service.start()

        with self.assertRaisesRegex(RcloneServiceError, "already running"):
            service.start()

    def test_stop_terminates_process_and_records_exit_code(self) -> None:
        """测试停止服务时终止进程并记录退出代码。"""
        service = self.make_service()
        service.start()

        info = service.stop()
        process = self.created_processes[0]

        self.assertTrue(process.terminate_called)
        self.assertTrue(process.wait_called)
        self.assertEqual(info.state, LifecycleState.STOPPED)
        self.assertEqual(info.exit_code, 0)
        self.assertIsNone(info.pid)

    def test_refresh_status_records_exit_code_without_crash_decision(self) -> None:
        """测试剥离后 refresh_status 仅记录退出码，崩溃决策交由 Watchdog。"""
        errors: list[Exception] = []
        process = FakeProcess(return_code=7)
        service = self.make_service(
            process_factory=lambda command, **kwargs: process,
        )
        service.add_error_listener(errors.append)
        service.start()

        state = service.refresh_status()

        # 只同步退出码，不判定崩溃（不置 ERROR、不通知错误）
        self.assertEqual(service.get_exit_code(), 7)
        self.assertEqual(len(errors), 0)
        # 状态保持 MOUNTED（僵尸状态），由 Watchdog 通过 pid 检测崩溃
        self.assertEqual(state, LifecycleState.MOUNTED)
        self.assertIsNone(service.get_pid())

    def test_invalid_profile_is_rejected(self) -> None:
        """测试无效的配置文件会被拒绝。"""
        service = self.make_service(profile={"id": "invalid"})

        with self.assertRaisesRegex(RcloneServiceError, "rclone_route"):
            service.start()

        self.assertEqual(len(self.created_processes), 0)

    def test_process_start_failure_is_reported(self) -> None:
        """测试进程启动失败时会被报告。"""
        errors: list[Exception] = []

        def failing_factory(command: list[str], **kwargs: Any) -> FakeProcess:
            """模拟进程工厂，始终引发 OSError，表示 rclone 可执行文件未找到。"""
            raise OSError("rclone executable not found")

        service = self.make_service(process_factory=failing_factory)
        service.add_error_listener(errors.append)

        with self.assertRaisesRegex(RcloneServiceError, "Unable to start rclone"):
            service.start()

        self.assertEqual(service.state, LifecycleState.ERROR)
        self.assertEqual(len(errors), 1)
        self.assertIn("not found", str(errors[0]))

    def test_listeners_can_be_removed(self) -> None:
        """测试可以移除状态监听器。"""
        states: list[LifecycleState] = []
        service = self.make_service()
        listener = lambda info: states.append(info.state)
        service.add_state_listener(listener)
        service.remove_state_listener(listener)

        service.start()

        self.assertEqual(states, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
