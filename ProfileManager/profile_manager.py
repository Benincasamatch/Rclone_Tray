"""Profile持久化保存模块 
Profile persistence module

Profile格式（Json）：
Profile format (Json):

    {
        "current_profile_id": "home",
        "profiles": {
            "home": {
                "id": "home",
                "remote-name": "home",
                "rclone_route": "http://0.0.0.0:5244/dav",
                "mount-drive": "R:"
            }
        }
    }

该模块只管理选中的Profile。 
This module only manages the selected Profile.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


_PROFILE_FIELDS = ("id", "remote-name", "rclone_route", "mount-drive")
"""Profile字段顺序，导入导出时保持一致"""
_DEFAULT_STORAGE_PATH = (
    Path(os.environ.get("APPDATA", Path.home())) / "RcloneTray" / "profiles.json"
) # 默认存储路径
_STORAGE_PATH = _DEFAULT_STORAGE_PATH # 存储路径

def configure_storage(file_path: str | os.PathLike[str]) -> Path:
    """设置该模块使用的JSON存储文件，并返回其路径。
    Set the JSON storage file used by this module and return its path."""
    global _STORAGE_PATH
    _STORAGE_PATH = Path(file_path).expanduser()
    return _STORAGE_PATH

def _empty_store() -> dict[str, Any]:
    """返回一个空的存储结构。
    Return an empty storage structure."""
    return {"current_profile_id": None, "profiles": {}}


def _read_store() -> dict[str, Any]:
    """读取存储文件并返回其内容。如果文件不存在，则返回一个空的存储结构。
    Read the storage file and return its contents. If the file does not exist, return an empty storage structure."""
    if not _STORAGE_PATH.exists():
        return _empty_store()

    try:
        with _STORAGE_PATH.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Profile file is not valid JSON: {_STORAGE_PATH}") from exc

    if not isinstance(data, dict) or not isinstance(data.get("profiles"), dict):
        raise ValueError("Profile file must contain a 'profiles' object")

    current_id = data.get("current_profile_id")
    if current_id is not None and current_id not in data["profiles"]:
        data["current_profile_id"] = None
    return data

def _write_store(store: dict[str, Any]) -> None:
    """以原子方式写入存储文件，以防止中断的写入导致文件损坏。
    Write the storage file atomically to prevent corruption from interrupted writes.
    """
    parent = _STORAGE_PATH.parent
    parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f"{_STORAGE_PATH.stem}.", suffix=".tmp", dir=parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as file:
            json.dump(store, file, ensure_ascii=False, indent=2)
            file.write("\n")
        os.replace(temporary_name, _STORAGE_PATH)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise

def _validate_profile(profile_id: str, profile_data: dict[str, Any]) -> dict[str, str]:
    """验证Profile ID和数据的有效性，并返回一个完整的Profile字典。
    Validate the profile ID and data, returning a complete profile dictionary."""
    if not isinstance(profile_id, str) or not profile_id.strip():
        raise ValueError("profile_id must be a non-empty string")
    if not isinstance(profile_data, dict):
        raise ValueError("profile_data must be an object")

    profile = dict(profile_data)
    profile["id"] = profile_id
    for field in _PROFILE_FIELDS[1:]:
        value = profile.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Profile field '{field}' must be a non-empty string")
    return {field: profile[field] for field in _PROFILE_FIELDS}

# 创建并持久化一个Profile。如果ID重复，则拒绝创建。
# Create and persist a profile. Duplicate IDs are rejected.
def create_profile(profile_id: str, remote_name: str, rclone_route: str,
                   mount_drive: str) -> dict[str, str]:
    store = _read_store()
    if profile_id in store["profiles"]:
        raise ValueError(f"Profile already exists: {profile_id}")
    profile = _validate_profile(profile_id, {
        "remote-name": remote_name,
        "rclone_route": rclone_route,
        "mount-drive": mount_drive,
    })
    store["profiles"][profile_id] = profile
    if store["current_profile_id"] is None:
        store["current_profile_id"] = profile_id
    _write_store(store)
    return dict(profile)

# 加载一个Profile，如果不存在则抛出KeyError。
# Load one profile, raising ``KeyError`` when it does not exist.
def load_profile(profile_id: str) -> dict[str, str]:
    store = _read_store()
    try:
        return dict(store["profiles"][profile_id])
    except KeyError as exc:
        raise KeyError(f"Profile not found: {profile_id}") from exc

# 保存一个Profile，如果不存在则创建它。
# Create or update a profile and persist it.
def save_profile(profile_id: str, profile_data: dict[str, Any]) -> dict[str, str]:
    store = _read_store()
    profile = _validate_profile(profile_id, profile_data)
    store["profiles"][profile_id] = profile
    if store["current_profile_id"] is None:
        store["current_profile_id"] = profile_id
    _write_store(store)
    return dict(profile)

# 删除一个Profile，如果不存在则抛出KeyError。
# Delete a profile and clear the current selection if necessary.
def delete_profile(profile_id: str) -> None:
    store = _read_store()
    if profile_id not in store["profiles"]:
        raise KeyError(f"Profile not found: {profile_id}")
    del store["profiles"][profile_id]
    if store["current_profile_id"] == profile_id:
        store["current_profile_id"] = next(iter(store["profiles"]), None)
    _write_store(store)

# 选择一个现有的Profile并持久化选择。
# Select an existing profile and persist the selection.
def switch_profile(profile_id: str) -> dict[str, str]:
    store = _read_store()
    if profile_id not in store["profiles"]:
        raise KeyError(f"Profile not found: {profile_id}")
    store["current_profile_id"] = profile_id
    _write_store(store)
    return dict(store["profiles"][profile_id])

# 获取当前选中的Profile,如果不存在则返回None。
# Return the selected profile, or ``None`` when no profile exists.
def get_current_profile() -> dict[str, str] | None:
    store = _read_store()
    current_id = store["current_profile_id"]
    return dict(store["profiles"][current_id]) if current_id else None

# 获取所有Profile,按文件顺序返回独立的字典列表。
# Return all profiles in file order as independent dictionaries.
def get_all_profiles() -> list[dict[str, str]]:
    return [dict(profile) for profile in _read_store()["profiles"].values()]

# 从JSON文件导入Profile并返回导入的Profile列表。
# 完整的模块存储格式和单个Profile对象都被接受。现有的Profile ID将被更新。
# Import profiles from a JSON file and return the imported profiles.
# Both a single profile object and the module's complete store format are accepted. Existing profile IDs are updated.
def import_profile(file_path: str | os.PathLike[str]) -> list[dict[str, str]]:

    path = Path(file_path).expanduser()
    try:
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except FileNotFoundError:
        raise FileNotFoundError(f"Profile file not found: {path}") from None
    except json.JSONDecodeError as exc:
        raise ValueError(f"Profile file is not valid JSON: {path}") from exc

    if isinstance(data, dict) and isinstance(data.get("profiles"), dict):
        raw_profiles = data["profiles"].values()
    elif isinstance(data, dict):
        raw_profiles = (data,)
    elif isinstance(data, list):
        raw_profiles = data
    else:
        raise ValueError("Import file must contain a profile object or profile list")

    store = _read_store()
    imported: list[dict[str, str]] = []
    for raw_profile in raw_profiles:
        if not isinstance(raw_profile, dict):
            raise ValueError("Every imported profile must be an object")
        profile_id = raw_profile.get("id")
        if not isinstance(profile_id, str):
            raise ValueError("Every imported profile must have a valid 'id' field")
        profile = _validate_profile(profile_id, raw_profile)
        store["profiles"][profile_id] = profile
        imported.append(dict(profile))
    if store["current_profile_id"] is None and imported:
        store["current_profile_id"] = imported[0]["id"]
    _write_store(store)
    return imported

def export_profile(profile_id: str, file_path: str | os.PathLike[str]) -> Path:
    """将一个Profile导出为JSON并返回目标路径。
    Export one profile as JSON and return the destination path."""
    profile = load_profile(profile_id)
    path = Path(file_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as file:
        json.dump(profile, file, ensure_ascii=False, indent=2)
        file.write("\n")
    return path

"""
开放 API：
public API:

    configure_storage: 设置存储路径
        set storage path

    create_profile: 创建Profile
        create Profile

    load_profile: 加载Profile
        load Profile

    save_profile: 保存Profile
        save Profile

    delete_profile: 删除Profile
        delete Profile

    switch_profile: 切换Profile
        switch Profile

    get_current_profile: 获取当前Profile
        get current Profile

    get_all_profiles: 获取所有Profile
        get all Profiles

    import_profile: 导入Profile
        import Profile

    export_profile: 导出Profile
        export Profile
"""
__all__ = [
    "configure_storage", "create_profile", "load_profile", "save_profile",
    "delete_profile", "switch_profile", "get_current_profile",
    "get_all_profiles", "import_profile", "export_profile",
]
