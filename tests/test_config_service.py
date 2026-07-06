"""Config Service 测试。"""

import tempfile
from pathlib import Path

import pytest

from rclone_tray.infrastructure.config_service import (
    ConfigService,
    DEFAULT_CONFIG,
)


class TestConfigService:
    """ConfigService 单元测试。"""

    @pytest.fixture
    def config_dir(self) -> Path:
        with tempfile.TemporaryDirectory() as tmp:
            yield Path(tmp)

    def test_read_default_when_not_exists(self, config_dir: Path) -> None:
        """配置文件不存在时返回默认配置。"""
        svc = ConfigService(config_dir)
        data = svc.read()
        assert "general" in data
        assert "profiles" in data
        assert data["general"]["language"] == "zh-CN"

    def test_write_and_read(self, config_dir: Path) -> None:
        """写入后能正确读取。"""
        svc = ConfigService(config_dir)
        data = svc.read()
        data["general"]["language"] = "en"
        data["profiles"]["test"] = {"targets": []}
        svc.write(data)

        # 重新读取
        svc2 = ConfigService(config_dir)
        loaded = svc2.read()
        assert loaded["general"]["language"] == "en"
        assert "test" in loaded["profiles"]

    def test_backup_and_restore(self, config_dir: Path) -> None:
        """备份与恢复。"""
        svc = ConfigService(config_dir)
        data = svc.read()
        data["general"]["language"] = "en"
        svc.write(data)

        backup_path = svc.backup()
        assert backup_path.exists()

        # 修改后恢复
        data2 = svc.read()
        data2["general"]["language"] = "ja"
        svc.write(data2)

        svc.restore(backup_path)
        restored = svc.read()
        assert restored["general"]["language"] == "en"

    def test_reset_to_defaults(self, config_dir: Path) -> None:
        """重置为默认配置。"""
        svc = ConfigService(config_dir)
        data = svc.read()
        data["general"]["language"] = "en"
        svc.write(data)

        defaults = svc.reset_to_defaults()
        assert defaults["general"]["language"] == "zh-CN"
