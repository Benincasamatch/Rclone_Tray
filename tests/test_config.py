"""Consumer-visible persistence invariants; isolated from user files."""
from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.config import (AppConfig, ConfigError, MountConfig, config_checksum,
                         config_path, list_remotes, load_config, save_config)


class ConfigBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="rclone-config-regression-")
        self.environment = patch.dict(os.environ, {"RCLONE_TRAY_DATA_DIR": self.directory.name})
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.directory.cleanup()

    def test_corrupt_file_is_not_replaced_by_defaults(self):
        original = b'{"mounts": [invalid]}'
        config_path().write_bytes(original)
        with self.assertRaises(ConfigError):
            load_config()
        with self.assertRaises(ConfigError):
            save_config(AppConfig())
        self.assertEqual(config_path().read_bytes(), original)

    def test_external_change_rejects_stale_save(self):
        cfg = AppConfig(theme="light")
        save_config(cfg, expected_checksum=None)
        baseline = config_checksum()
        external = copy.deepcopy(cfg)
        external.theme = "dark"
        save_config(external)
        external_bytes = config_path().read_bytes()
        cfg.mount_delay_seconds = 99
        with self.assertRaises(ConfigError):
            save_config(cfg, expected_checksum=baseline)
        self.assertEqual(config_path().read_bytes(), external_bytes)
        self.assertEqual(load_config().theme, "dark")

    def test_atomic_replace_failure_keeps_prior_configuration(self):
        save_config(AppConfig(theme="light"))
        original = config_path().read_bytes()
        with patch("core.config.os.replace", side_effect=PermissionError("locked")):
            with self.assertRaises(ConfigError):
                save_config(AppConfig(theme="dark"))
        self.assertEqual(config_path().read_bytes(), original)
        self.assertEqual(load_config().theme, "light")

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI")
    def test_legacy_password_migration_preserves_mount_identity(self):
        secret = "migration-only fake password / 中文"
        mount = MountConfig.new("sample", "sample:folder", "Y:")
        legacy = {"gui_no_auth": False, "gui_user": "tester", "gui_pass": secret,
                  "mounts": [mount.__dict__], "theme": "dark"}
        config_path().write_text(json.dumps(legacy), encoding="utf-8")
        original = config_path().read_bytes()
        cfg = load_config()
        self.assertEqual(cfg.gui_pass, secret)
        self.assertEqual(cfg.mounts[0].id, mount.id)
        self.assertEqual(config_path().read_bytes(), original)
        save_config(cfg, expected_checksum=config_checksum())
        encoded = config_path().read_text(encoding="utf-8")
        self.assertNotIn(secret, encoded)
        restored = load_config()
        self.assertEqual(restored.gui_pass, secret)
        self.assertEqual(restored.mounts[0], mount)

    def test_wrong_boolean_type_does_not_enable_startup(self):
        config_path().write_text('{"startup_enabled":"false"}', encoding="utf-8")
        with self.assertRaises(ConfigError):
            load_config()

    def test_remote_parse_failure_is_not_an_empty_available_list(self):
        conf = Path(self.directory.name) / "broken.conf"
        conf.write_text("not a configuration section", encoding="utf-8")
        with self.assertRaises(ConfigError):
            list_remotes(conf)


if __name__ == "__main__":
    unittest.main()
