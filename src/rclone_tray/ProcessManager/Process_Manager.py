"""Process Manager 模块：负责应用程序的进程管理。

该模块提供一个泛型的 ``ProcessManager`` 类，封装单个子进程的生命周期管理：
创建（spawn）、停止（terminate → kill）、状态轮询（poll），以及 PID / 退出码 / 运行状态跟踪。

它不包含任何 rclone 特有逻辑；rclone 挂载生命周期（``LifecycleState``、``MountInfo``、
命令构建、profile 校验）由 ``RcloneService`` 负责，并通过注入本模块的 ``ProcessManager``
实例来操作底层进程。

职责：
    - 创建并启动一个子进程（start）
    - 停止子进程：优先优雅终止，超时后强制结束（stop）
    - 轮询进程状态，记录退出码（refresh）
    - 提供 PID、退出码、运行状态查询

不会负责：
    - 崩溃检测与自动重启（由 Watchdog 负责）
    - rclone 挂载生命周期与命令构建（由 RcloneService 负责）
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Callable, Sequence


@dataclass(frozen=True)  # frozen=True 对象创建后不可变
class ProcessInfo:
    """当前子进程的只读快照。"""

    pid: int | None
    exit_code: int | None
    is_running: bool


class ProcessManagerError(RuntimeError):
    """进程管理操作失败，例如重复启动。"""


class ProcessManager:
    """管理单个子进程的生命周期。

    工作流程：
        1. start() 使用给定的命令列表创建子进程。
        2. stop() 优雅终止，超时后强制结束。
        3. refresh() 同步进程退出的原始事实（退出码），不做崩溃判定。
        4. 通过 pid / exit_code / is_running / get_info() 查询当前状态。
    """

    _process: subprocess.Popen[str] | None
    _last_exit_code: int | None

    def __init__(
        self,
        process_factory: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
    ) -> None:
        """初始化 ProcessManager 实例。

        Args:
            process_factory: 创建子进程的工厂，默认 ``subprocess.Popen``，测试时可注入替身。
        """
        self._process_factory = process_factory
        self._process: subprocess.Popen[str] | None = None
        self._last_exit_code: int | None = None

    @property
    def pid(self) -> int | None:
        """返回当前进程的 PID，如果没有运行则返回 None。"""
        return self._process.pid if self._process and self._process.poll() is None else None

    @property
    def exit_code(self) -> int | None:
        """返回当前进程的退出码，如果没有退出过则返回 None。"""
        return self._last_exit_code

    @property
    def is_running(self) -> bool:
        """返回当前是否有一个正在运行的进程。"""
        return self._process is not None and self._process.poll() is None

    def start(self, command: Sequence[str]) -> ProcessInfo:
        """使用给定命令启动子进程。

        Args:
            command: 要执行的命令列表（argv）。

        Raises:
            ProcessManagerError: 已有一个进程在运行时再次调用 start。
            OSError / subprocess.SubprocessError: 底层创建进程失败时向上传播。
        """
        if self.is_running:
            raise ProcessManagerError("process is already running")

        self._process = self._process_factory(
            list(command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        self._last_exit_code = None
        return self.get_info()

    def stop(self, timeout: float = 5.0) -> ProcessInfo:
        """停止当前进程，超时后强制结束。"""
        process = self._process
        if process is None or process.poll() is not None:
            return self.get_info()

        try:
            process.terminate()
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        self._last_exit_code = process.returncode
        return self.get_info()

    def refresh(self) -> ProcessInfo:
        """同步进程状态。

        仅记录进程退出的原始事实（退出码），不做崩溃判定；
        崩溃识别与自动重启由上层（如 Watchdog）负责。
        """
        process = self._process
        if process is not None:
            return_code = process.poll()
            if return_code is not None and self._last_exit_code is None:
                self._last_exit_code = return_code
        return self.get_info()

    def get_info(self) -> ProcessInfo:
        """返回进程状态的只读快照。"""
        return ProcessInfo(
            pid=self.pid,
            exit_code=self._last_exit_code,
            is_running=self.is_running,
        )


"""
Public API
    ProcessManager:管理单个子进程的生命周期
    ProcessInfo:当前子进程的只读快照
    ProcessManagerError:表示进程管理操作失败

使用方法：
    1. 创建 ProcessManager 实例（默认使用 subprocess.Popen；测试时可注入 process_factory）。
    2. 调用 start(command) 启动子进程。
    3. 调用 stop(timeout) 停止，超时后强制结束。
    4. 使用 refresh() 同步状态，使用 pid / exit_code / is_running / get_info() 查询。
    5. 崩溃检测与自动重启由上层模块（如 Watchdog）负责。
"""
__all__ = [
    "ProcessInfo",
    "ProcessManager",
    "ProcessManagerError",
]