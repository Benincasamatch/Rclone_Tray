"""Ownership invariants exercised with real disposable child processes."""
from __future__ import annotations

import copy
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from core.config import AppConfig, MountConfig
from core.processes import ProcessManager


class RuntimeOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rclone-runtime-regression-")
        self.environment = patch.dict(os.environ, {"RCLONE_TRAY_DATA_DIR": self.temp.name})
        self.environment.start()
        self.mount = MountConfig.new("owned", "sample:", "Y:")
        self.cfg = AppConfig(rclone_path=sys.executable, mounts=[self.mount])
        self.manager = ProcessManager(self.cfg)
        self.child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.process = self.manager.mounts[self.mount.id]
        self.process.proc = self.child

    def tearDown(self):
        if self.child.poll() is None:
            self.child.terminate()
        self.child.wait(timeout=10)
        self.process.proc = None
        self.environment.stop()
        self.temp.cleanup()

    def test_theme_and_name_save_retains_real_child_ownership(self):
        candidate = copy.deepcopy(self.cfg)
        candidate.theme = "dark"
        candidate.mounts[0].name = "renamed"
        self.manager.update_config(candidate)
        self.assertIs(self.manager.mounts[self.mount.id], self.process)
        self.assertIs(self.manager.mounts[self.mount.id].proc, self.child)
        self.assertEqual(self.manager.statuses()[1][self.mount.id].pid, self.child.pid)
        self.assertIsNone(self.child.poll())
        candidate.mounts[0].name = "external draft mutation"
        self.assertEqual(self.manager.mounts[self.mount.id].mount.name, "renamed")

    def test_connection_change_rejected_without_partial_metadata_update(self):
        candidate = copy.deepcopy(self.cfg)
        candidate.theme = "dark"
        candidate.mounts[0].remote = "other:folder"
        with self.assertRaises(ValueError):
            self.manager.update_config(candidate)
        self.assertEqual(self.manager.cfg.theme, self.cfg.theme)
        self.assertEqual(self.manager.mounts[self.mount.id].mount.remote, "sample:")
        self.assertEqual(self.manager.statuses()[1][self.mount.id].pid, self.child.pid)

    def test_delete_running_mount_does_not_orphan_process(self):
        candidate = copy.deepcopy(self.cfg)
        candidate.mounts = []
        with self.assertRaises(ValueError):
            self.manager.update_config(candidate)
        self.assertIn(self.mount.id, self.manager.mounts)
        self.assertIsNone(self.child.poll())


class RedactionTests(unittest.TestCase):
    def test_short_secret_does_not_destroy_log_text(self):
        from core.processes import _safe_text
        self.assertEqual(_safe_text('mount a failed; pass=secret1', ('a',)), 'mount a failed; pass=[redacted]')

    def test_long_secret_is_still_removed(self):
        from core.processes import _safe_text
        self.assertEqual(_safe_text('token abcdef echoed', ('abcdef',)), 'token [redacted] echoed')

    def test_default_gui_requires_credentials(self):
        import threading
        from core.processes import GuiService
        gui = GuiService(AppConfig(rclone_path=sys.executable), threading.Event())
        with patch('core.processes.find_rclone', return_value=sys.executable), \
             patch('core.processes.get_version', return_value='1.74.0'):
            self.assertIn('必须填写用户名和密码', gui._preflight())


if __name__ == "__main__":
    unittest.main()
