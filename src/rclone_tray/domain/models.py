"""Domain Models - 领域模型

定义核心数据结构和枚举，不包含业务逻辑。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional


class MountState(Enum):
    """挂载状态机（对应 state machine.md）。"""

    UNCONFIGURED = "unconfigured"       # 未配置
    CONFIGURED = "configured"           # 已配置，待启动
    STARTING = "starting"               # 启动中
    MOUNTED = "mounted"                 # 已挂载
    RESTARTING = "restarting"           # 自动恢复中
    ERROR = "error"                     # 错误/异常
    STOPPING = "stopping"               # 正在停止
    STOPPED = "stopped"                 # 已停止

    def is_active(self) -> bool:
        """是否处于活动状态（非终态）。"""
        return self in (MountState.STARTING, MountState.MOUNTED, MountState.RESTARTING, MountState.STOPPING)

    def is_transient(self) -> bool:
        """是否处于过渡状态。"""
        return self in (MountState.STARTING, MountState.RESTARTING, MountState.STOPPING)


class Theme(Enum):
    """主题。"""
    SYSTEM = "system"
    LIGHT = "light"
    DARK = "dark"


@dataclass
class RemoteTarget:
    """远程挂载目标。

    Attributes:
        remote: 远程地址 (如 remote:path)
        mount_point: 本地挂载点 (如 Z:)
        options: 额外挂载参数
    """
    remote: str
    mount_point: str
    options: str = ""


@dataclass
class Profile:
    """挂载配置。

    一个 Profile 可以包含多个 RemoteTarget（多目标挂载）。
    """
    name: str
    targets: list[RemoteTarget] = field(default_factory=list)
    rclone_path: str = ""  # 覆盖全局 rclone 路径

    def add_target(self, remote: str, mount_point: str, options: str = "") -> None:
        """添加挂载目标。"""
        self.targets.append(RemoteTarget(remote=remote, mount_point=mount_point, options=options))

    def remove_target(self, mount_point: str) -> bool:
        """移除指定挂载点的目标。"""
        for i, t in enumerate(self.targets):
            if t.mount_point == mount_point:
                self.targets.pop(i)
                return True
        return False


@dataclass
class MountInfo:
    """挂载信息（运行时状态）。"""
    profile_name: str
    state: MountState = MountState.UNCONFIGURED
    pids: list[int] = field(default_factory=list)       # rclone 进程 PID 列表
    exit_codes: list[int | None] = field(default_factory=list)  # 退出码列表
    current_target_index: int = 0
    retry_count: int = 0
    max_retries: int = 3
    message: str = ""  # 状态描述信息

    @property
    def is_mounted(self) -> bool:
        return self.state == MountState.MOUNTED

    @property
    def is_healthy(self) -> bool:
        return self.state in (MountState.MOUNTED, MountState.CONFIGURED)


@dataclass
class AppConfig:
    """应用配置（对应 config.toml 的结构）。"""
    language: str = "zh-CN"
    theme: str = "system"
    minimize_to_tray: bool = True
    auto_start: bool = False
    rclone_path: str = ""
