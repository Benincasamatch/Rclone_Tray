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

    def result(self):
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            events=self.controller.drain_events()
            if events:return events[-1]
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


if __name__=='__main__':unittest.main()
