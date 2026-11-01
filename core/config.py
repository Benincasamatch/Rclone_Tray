"""Validated, atomic application configuration and rclone config discovery."""
from __future__ import annotations

import base64
import configparser
import ctypes
import hashlib
import json
import os
import re
import tempfile
import uuid
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Optional

APP_NAME = "rclone_tray"
SCHEMA_VERSION = 2
_UNCHECKED = object()


class ConfigError(Exception):
    """A configuration failure which is safe to display without secret values."""


def _io_error(action: str, exc: OSError) -> ConfigError:
    code = getattr(exc, "winerror", None) or exc.errno
    return ConfigError(f"{action}失败（系统错误 {code}）。请检查路径、权限和文件占用；原配置未被重置。")


def app_data_dir() -> Path:
    override = os.environ.get("RCLONE_TRAY_DATA_DIR")
    base = os.environ.get("APPDATA")
    path = Path(override).expanduser() if override else (
        Path(base) / APP_NAME if base else Path.home() / f".{APP_NAME}"
    )
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise _io_error("创建应用数据目录", exc) from None
    return path


def config_path() -> Path:
    return app_data_dir() / "config.json"


def _data_subdir(name: str) -> Path:
    path = app_data_dir() / name
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise _io_error("创建应用数据子目录", exc) from None
    return path


def logs_dir() -> Path:
    return _data_subdir("logs")


def cache_dir() -> Path:
    return _data_subdir("cache")


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _dpapi(data: bytes, encrypt: bool) -> bytes:
    if os.name != "nt":
        raise ConfigError("凭据加密需要 Windows DPAPI；无法在此系统读写受保护的密码。")
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source = _DataBlob(len(data), buffer)
    result = _DataBlob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    if encrypt:
        fn = crypt32.CryptProtectData
        fn.argtypes = [ctypes.POINTER(_DataBlob), ctypes.c_wchar_p,
                       ctypes.POINTER(_DataBlob), ctypes.c_void_p, ctypes.c_void_p,
                       ctypes.c_ulong, ctypes.POINTER(_DataBlob)]
        ok = fn(ctypes.byref(source), APP_NAME, None, None, None, 1, ctypes.byref(result))
    else:
        fn = crypt32.CryptUnprotectData
        fn.argtypes = [ctypes.POINTER(_DataBlob), ctypes.c_void_p,
                       ctypes.POINTER(_DataBlob), ctypes.c_void_p, ctypes.c_void_p,
                       ctypes.c_ulong, ctypes.POINTER(_DataBlob)]
        ok = fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result))
    if not ok:
        raise ConfigError("Windows DPAPI 凭据保护失败。请使用保存此配置的 Windows 用户；原配置保持不变。")
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        kernel32.LocalFree(result.pbData)


def _encode_password(password: str) -> dict:
    return {"scheme": "dpapi", "data": base64.b64encode(_dpapi(password.encode("utf-8"), True)).decode("ascii")}


def _decode_password(value: object, legacy: bool) -> str:
    if legacy and isinstance(value, str):
        return value
    if not isinstance(value, dict) or set(value) != {"scheme", "data"}:
        raise ConfigError("gui_pass 必须是受保护凭据；请修复配置格式，勿覆盖原文件。")
    if value.get("scheme") != "dpapi" or not isinstance(value.get("data"), str):
        raise ConfigError("不支持此凭据保护格式；请使用兼容版本和原 Windows 用户。")
    try:
        encrypted = base64.b64decode(value["data"], validate=True)
        if not encrypted:
            raise ValueError
        return _dpapi(encrypted, False).decode("utf-8")
    except (ValueError, UnicodeError):
        raise ConfigError("受保护密码数据损坏；请修复原配置或从备份恢复。") from None


def _require_type(value: object, expected: type, label: str) -> None:
    if type(value) is not expected:
        raise ConfigError(f"配置字段 {label} 类型错误；请修复原文件。")
    if expected is str and "\0" in value:
        raise ConfigError(f"配置字段 {label} 含无效字符。")
    if expected is str:
        try:
            value.encode("utf-8")
        except UnicodeError:
            raise ConfigError(f"配置字段 {label} 含无效 Unicode 字符。") from None


@dataclass
class MountConfig:
    id: str
    name: str
    remote: str
    mount_point: str
    vfs_cache_mode: str = "writes"
    network_mode: bool = False
    read_only: bool = False
    auto_mount: bool = True
    volname: str = ""

    @staticmethod
    def new(name: str, remote: str, mount_point: str, **kw) -> "MountConfig":
        return MountConfig(uuid.uuid4().hex[:12], name, remote, mount_point, **kw)

    def validate(self) -> None:
        for item in fields(self):
            _require_type(getattr(self, item.name), bool if item.name in {
                "network_mode", "read_only", "auto_mount"
            } else str, f"mounts.{item.name}")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", self.id):
            raise ConfigError("挂载 ID 无效；不得自动替换既有身份。")
        if not self.name.strip() or not self.remote.strip() or not self.mount_point.strip():
            raise ConfigError("挂载名称、remote 和挂载点不能为空。")
        if self.vfs_cache_mode not in {"off", "minimal", "writes", "full"}:
            raise ConfigError("挂载缓存模式必须为 off/minimal/writes/full。")


@dataclass
class AppConfig:
    rclone_path: str = ""
    gui_no_auth: bool = False
    gui_user: str = field(default="", repr=False)
    gui_pass: str = field(default="", repr=False)
    gui_auto_start: bool = False
    startup_enabled: bool = False
    auto_mount_on_boot: bool = True
    mount_delay_seconds: int = 15
    theme: str = "dark"
    mounts: list[MountConfig] = field(default_factory=list)
    rclone_conf: str = ""
    reduce_motion: bool = False

    def validate(self) -> None:
        boolean_fields = {"gui_no_auth", "gui_auto_start", "startup_enabled",
                          "auto_mount_on_boot", "reduce_motion"}
        for item in fields(self):
            expected = bool if item.name in boolean_fields else (
                int if item.name == "mount_delay_seconds" else list if item.name == "mounts" else str
            )
            _require_type(getattr(self, item.name), expected, item.name)
        if self.theme not in {"system", "light", "dark"}:
            raise ConfigError("主题必须为 system/light/dark。")
        if not 0 <= self.mount_delay_seconds <= 86400:
            raise ConfigError("自动挂载延迟须为 0–86400 秒的整数。")
        ids: set[str] = set()
        for mount in self.mounts:
            if not isinstance(mount, MountConfig):
                raise ConfigError("mounts 必须由挂载配置组成。")
            mount.validate()
            if mount.id in ids:
                raise ConfigError("挂载 ID 重复；请修复原配置，不得丢弃记录。")
            ids.add(mount.id)

    def to_dict(self) -> dict:
        self.validate()
        data = asdict(self)
        data["schema_version"] = SCHEMA_VERSION
        data["gui_pass"] = _encode_password(self.gui_pass)
        return data

    @staticmethod
    def from_dict(data: dict) -> "AppConfig":
        if not isinstance(data, dict):
            raise ConfigError("配置根节点必须是 JSON 对象；原文件未被重置。")
        schema = data.get("schema_version", 1)
        if type(schema) is not int or schema not in {1, SCHEMA_VERSION}:
            raise ConfigError("不支持此配置 schema_version；请使用兼容版本。")
        allowed = {item.name for item in fields(AppConfig)} | {"schema_version"}
        if set(data) - allowed:
            raise ConfigError("配置包含无法识别的字段；请使用兼容版本，避免丢失数据。")
        if schema == SCHEMA_VERSION and not (allowed - {"schema_version"}).issubset(data):
            raise ConfigError("当前版本配置缺少必需字段；请从备份恢复，不能静默填充默认值。")
        values = {key: value for key, value in data.items() if key != "schema_version"}
        if "gui_pass" in values:
            values["gui_pass"] = _decode_password(values["gui_pass"], schema == 1)
        elif schema == SCHEMA_VERSION:
            raise ConfigError("配置缺少受保护密码字段；请修复原文件。")
        raw_mounts = values.get("mounts", [])
        if not isinstance(raw_mounts, list):
            raise ConfigError("mounts 必须是数组。")
        mounts = []
        mount_fields = {item.name for item in fields(MountConfig)}
        for raw in raw_mounts:
            if not isinstance(raw, dict) or set(raw) - mount_fields:
                raise ConfigError("挂载配置格式错误或包含未知字段。")
            if not {"id", "name", "remote", "mount_point"}.issubset(raw):
                raise ConfigError("挂载缺少 ID、名称、remote 或挂载点；请恢复原记录。")
            mounts.append(MountConfig(**raw))
        values["mounts"] = mounts
        cfg = AppConfig(**values)
        cfg.validate()
        return cfg


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    data = {}
    for key, value in pairs:
        if key in data:
            raise ConfigError("JSON 配置包含重复字段；请修复原文件。")
        data[key] = value
    return data


def _parse_config(raw: bytes) -> AppConfig:
    try:
        data = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_object)
    except (UnicodeError, ValueError):
        raise ConfigError("配置不是有效 UTF-8 JSON；请修复文件或从备份恢复，原文件未被重置。") from None
    return AppConfig.from_dict(data)


def _read_config_bytes(path: Path) -> Optional[bytes]:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _io_error("读取应用配置", exc) from None


def config_checksum() -> Optional[str]:
    """SHA-256 of current on-disk bytes; None means absent, errors are visible."""
    raw = _read_config_bytes(config_path())
    return hashlib.sha256(raw).hexdigest() if raw is not None else None


def load_config() -> AppConfig:
    raw = _read_config_bytes(config_path())
    return AppConfig() if raw is None else _parse_config(raw)


def save_config(cfg: AppConfig, *, expected_checksum: object = _UNCHECKED) -> None:
    """Persist atomically; optionally reject any external change, including creation."""
    path = config_path()
    original = _read_config_bytes(path)
    checksum = hashlib.sha256(original).hexdigest() if original is not None else None
    if expected_checksum is not _UNCHECKED and checksum != expected_checksum:
        raise ConfigError("配置已被外部修改；未保存。请重新加载后再应用草稿。")
    if original is not None:
        _parse_config(original)  # Never replace damaged/unsupported data, even without CAS.
    encoded = json.dumps(cfg.to_dict(), ensure_ascii=False, indent=2).encode("utf-8")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", prefix=".config-", suffix=".tmp",
                                         dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        if _read_config_bytes(path) != original:
            raise ConfigError("保存期间配置被外部修改；未覆盖。请重新加载后重试。")
        os.replace(temporary, path)
        temporary = None
    except OSError as exc:
        raise _io_error("原子保存应用配置", exc) from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def rclone_config_candidates() -> list[Path]:
    explicit_env = os.environ.get("RCLONE_CONFIG")
    if explicit_env:
        return [Path(os.path.expandvars(explicit_env)).expanduser()]
    if os.environ.get("RCLONE_TRAY_DATA_DIR"):
        return [app_data_dir() / "rclone.conf"]
    candidates: list[Path] = []
    if os.environ.get("APPDATA"):
        candidates.append(Path(os.environ["APPDATA"]) / "rclone" / "rclone.conf")
    if os.environ.get("XDG_CONFIG_HOME"):
        candidates.append(Path(os.environ["XDG_CONFIG_HOME"]) / "rclone" / "rclone.conf")
    candidates.extend([Path.home() / ".config" / "rclone" / "rclone.conf",
                       Path.home() / ".rclone.conf"])
    return candidates


def find_rclone_conf(explicit: str = "") -> Optional[Path]:
    candidates = [Path(os.path.expandvars(explicit)).expanduser()] if explicit else rclone_config_candidates()
    for path in candidates:
        try:
            if path.is_file():
                return path
        except OSError as exc:
            raise _io_error("检查 rclone 配置", exc) from None
    if explicit or os.environ.get("RCLONE_CONFIG"):
        raise ConfigError("指定的 rclone 配置文件不存在或不是文件；请修正路径，不会回退到其他配置。")
    return None


def list_remotes(conf_path: Optional[Path]) -> list[str]:
    if conf_path is None:
        return []
    try:
        raw = Path(conf_path).read_bytes()
    except OSError as exc:
        raise _io_error("读取 rclone 配置", exc) from None
    text = None
    for encoding in ("utf-8-sig", "gbk"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeError:
            continue
    if text is None:
        raise ConfigError("rclone 配置编码无法识别；请保存为 UTF-8。")
    if text.startswith("RCLONE_ENCRYPT_V"):
        raise ConfigError("rclone 配置已加密；无法直接枚举 remote，请在 rclone 中解锁配置或手动输入已有 remote。")
    parser = configparser.RawConfigParser(interpolation=None, strict=True)
    try:
        parser.read_string(text)
    except configparser.Error:
        raise ConfigError("rclone 配置格式无效；请通过 rclone config 修复。") from None
    return [section + ":" for section in parser.sections() if section.strip()]
