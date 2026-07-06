"""Profile Manager 测试。"""

import tempfile
from pathlib import Path

import pytest

from rclone_tray.domain.profile_manager import (
    ProfileExistsError,
    ProfileManager,
    ProfileNotFoundError,
)
from rclone_tray.infrastructure.config_service import ConfigService


class TestProfileManager:
    """ProfileManager 单元测试。"""

    @pytest.fixture
    def manager(self) -> ProfileManager:
        with tempfile.TemporaryDirectory() as tmp:
            config = ConfigService(Path(tmp))
            pm = ProfileManager(config)
            pm.load()
            yield pm

    def test_create_profile(self, manager: ProfileManager) -> None:
        """创建配置。"""
        profile = manager.create_profile("Test")
        assert profile.name == "Test"
        assert "Test" in manager.profiles

    def test_create_duplicate(self, manager: ProfileManager) -> None:
        """重复创建应抛出异常。"""
        manager.create_profile("Test")
        with pytest.raises(ProfileExistsError):
            manager.create_profile("Test")

    def test_delete_profile(self, manager: ProfileManager) -> None:
        """删除配置。"""
        manager.create_profile("Test")
        assert manager.delete_profile("Test") is True
        assert "Test" not in manager.profiles

    def test_delete_not_found(self, manager: ProfileManager) -> None:
        """删除不存在的配置应抛出异常。"""
        with pytest.raises(ProfileNotFoundError):
            manager.delete_profile("NonExistent")

    def test_rename_profile(self, manager: ProfileManager) -> None:
        """重命名配置。"""
        manager.create_profile("Old")
        profile = manager.rename_profile("Old", "New")
        assert profile.name == "New"
        assert "Old" not in manager.profiles
        assert "New" in manager.profiles

    def test_switch_profile(self, manager: ProfileManager) -> None:
        """切换配置。"""
        manager.create_profile("A")
        manager.create_profile("B")

        manager.switch_profile("A")
        assert manager.active_profile_name == "A"

        manager.switch_profile("B")
        assert manager.active_profile_name == "B"

    def test_export_import(self, manager: ProfileManager) -> None:
        """导入导出。"""
        profile = manager.create_profile("Test")
        profile.add_target("remote:path", "Z:")

        exported = manager.export_profiles()
        assert "Test" in exported

        # 新建一个 manager 导入
        with tempfile.TemporaryDirectory() as tmp:
            config2 = ConfigService(Path(tmp))
            pm2 = ProfileManager(config2)
            pm2.load()
            count = pm2.import_profiles(exported)
            assert count == 1
            assert "Test" in pm2.profiles
