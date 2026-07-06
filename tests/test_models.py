"""Domain Models 测试。"""

from rclone_tray.domain.models import MountState, Profile, RemoteTarget


class TestMountState:
    """MountState 枚举测试。"""

    def test_is_active(self) -> None:
        assert MountState.STARTING.is_active()
        assert MountState.MOUNTED.is_active()
        assert not MountState.ERROR.is_active()
        assert not MountState.STOPPED.is_active()

    def test_is_transient(self) -> None:
        assert MountState.STARTING.is_transient()
        assert not MountState.MOUNTED.is_transient()
        assert not MountState.ERROR.is_transient()


class TestProfile:
    """Profile 数据类测试。"""

    def test_add_target(self) -> None:
        profile = Profile(name="Test")
        profile.add_target("remote:path", "Z:")
        assert len(profile.targets) == 1
        assert profile.targets[0].remote == "remote:path"
        assert profile.targets[0].mount_point == "Z:"

    def test_remove_target(self) -> None:
        profile = Profile(name="Test")
        profile.add_target("r1", "Z:")
        profile.add_target("r2", "Y:")
        assert profile.remove_target("Z:") is True
        assert len(profile.targets) == 1
        assert profile.targets[0].mount_point == "Y:"

    def test_remove_target_not_found(self) -> None:
        profile = Profile(name="Test")
        assert profile.remove_target("Z:") is False
