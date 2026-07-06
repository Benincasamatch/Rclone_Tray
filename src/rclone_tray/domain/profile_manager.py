"""Profile Manager - 配置管理

职责：
- 管理所有 Profile（新增/删除/修改/切换/导入/导出）
- 提供统一的 Profile 入口
- 依赖 ConfigService 持久化

对外接口:
    load_profiles()
    save_profiles()
    get_profile(name)
    create_profile(name)
    delete_profile(name)
    rename_profile(old_name, new_name)
    switch_profile(name)
    import_profiles(data)
    export_profiles()
"""

from __future__ import annotations

from typing import Any, Optional

from rclone_tray.domain.models import Profile, RemoteTarget
from rclone_tray.infrastructure.config_service import ConfigService
from rclone_tray.infrastructure.logger import get_logger

logger = get_logger(__name__)


class ProfileManager:
    """配置管理器 - 负责 Profile 的 CRUD 与切换。"""

    def __init__(self, config_service: ConfigService) -> None:
        self._config = config_service
        self._profiles: dict[str, Profile] = {}
        self._active_profile: Optional[str] = None

    # ── 属性 ──────────────────────────────────────────────

    @property
    def profiles(self) -> dict[str, Profile]:
        """获取所有 Profile（只读视图）。"""
        return dict(self._profiles)

    @property
    def profile_names(self) -> list[str]:
        """返回所有 Profile 名称列表。"""
        return list(self._profiles.keys())

    @property
    def active_profile_name(self) -> Optional[str]:
        """当前激活的 Profile 名称。"""
        return self._active_profile

    @property
    def active_profile(self) -> Optional[Profile]:
        """当前激活的 Profile 对象。"""
        if self._active_profile and self._active_profile in self._profiles:
            return self._profiles[self._active_profile]
        return None

    # ── 生命周期 ──────────────────────────────────────────

    def load(self) -> None:
        """从配置文件加载所有 Profile。"""
        data = self._config.read()
        profiles_data = data.get("profiles", {})
        self._profiles.clear()

        for name, profile_data in profiles_data.items():
            try:
                profile = self._dict_to_profile(name, profile_data)
                self._profiles[name] = profile
            except (KeyError, TypeError) as exc:
                logger.warning("跳过无效配置 '%s': %s", name, exc)

        # 恢复上次激活的配置
        self._active_profile = data.get("general", {}).get("active_profile")
        if self._active_profile and self._active_profile not in self._profiles:
            logger.warning("上次激活的配置 '%s' 不存在，重置", self._active_profile)
            self._active_profile = None

        logger.info("已加载 %d 个配置", len(self._profiles))

    def save(self) -> None:
        """将所有 Profile 持久化到配置文件。"""
        data = self._config.read()
        data["profiles"] = {name: self._profile_to_dict(p) for name, p in self._profiles.items()}
        if self._active_profile:
            data.setdefault("general", {})["active_profile"] = self._active_profile
        self._config.write(data)
        logger.info("配置已保存 (%d 个)", len(self._profiles))

    # ── CRUD ─────────────────────────────────────────────

    def create_profile(self, name: str) -> Profile:
        """创建一个新配置。"""
        if not name or not name.strip():
            raise ProfileError("配置名称不能为空")
        if name in self._profiles:
            raise ProfileExistsError(f"配置 '{name}' 已存在")

        profile = Profile(name=name.strip())
        self._profiles[profile.name] = profile
        logger.info("创建配置: %s", profile.name)
        return profile

    def get_profile(self, name: str) -> Optional[Profile]:
        """获取指定配置。"""
        return self._profiles.get(name)

    def delete_profile(self, name: str) -> bool:
        """删除配置。"""
        if name not in self._profiles:
            raise ProfileNotFoundError(f"配置 '{name}' 不存在")
        if name == self._active_profile:
            self._active_profile = None
        del self._profiles[name]
        logger.info("删除配置: %s", name)
        return True

    def rename_profile(self, old_name: str, new_name: str) -> Profile:
        """重命名配置。"""
        if old_name not in self._profiles:
            raise ProfileNotFoundError(f"配置 '{old_name}' 不存在")
        if new_name in self._profiles:
            raise ProfileExistsError(f"配置 '{new_name}' 已存在")

        profile = self._profiles.pop(old_name)
        profile.name = new_name
        self._profiles[new_name] = profile

        if self._active_profile == old_name:
            self._active_profile = new_name

        logger.info("重命名配置: %s -> %s", old_name, new_name)
        return profile

    def switch_profile(self, name: str) -> Profile:
        """切换当前激活的配置。"""
        if name not in self._profiles:
            raise ProfileNotFoundError(f"配置 '{name}' 不存在")
        self._active_profile = name
        logger.info("切换配置: %s", name)
        return self._profiles[name]

    # ── 导入/导出 ────────────────────────────────────────

    def export_profiles(self) -> dict[str, Any]:
        """导出所有配置为可序列化字典。"""
        return {
            name: self._profile_to_dict(p)
            for name, p in self._profiles.items()
        }

    def import_profiles(self, data: dict[str, Any], merge: bool = False) -> int:
        """从字典导入配置。

        Args:
            data: 配置数据字典
            merge: 是否与现有配置合并

        Returns:
            导入的配置数量
        """
        count = 0
        for name, profile_data in data.items():
            if name in self._profiles and not merge:
                logger.warning("跳过已存在的配置: %s", name)
                continue
            try:
                profile = self._dict_to_profile(name, profile_data)
                self._profiles[name] = profile
                count += 1
            except (KeyError, TypeError) as exc:
                logger.warning("导入配置 '%s' 失败: %s", name, exc)
        logger.info("导入 %d 个配置", count)
        return count

    # ── 序列化 ────────────────────────────────────────────

    @staticmethod
    def _profile_to_dict(profile: Profile) -> dict[str, Any]:
        return {
            "rclone_path": profile.rclone_path or "",
            "targets": [
                {
                    "remote": t.remote,
                    "mount_point": t.mount_point,
                    "options": t.options,
                }
                for t in profile.targets
            ],
        }

    @staticmethod
    def _dict_to_profile(name: str, data: dict[str, Any]) -> Profile:
        targets = [
            RemoteTarget(
                remote=t["remote"],
                mount_point=t["mount_point"],
                options=t.get("options", ""),
            )
            for t in data.get("targets", [])
        ]
        return Profile(
            name=name,
            targets=targets,
            rclone_path=data.get("rclone_path", ""),
        )


# ── 异常定义 ─────────────────────────────────────────────


class ProfileError(Exception):
    """配置管理器基础异常。"""


class ProfileExistsError(ProfileError):
    """配置已存在。"""


class ProfileNotFoundError(ProfileError):
    """配置未找到。"""
