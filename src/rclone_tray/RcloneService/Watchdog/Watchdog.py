"""
watchdog:监测进程状态
rclone service的守护进程模块，负责监测rclone进程的状态，并在进程异常退出时尝试重新启动。

根据profile给定的当前rclone路径，监测rclone进程的状态.rclone service启动进程后，该模块开始持续监控当前挂载信息，包括挂载点、文件系统类型等。

若进程异常退出，watchdog会尝试重新启动rclone进程，并记录重启次数。若重启次数超过设定的最大值，则会停止尝试重启，并记录错误信息。

负责任务：
    crash detection
    Restarting the process if it crashes
    Retry count

本模块通过包装一个 ``RcloneService`` 实例，把崩溃检测、自动重启与重试计数从
``Rclone_Service`` 中剥离出来，使 ``RcloneService`` 专注于生命周期控制与状态查询。
"""

from __future__ import annotations

import time

from MountManager.Mount_Manager import LifecycleState, MountInfo
from RcloneService.Rclone_Service import RcloneService


class WatchdogError(RuntimeError):
    """Watchdog 操作失败，例如超过最大重试次数。"""


class Watchdog:
    r"""监控 ``RcloneService`` 进程，崩溃时自动重启，并限制重试次数。

    职责：
        - crash detection：检测底层 rclone 进程是否意外退出。
        - restart：检测到崩溃后自动停止并重新启动底层服务。
        - retry count：记录重启次数，超过 ``max_retries`` 后停止尝试。

    状态机（参考 docs/state machine.md）：
        mounted --crash--> restart --success--> mounted
                                \--retry limit--> error
    """

    def __init__(
        self,
        service: RcloneService,
        *,
        max_retries: int = 3,
        retry_delay: float = 1.0,
        check_interval: float = 0.5,
    ) -> None:
        """初始化 Watchdog 实例。

        Args:
            service: 被监控的 RcloneService 实例。
            max_retries: 崩溃后最大自动重启次数，达到后停止尝试。
            retry_delay: 每次重启前的等待秒数。
            check_interval: 监控循环的检测间隔秒数。
        """
        if max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        self._service = service
        self._max_retries = max_retries
        self._retry_delay = max(0.0, retry_delay)
        self._check_interval = max(0.0, check_interval)
        self._retry_count = 0
        self._monitoring = False

    @property
    def service(self) -> RcloneService:
        """返回被监控的 RcloneService 实例。"""
        return self._service

    @property
    def retry_count(self) -> int:
        """返回当前已重试次数。"""
        return self._retry_count

    @property
    def max_retries(self) -> int:
        """返回最大允许的重试次数。"""
        return self._max_retries

    @property
    def is_monitoring(self) -> bool:
        """返回是否正在监控。"""
        return self._monitoring

    def check_crash(self) -> bool:
        """检测底层进程是否意外退出（crash detection）。

        判定规则：底层服务处于应运行状态（starting / mounted / restart）但进程
        已退出（pid 为 None）时，视为崩溃。主动停止后状态为 stopped，不视为崩溃。
        """
        info = self._service.get_mount_info()
        expected_running = info.state in (
            LifecycleState.STARTING,
            LifecycleState.MOUNTED,
            LifecycleState.RESTARTING,
        )
        return expected_running and info.pid is None

    def restart(self) -> MountInfo:
        """停止并重新启动底层服务，记录重试次数。

        若已超过最大重试次数则抛出 :class:`WatchdogError`，并停止尝试。
        """
        if self._retry_count >= self._max_retries:
            raise WatchdogError(
                f"rclone crashed more than {self._max_retries} times; giving up"
            )
        self._retry_count += 1
        try:
            self._service.stop()
        except Exception:
            pass
        if self._retry_delay > 0:
            time.sleep(self._retry_delay)
        return self._service.start()

    def run(self) -> None:
        """阻塞式监控循环。

        持续检测崩溃并自动重启，直到调用 :meth:`stop` 或超过最大重试次数。
        超过重试上限时抛出 :class:`WatchdogError` 并停止监控。
        """
        self._monitoring = True
        try:
            while self._monitoring:
                if self.check_crash():
                    if self._retry_count >= self._max_retries:
                        self._monitoring = False
                        raise WatchdogError(
                            f"rclone crashed more than {self._max_retries} times; giving up"
                        )
                    self.restart()
                time.sleep(self._check_interval)
        finally:
            self._monitoring = False

    def stop(self) -> None:
        """请求停止监控（主动停止，不触发自动重启）。"""
        self._monitoring = False


"""
Public API
    Watchdog:监控 RcloneService 进程，崩溃时自动重启，并限制重试次数
    WatchdogError:表示 Watchdog 操作失败（如超过最大重试次数）

使用方法：
    1. 创建 RcloneService 实例并 start() 启动挂载。
    2. 创建 Watchdog 实例并传入该 service，设置 max_retries 等参数。
    3. 调用 run() 开始阻塞式监控（可放入后台线程），崩溃后自动重启。
    4. 需要停止时调用 stop()；超过重试上限时 run() 抛出 WatchdogError。
    5. 也可直接调用 check_crash() / restart() 由上层编排监控节奏。
"""
__all__ = [
    "Watchdog",
    "WatchdogError",
]


