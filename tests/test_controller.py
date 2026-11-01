"""Transaction and startup boundaries, isolated from user configuration/registry."""
from __future__ import annotations
import copy
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from core.config import AppConfig, MountConfig, config_path, load_config, save_config
from app.controller import Controller


class ControllerBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='rclone-controller-regression-')
        self.env = patch.dict(os.environ, {'RCLONE_TRAY_DATA_DIR': self.temp.name})
        self.env.start()
        self.conf = Path(self.temp.name) / 'rclone.conf'
        self.conf.write_text('[sample]\ntype = local\n',encoding='utf-8')
        cfg = AppConfig(rclone_conf=str(self.conf),mounts=[MountConfig.new('first','sample:','Y:')])
        save_config(cfg)
        self.controller = Controller(str(Path(__file__).parent.parent / 'main.py'))

    def tearDown(self):
        self.controller.pm.cancel.set()
        self.controller.pm.stop_all()
        self.controller.close_executor()
        self.env.stop()
        self.temp.cleanup()

    def result(self, key=None):
        deadline=time.monotonic()+5
        latest=None
        while time.monotonic()<deadline:
            for event in self.controller.drain_events():
                if key is None or event.get('key')==key:
                    latest=event
            if latest is not None:
                return latest
            time.sleep(.01)
        self.fail('operation did not settle')

    def test_stale_mount_revision_preserves_newer_commit(self):
        snapshot=self.controller.snapshot()
        first=copy.deepcopy(snapshot['config'].mounts[0])
        first.name='newer'
        self.controller.save_mount(first,snapshot['revision'])
        self.assertTrue(self.result()['ok'])
        first.name='stale'
        self.controller.save_mount(first,snapshot['revision'])
        result=self.result()
        self.assertFalse(result['ok'])
        self.assertEqual(load_config().mounts[0].name,'newer')
        self.assertEqual(self.controller.snapshot()['config'].mounts[0].name,'newer')

    def test_save_failure_does_not_mutate_committed_mount(self):
        snapshot=self.controller.snapshot()
        draft=copy.deepcopy(snapshot['config'].mounts[0]);draft.name='unsaved'
        with patch('core.config.os.replace',side_effect=PermissionError('locked')):
            self.controller.save_mount(draft,snapshot['revision'])
            self.assertFalse(self.result()['ok'])
        self.assertEqual(self.controller.snapshot()['config'].mounts[0].name,'first')
        self.assertEqual(self.controller.pm.mounts[draft.id].mount.name,'first')
        self.assertEqual(load_config().mounts[0].name,'first')

    def test_manual_launch_does_not_schedule_boot_mount(self):
        with patch.object(self.controller.pm,'start_all_mounts') as start:
            self.controller.schedule_startup(False)
            time.sleep(.05)
            self.assertIsNone(self.controller._timer)
            start.assert_not_called()

    def test_exit_cancels_pending_boot_launch_and_rejects_new_start(self):
        self.controller.schedule_startup(True)
        self.controller.request_exit()
        self.assertTrue(self.result()['ok'])
        self.assertIsNone(self.controller._timer)
        self.assertFalse(self.controller.start_mount(self.controller.snapshot()['config'].mounts[0].id))
        result=self.result()
        self.assertFalse(result['ok'])
        self.assertTrue(self.controller.snapshot()['closing'])

    def test_discovered_rclone_path_is_never_persisted(self):
        # 单文件版内置 rclone 位于临时解包目录；把它写进配置会让下次启动找不到程序。
        bundle = str(Path(self.temp.name) / '_MEI123456' / 'rclone' / 'rclone.exe')
        with patch('app.controller.find_rclone', return_value=bundle), \
             patch('app.controller.get_version', return_value='1.74.0'):
            self.controller.refresh_environment()
            self.assertTrue(self.result('environment')['ok'])
            snapshot = self.controller.snapshot()
            self.assertEqual(snapshot['exe'], bundle)
            self.assertEqual(snapshot['config'].rclone_path, '')
            self.controller.save_settings(copy.deepcopy(snapshot['config']), snapshot['revision'])
            self.assertTrue(self.result('settings')['ok'])
        self.assertEqual(load_config().rclone_path, '')

    def test_saving_settings_keeps_pending_boot_mount(self):
        self.controller._cfg.mount_delay_seconds = 3600
        self.controller.schedule_startup(True)
        self.assertIsNotNone(self.controller._timer)
        with patch.object(self.controller.pm, 'start_all_mounts') as start:
            snapshot = self.controller.snapshot()
            self.controller.save_settings(copy.deepcopy(snapshot['config']), snapshot['revision'])
            self.assertTrue(self.result('settings')['ok'])
            self.assertIsNotNone(self.controller._timer)
            start.assert_not_called()
            disabled = copy.deepcopy(self.controller.snapshot()['config'])
            disabled.auto_mount_on_boot = False
            self.controller.save_settings(disabled, self.controller.snapshot()['revision'])
            self.assertTrue(self.result('settings')['ok'])
            self.assertIsNone(self.controller._timer)


if __name__=='__main__':unittest.main()
