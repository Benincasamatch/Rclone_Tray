"""Mount Manager - 挂载生命周期管理

职责：
- 管理挂载的启动、停止、重启流程
- 维护挂载状态机
- 对外提供状态查询接口
- 不直接操作进程（委托给 RcloneService 和 Watchdog）

状态机（对应 state machine.md）:
    configured → start → starting → success → mounted
    mounted → crash → restart → success → mounted
    restart → retry limit → error
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from rclone_tray.domain.models import MountInfo, MountState, Profile
from rclone_tray.infrastructure.logger import get_logger
from rclone_tray.infrastructure.rclone_service import RcloneService
from rclone_tray.infrastructure.watchdog import Watchdog, WatchdogEvent

logger = get_logger(__name__)


class MountManager:
    """挂载管理器 - 负责挂载的生命周期管理。"""

    def __init__(
        self,
        rclone_service: RcloneService,
        watchdog: Watchdog,
    ) -> None:
        self._rclone = rclone_service
        self._watchdog = watchdog
        self._mount_info: dict[str, MountInfo] = {}  # profile_name -> MountInfo
        self._on_state_change: Optional[list] = None

    # ── 属性 ──────────────────────────────────────────────

    @property
    def all_mount_info(self) -> dict[str, MountInfo]:
        """返回所有 Profile 的挂载信息。"""
        return dict(self._mount_info)

    def get_mount_info(self, profile_name: str) -> Optional[MountInfo]:
        """获取指定配置的挂载信息。"""
        return self._mount_info.get(profile_name)

    @property
    def is_any_mounted(self) -> bool:
        """是否有任何配置已挂载。"""
        return any(
            info.state == MountState.MOUNTED
            for info in self._mount_info.values()
        )

    # ── 核心操作 ──────────────────────────────────────────

    def start(self, profile: Profile) -> MountInfo:
        """启动指定 Profile 的所有挂载目标。

        返回 MountInfo 包含当前状态。
        """
        if not profile.targets:
            raise MountError(f"配置 '{profile.name}' 没有挂载目标")

        info = self._get_or_create_info(profile.name)
        if info.state in (MountState.STARTING, MountState.MOUNTED, MountState.RESTARTING):
            raise MountError(f"配置 '{profile.name}' 当前状态为 {info.state.value}，无法启动")

        info.state = MountState.STARTING
        info.message = "正在启动..."
        self._emit_state_change(profile.name, info.state)

        rclone_path = profile.rclone_path or "rclone"

        try:
            for target in profile.targets:
                logger.info("挂载 %s -> %s", target.remote, target.mount_point)
                self._rclone.start(
                    mount_point=target.mount_point,
                    remote=target.remote,
                    rclone_path=rclone_path,
                    extra_args=target.options.split() if target.options else None,
                )

            # 更新状态信息
            info.pids = self._rclone.get_pids()
            info.exit_codes = [
                self._rclone.get_exit_code(t.mount_point)
                for t in profile.targets
            ]

            # 启动 Watchdog 监控
            for target in profile.targets:
                self._watchdog.watch(target.mount_point)

            info.state = MountState.MOUNTED
            info.retry_count = 0
            info.message = "已挂载"
            logger.info("配置 '%s' 挂载成功", profile.name)

        except Exception as exc:
            info.state = MountState.ERROR
            info.message = f"挂载失败: {exc}"
            logger.error("配置 '%s' 挂载失败: %s", profile.name, exc)
            # 清理可能已启动的进程
            self._stop_all_targets(profile)

        self._emit_state_change(profile.name, info.state)
        return info

    def stop(self, profile_name: str) -> MountInfo:
        """停止指定配置的所有挂载。"""
        info = self._get_or_create_info(profile_name)
        if info.state == MountState.STOPPED or info.state == MountState.UNCONFIGURED:
            return info

        info.state = MountState.STOPPING
        info.message = "正在停止..."
        self._emit_state_change(profile_name, info.state)

        # 从 Watchdog 移除
        for mp in self._rclone.active_mount_points:
            self._watchdog.unwatch(mp)

        # 停止所有进程
        self._rclone.stop_all()

        info.state = MountState.STOPPED
        info.message = "已停止"
        info.pids = []
        self._emit_state_change(profile_name, info.state)
        logger.info("配置 '%s' 已停止", profile_name)
        return info

    def restart(self, profile: Profile) -> MountInfo:
        """重启指定配置。"""
        self.stop(profile.name)
        return self.start(profile)

    # ── Watchdog 事件处理 ────────────────────────────────

    def handle_watchdog_event(self, event: WatchdogEvent, mount_point: str) -> None:
        """处理 Watchdog 发出的进程事件。"""
        # 找到拥有此挂载点的 Profile
        profile_name = self._find_profile_by_mount_point(mount_point)
        if not profile_name:
            return

        info = self._get_or_create_info(profile_name)

        if event == WatchdogEvent.CRASH_DETECTED:
            info.state = MountState.RESTARTING
            info.message = f"检测到进程崩溃 ({mount_point})，正在恢复..."
            self._emit_state_change(profile_name, info.state)

        elif event == WatchdogEvent.RECOVERY_STARTED:
            info.state = MountState.RESTARTING
            info.retry_count += 1
            info.message = f"正在自动恢复 ({info.retry_count})..."
            self._emit_state_change(profile_name, info.state)

        elif event == WatchdogEvent.MAX_RETRIES_EXCEEDED:
            info.state = MountState.ERROR
            info.message = f"自动恢复失败，已超过最大重试次数 ({info.retry_count})"
            self._emit_state_change(profile_name, info.state)

        elif event == WatchdogEvent.RECOVERY_FAILED:
            info.state = MountState.ERROR
            info.message = f"自动恢复失败: {mount_point}"
            self._emit_state_change(profile_name, info.state)

    # ── 状态变更回调 ─────────────────────────────────────

    def set_on_state_change(self, callback) -> None:
        """设置状态变更回调。"""
        if self._on_state_change is None:
            self._on_state_change = []
        self._on_state_change.append(callback)

    def _emit_state_change(self, profile_name: str, state: MountState) -> None:
        """发出状态变更事件。"""
        if not self._on_state_change:
            return
        for cb in self._on_state_change:
            try:
                cb(profile_name, state)
            except Exception as exc:
                logger.error("状态变更回调异常: %s", exc)

    # ── 内部方法 ──────────────────────────────────────────

    def _get_or_create_info(self, profile_name: str) -> MountInfo:
        if profile_name not in self._mount_info:
            self._mount_info[profile_name] = MountInfo(profile_name=profile_name)
        return self._mount_info[profile_name]

    def _stop_all_targets(self, profile: Profile) -> None:
        """停止 Profile 的所有挂载目标。"""
        for target in profile.targets:
            try:
                self._rclone.stop(target.mount_point, timeout=5.0)
            except Exception as exc:
                logger.warning("停止 %s 时出错: %s", target.mount_point, exc)

    def _find_profile_by_mount_point(self, mount_point: str) -> Optional[str]:
        """根据挂载点查找所属的 Profile 名称。"""
        for name, info in self._mount_info.items():
            if info.pids and mount_point in self._rclone.active_mount_points:
                return name
        return None


# ── 异常定义 ─────────────────────────────────────────────


class MountError(Exception):
    """挂载管理基础异常。"""
