"""Rclone 挂载生命周期服务。

该模块负责启动和管理一个 rclone mount 进程。Profile 数据默认从 ``ProfileManager``读取，也可以通过 ``profile_provider`` 注入，便于测试和后续解耦。

泛型的子进程生命周期管理（创建、停止、轮询、PID/退出码跟踪）已剥离至 ``ProcessManager``，
本模块通过注入的 ``ProcessManager`` 实例操作底层进程，专注于 rclone 特有的
profile 校验、命令构建、生命周期状态与事件通知。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import os
from pathlib import Path
import subprocess
from typing import Any, Callable, Mapping, Optional, Sequence

from ProcessManager.Process_Manager import ProcessManager


class LifecycleState(str, Enum):
    """rclone mount 的生命周期状态。"""

    CONFIGURED = "configured"
    STARTING = "starting"
    MOUNTED = "mounted"
    STOPPING = "stopping"
    STOPPED = "stopped"
    RESTARTING = "restart"
    ERROR = "error"


@dataclass(frozen=True)# frozen=True 对象创建后不可变
class MountInfo:
    """当前进程和挂载配置的只读快照。"""

    state: LifecycleState
    profile_id: str | None
    remote: str | None
    mount_point: str | None
    pid: int | None
    exit_code: int | None


class RcloneServiceError(RuntimeError): # 表示 Rclone 服务操作失败
    """Rclone 服务操作失败。"""

ProfileProvider = Callable[[], Optional[Mapping[str, Any]]] # 不接受参数，返回当前的 Profile 字典或 None
StateListener = Callable[[MountInfo], None] # 状态监听器，接受 MountInfo 对象作为参数，无返回值
ErrorListener = Callable[[Exception], None] # 错误监听器，接受 Exception 对象作为参数，无返回值
"""使用typing.Callable定义函数类型，指定参数和返回值类型。"""


class RcloneService:
    """管理 rclone mount 子进程及其生命周期事件。"""

    _listeners: list[StateListener]
    _error_listeners: list[ErrorListener]
    _process_manager: ProcessManager
    _profile: Mapping[str, Any] | None
    _state: LifecycleState

    """
    工作流程：
    1. 从当前 Profile 读取 rclone 配置。
    2. 通过 ProcessManager 启动 rclone mount 子进程。
    3. 通过 ProcessManager 停止该子进程。
    4. 保存当前挂载状态、PID 和退出码。
    5. 通过监听器通知外部代码状态变化或错误。

    注意：泛型子进程生命周期已剥离至 ProcessManager；
    进程崩溃检测、自动重启与重试计数已剥离至 Watchdog 模块。
    """

    def __init__(
        self,
        executable: str | os.PathLike[str] = "rclone",
        profile_provider: ProfileProvider | None = None,
        *,
        extra_args: Sequence[str] = (),
        process_manager: ProcessManager | None = None,
    ) -> None:
        """初始化 RcloneService 实例。"""
        self.executable = str(executable)
        self._profile_provider = profile_provider or self._load_current_profile
        self._extra_args = tuple(str(arg) for arg in extra_args)
        self._process_manager = process_manager or ProcessManager()
        self._profile: Mapping[str, Any] | None = None
        self._state = LifecycleState.STOPPED
        self._listeners: list[StateListener] = []
        self._error_listeners: list[ErrorListener] = []

    @staticmethod # 静态方法，不依赖于类实例
    def _load_current_profile() -> Mapping[str, Any] | None:
        """从 ProfileManager 获取当前 Profile。"""
        from ProfileManager.Profile_Manager import get_current_profile

        return get_current_profile()

    def add_state_listener(self, listener: StateListener) -> None:
        """添加状态监听器。"""
        if listener not in self._listeners:
            self._listeners.append(listener)

    def remove_state_listener(self, listener: StateListener) -> None:
        """移除状态监听器。"""
        if listener in self._listeners:
            self._listeners.remove(listener)

    def add_error_listener(self, listener: ErrorListener) -> None:
        """添加错误监听器。"""
        if listener not in self._error_listeners:
            self._error_listeners.append(listener)

    def remove_error_listener(self, listener: ErrorListener) -> None:
        """移除错误监听器。"""
        if listener in self._error_listeners:
            self._error_listeners.remove(listener)

    @property
    def state(self) -> LifecycleState:
        """返回当前生命周期状态。"""
        self.refresh_status()
        return self._state

    def get_pid(self) -> int | None:
        """返回当前 rclone 进程的 PID，如果没有运行则返回 None。"""
        self.refresh_status()
        return self._process_manager.pid

    def get_exit_code(self) -> int | None:
        """返回当前 rclone 进程的退出码，如果没有运行则返回 None。"""
        self.refresh_status()
        return self._process_manager.exit_code

    def get_mount_info(self) -> MountInfo:
        """返回完整只读状态快照，包括
            生命周期状态
            Profile ID
            rclone 配置
            挂载点
            进程 PID
            退出码
        """
        self.refresh_status()
        profile = self._profile
        return MountInfo(
            state=self._state,
            profile_id=self._value(profile, "id"),
            remote=self._value(profile, "rclone_route"),
            mount_point=self._value(profile, "mount-drive"),
            pid=self._process_manager.pid,
            exit_code=self._process_manager.exit_code,
        )

    def check_status(self) -> MountInfo:
        """刷新并返回当前挂载状态。"""
        return self.get_mount_info()

    def start(self) -> MountInfo:
        """使用当前 Profile 启动 rclone mount。
        主要流程：
            1. 检查当前是否已有 rclone mount 进程在运行。
            2. 获取当前 Profile 并验证其合法性。
            3. 构建 rclone mount 命令并启动子进程。
                组装命令：rclone mount <rclone_route> <mount-drive> <extra_args>
        """
        self.refresh_status()
        if self._process_manager.is_running:
            raise RcloneServiceError("rclone mount is already running")

        profile = self._profile_provider()
        self._validate_profile(profile)
        # Normalize profile to a mapping[str, Any] or None to satisfy type checkers
        if profile is None:
            self._profile = None
        else:
            # Ensure keys are str (some sources may provide bytes keys)
            self._profile = {str(k): v for k, v in profile.items()}
        self._set_state(LifecycleState.STARTING)
        # Use the normalized self._profile which is guaranteed to be a mapping or None
        if self._profile is None:
            err = RcloneServiceError("no profile available to start rclone")
            self._notify_error(err)
            self._set_state(LifecycleState.ERROR)
            raise err

        command = [
            self.executable,
            "mount",
            str(self._profile.get("rclone_route")),
            str(self._profile.get("mount-drive")),
            *self._extra_args,
        ]
        try:
            self._process_manager.start(command)
        except (OSError, subprocess.SubprocessError) as exc:
            self._notify_error(exc)
            self._set_state(LifecycleState.ERROR)
            raise RcloneServiceError(f"Unable to start rclone: {exc}") from exc

        self._set_state(LifecycleState.MOUNTED)
        return self.get_mount_info()

    def stop(self, timeout: float = 5.0) -> MountInfo:
        """停止当前 rclone 进程，超时后强制结束。"""
        self.refresh_status()
        if not self._process_manager.is_running:
            self._set_state(LifecycleState.STOPPED)
            return self.get_mount_info()

        self._set_state(LifecycleState.STOPPING)
        self._process_manager.stop(timeout=timeout)
        self._set_state(LifecycleState.STOPPED)
        return self.get_mount_info()

    def refresh_status(self) -> LifecycleState:
        """同步子进程状态。

        仅负责更新进程退出的原始事实（退出码），不做崩溃判定。
        进程意外退出（crash detection）的识别与自动重启由 Watchdog 负责。
        """
        info = self._process_manager.refresh()
        # 主动停止流程：正常过渡到 STOPPED；否则保持原状态，由 Watchdog 判定崩溃
        if self._state == LifecycleState.STOPPING and not info.is_running:
            self._set_state(LifecycleState.STOPPED)
        return self._state

    @staticmethod
    def _value(profile: Mapping[str, Any] | None, key: str) -> str | None:
        """从 Profile 中获取指定键的值，如果不存在则返回 None。"""
        value = profile.get(key) if profile else None
        return str(value) if value is not None else None

    @staticmethod
    def _validate_profile(profile: Mapping[str, Any] | None) -> None:
        """验证 Profile 是否包含必要的字段。"""
        if not profile:
            raise RcloneServiceError("No current profile is configured")
        for field in ("rclone_route", "mount-drive"):
            if not isinstance(profile.get(field), str) or not profile[field].strip():
                raise RcloneServiceError(f"Profile field '{field}' is required")

    def _set_state(self, state: LifecycleState) -> None:
        """设置当前生命周期状态并通知所有状态监听器。"""
        self._state = state
        info = MountInfo(
            state=state,
            profile_id=self._value(self._profile, "id"),
            remote=self._value(self._profile, "rclone_route"),
            mount_point=self._value(self._profile, "mount-drive"),
            pid=self._process_manager.pid,
            exit_code=self._process_manager.exit_code,
        )
        for listener in tuple(self._listeners):
            listener(info)

    def _notify_error(self, error: Exception) -> None:
        """通知所有错误监听器。"""
        for listener in tuple(self._error_listeners):
            listener(error)


"""
Public API
    LifecycleState:rclone mount 的生命周期状态
    MountInfo:当前进程和挂载配置的只读快照
    RcloneService:管理 rclone mount 子进程及其生命周期事件
    RcloneServiceError:表示 rclone 服务相关错误的异常类

使用方法：
    1. 创建 RcloneService 实例，指定 rclone 可执行文件路径、可选的 profile_provider 和 process_manager。
    2. 使用 add_state_listener 和 add_error_listener 注册状态和错误监听器。
    3. 调用 start() 启动 rclone mount，stop() 停止挂载。
    4. 使用 get_mount_info() 获取当前挂载信息，使用 refresh_status() 同步子进程状态。
    5. 子进程生命周期由 ProcessManager 负责，崩溃检测、自动重启与重试计数由 Watchdog 模块负责。
"""
__all__ = [
    "LifecycleState",
    "MountInfo",
    "RcloneService",
    "RcloneServiceError",
]
