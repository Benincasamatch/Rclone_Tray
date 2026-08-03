"""Profile Manager 的完整功能测试。

运行方式：
    python -m unittest -v test_profile_manager.py
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import profile_manager


class ProfileManagerTests(unittest.TestCase):
    """使用临时目录测试，避免修改真实的 Profile 文件。"""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.storage_path = self.root / "profiles.json"
        self.assertEqual(
            profile_manager.configure_storage(self.storage_path), self.storage_path
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @staticmethod
    def make_profile(profile_id: str = "home", drive: str = "R:") -> dict[str, str]:
        return {
            "id": profile_id,
            "remote-name": "remote-home" if profile_id == "home" else f"remote-{profile_id}",
            "rclone_route": "remote-home:/data" if profile_id == "home" else f"remote-{profile_id}:/data",
            "mount-drive": drive,
        }

    def test_empty_storage_and_configure_storage(self) -> None:
        self.assertIsNone(profile_manager.get_current_profile())
        self.assertEqual(profile_manager.get_all_profiles(), [])
        self.assertFalse(self.storage_path.exists())

    def test_create_and_load_profile(self) -> None:
        profile = profile_manager.create_profile(
            "home", "remote-home", "remote-home:/data", "R:"
        )

        self.assertEqual(profile, self.make_profile())
        self.assertEqual(profile_manager.load_profile("home"), profile)
        self.assertEqual(profile_manager.get_current_profile(), profile)
        self.assertTrue(self.storage_path.exists())

        with self.storage_path.open(encoding="utf-8") as file:
            stored_data = json.load(file)
        self.assertEqual(stored_data["current_profile_id"], "home")
        self.assertEqual(stored_data["profiles"]["home"], profile)

    def test_create_rejects_duplicate_id(self) -> None:
        profile_manager.create_profile("home", "remote", "remote:/", "R:")
        with self.assertRaisesRegex(ValueError, "already exists"):
            profile_manager.create_profile("home", "other", "other:/", "S:")

    def test_save_profile_creates_and_updates_profile(self) -> None:
        created = profile_manager.save_profile("home", self.make_profile())
        self.assertEqual(created, self.make_profile())

        updated_data = self.make_profile(drive="S:")
        updated_data["rclone_route"] = "remote-home:/updated"
        updated = profile_manager.save_profile("home", updated_data)

        self.assertEqual(updated, updated_data)
        self.assertEqual(profile_manager.load_profile("home"), updated_data)

    def test_get_all_profiles_returns_independent_profiles(self) -> None:
        home = profile_manager.create_profile("home", "home", "home:/", "R:")
        work = profile_manager.create_profile("work", "work", "work:/", "S:")

        profiles = profile_manager.get_all_profiles()
        self.assertEqual(profiles, [home, work])
        profiles[0]["mount-drive"] = "X:"
        self.assertEqual(profile_manager.load_profile("home")["mount-drive"], "R:")

    def test_switch_profile(self) -> None:
        profile_manager.create_profile("home", "home", "home:/", "R:")
        work = profile_manager.create_profile("work", "work", "work:/", "S:")

        self.assertEqual(profile_manager.switch_profile("work"), work)
        self.assertEqual(profile_manager.get_current_profile(), work)

        # 重新读取文件，确认切换状态已经持久化。
        profile_manager.configure_storage(self.storage_path)
        self.assertEqual(profile_manager.get_current_profile(), work)

    def test_delete_profile_and_select_fallback(self) -> None:
        home = profile_manager.create_profile("home", "home", "home:/", "R:")
        profile_manager.create_profile("work", "work", "work:/", "S:")

        profile_manager.delete_profile("home")
        self.assertEqual(profile_manager.get_all_profiles(), [
            profile_manager.load_profile("work")
        ])
        self.assertEqual(profile_manager.get_current_profile()["id"], "work")

        profile_manager.delete_profile("work")
        self.assertIsNone(profile_manager.get_current_profile())
        self.assertEqual(profile_manager.get_all_profiles(), [])
        self.assertNotIn(home, profile_manager.get_all_profiles())

    def test_missing_profile_operations_raise_key_error(self) -> None:
        with self.assertRaisesRegex(KeyError, "not found"):
            profile_manager.load_profile("missing")
        with self.assertRaisesRegex(KeyError, "not found"):
            profile_manager.switch_profile("missing")
        with self.assertRaisesRegex(KeyError, "not found"):
            profile_manager.delete_profile("missing")
        with self.assertRaisesRegex(KeyError, "not found"):
            profile_manager.export_profile("missing", self.root / "missing.json")

    def test_invalid_profile_data_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            profile_manager.create_profile("", "remote", "remote:/", "R:")
        with self.assertRaises(ValueError):
            profile_manager.create_profile("home", "", "remote:/", "R:")
        with self.assertRaises(ValueError):
            profile_manager.create_profile("home", "remote", "", "R:")
        with self.assertRaises(ValueError):
            profile_manager.create_profile("home", "remote", "remote:/", "")
        with self.assertRaises(ValueError):
            profile_manager.save_profile("home", {"id": "home"})

    def test_export_and_import_single_profile(self) -> None:
        profile = profile_manager.create_profile("home", "home", "home:/", "R:")
        export_path = self.root / "exports" / "home.json"

        self.assertEqual(profile_manager.export_profile("home", export_path), export_path)
        with export_path.open(encoding="utf-8") as file:
            self.assertEqual(json.load(file), profile)

        profile_manager.delete_profile("home")
        imported = profile_manager.import_profile(export_path)
        self.assertEqual(imported, [profile])
        self.assertEqual(profile_manager.load_profile("home"), profile)

    def test_import_profile_list_and_complete_store(self) -> None:
        profile_list_path = self.root / "profile-list.json"
        profiles = [
            self.make_profile("home", "R:"),
            self.make_profile("work", "S:"),
        ]
        profile_list_path.write_text(
            json.dumps(profiles, ensure_ascii=False), encoding="utf-8"
        )
        self.assertEqual(profile_manager.import_profile(profile_list_path), profiles)
        self.assertEqual(profile_manager.get_all_profiles(), profiles)

        store_path = self.root / "store.json"
        imported_profile = self.make_profile("cloud", "T:")
        store_path.write_text(
            json.dumps(
                {"current_profile_id": "cloud", "profiles": {"cloud": imported_profile}},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.assertEqual(profile_manager.import_profile(store_path), [imported_profile])
        self.assertEqual(profile_manager.load_profile("cloud"), imported_profile)

    def test_import_errors_and_invalid_storage_errors(self) -> None:
        with self.assertRaises(FileNotFoundError):
            profile_manager.import_profile(self.root / "does-not-exist.json")

        invalid_json = self.root / "invalid.json"
        invalid_json.write_text("{invalid", encoding="utf-8")
        with self.assertRaises(ValueError):
            profile_manager.import_profile(invalid_json)

        invalid_profile = self.root / "invalid-profile.json"
        invalid_profile.write_text(json.dumps({"id": "only-id"}), encoding="utf-8")
        with self.assertRaises(ValueError):
            profile_manager.import_profile(invalid_profile)

        self.storage_path.write_text("[]", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "profiles"):
            profile_manager.get_all_profiles()


if __name__ == "__main__":
    unittest.main(verbosity=2)
