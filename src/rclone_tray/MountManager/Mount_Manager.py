"""Mount Manager:管理 rclone 挂载信息的只读快照。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Mapping

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


@dataclass(frozen=True)
class MountInfo:
	"""当前进程和挂载配置的只读快照。"""

	state: LifecycleState
	profile_id: str | None
	remote: str | None
	mount_point: str | None
	pid: int | None
	exit_code: int | None


ProfileProvider = Callable[[], Mapping[str, Any] | None]
StateProvider = Callable[[], LifecycleState]


class MountManager:
	"""组装并返回 rclone 挂载的只读快照。"""

	def __init__(
		self,
		profile_provider: ProfileProvider,
		state_provider: StateProvider,
		process_manager: ProcessManager,
	) -> None:
		self._profile_provider = profile_provider
		self._state_provider = state_provider
		self._process_manager = process_manager

	def get_mount_info(self) -> MountInfo:
		"""返回完整只读状态快照。"""
		profile = self._profile_provider()
		return MountInfo(
			state=self._state_provider(),
			profile_id=self._value(profile, "id"),
			remote=self._value(profile, "rclone_route"),
			mount_point=self._value(profile, "mount-drive"),
			pid=self._process_manager.pid,
			exit_code=self._process_manager.exit_code,
		)

	def check_status(self) -> MountInfo:
		"""刷新调用方看到的快照并返回当前挂载状态。"""
		return self.get_mount_info()

	@staticmethod
	def _value(profile: Mapping[str, Any] | None, key: str) -> str | None:
		"""从 Profile 中获取指定键的值，如果不存在则返回 None。"""
		value = profile.get(key) if profile else None
		return str(value) if value is not None else None

"""
Public API:
    LifecycleState:枚举，表示 rclone 挂载的生命周期状态
    MountInfo:数据类，表示 rclone 挂载的只读快照
    MountManager:管理 rclone 挂载信息的只读快照
"""
__all__ = [
	"LifecycleState",
	"MountInfo",
	"MountManager",
]