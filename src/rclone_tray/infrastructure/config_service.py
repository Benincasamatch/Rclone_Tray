"""Config Service - 配置文件读写 (TOML 格式)

职责：
- 读取/写入 TOML 配置文件
- 提供默认配置
- 配置备份与恢复
- 不包含业务逻辑
"""

from __future__ import annotations

import shutil
import tomllib
from pathlib import Path
from typing import Any

import tomli_w

from rclone_tray.infrastructure.logger import get_logger

logger = get_logger(__name__)

# 默认配置结构
DEFAULT_CONFIG: dict[str, Any] = {
    "general": {
        "language": "zh-CN",
        "theme": "system",
        "minimize_to_tray": True,
        "auto_start": False,
        "rclone_path": "",  # 空表示自动检测
    },
    "profiles": {},
}

CONFIG_FILENAME = "config.toml"
BACKUP_SUFFIX = ".backup"


class ConfigService:
    """TOML 配置文件读写服务。

    使用方式:
        config = ConfigService(config_dir)
        data = config.read()
        config.write(data)
    """

    def __init__(self, config_dir: str | Path) -> None:
        self._config_dir = Path(config_dir).resolve()
        self._config_path = self._config_dir / CONFIG_FILENAME
        self._config_dir.mkdir(parents=True, exist_ok=True)

    # ── 公开接口 ──────────────────────────────────────────

    @property
    def config_path(self) -> Path:
        """配置文件完整路径。"""
        return self._config_path

    @property
    def config_dir(self) -> Path:
        """配置目录路径。"""
        return self._config_dir

    def read(self) -> dict[str, Any]:
        """读取配置文件，若不存在则返回默认配置。"""
        if not self._config_path.exists():
            logger.info("配置文件不存在，返回默认配置: %s", self._config_path)
            return dict(DEFAULT_CONFIG)
        try:
            raw = self._config_path.read_bytes()
            data: dict[str, Any] = tomllib.loads(raw.decode("utf-8"))
            logger.debug("配置文件读取成功: %s", self._config_path)
            return self._merge_defaults(data)
        except (tomllib.TOMLDecodeError, OSError, ValueError) as exc:
            logger.error("配置文件解析失败: %s", exc)
            return dict(DEFAULT_CONFIG)

    def write(self, data: dict[str, Any]) -> None:
        """写入配置文件。"""
        try:
            toml_str = tomli_w.dumps(data)
            self._config_path.write_text(toml_str, encoding="utf-8")
            logger.info("配置文件写入成功: %s", self._config_path)
        except (OSError, ValueError) as exc:
            logger.error("配置文件写入失败: %s", exc)
            raise ConfigWriteError(str(exc)) from exc

    def backup(self) -> Path:
        """创建配置文件备份，返回备份路径。"""
        if not self._config_path.exists():
            raise ConfigNotFoundError("无法备份不存在的配置文件")
        backup_path = self._config_path.with_suffix(
            f".toml{BACKUP_SUFFIX}"
        )
        shutil.copy2(self._config_path, backup_path)
        logger.info("配置备份创建成功: %s", backup_path)
        return backup_path

    def restore(self, backup_path: str | Path | None = None) -> None:
        """从备份文件恢复配置。"""
        src = Path(backup_path) if backup_path else self._config_path.with_suffix(f".toml{BACKUP_SUFFIX}")
        if not src.exists():
            raise ConfigNotFoundError(f"备份文件不存在: {src}")
        shutil.copy2(src, self._config_path)
        logger.info("配置恢复成功: %s <- %s", self._config_path, src)

    def reset_to_defaults(self) -> dict[str, Any]:
        """重置为默认配置并写入文件。"""
        defaults = dict(DEFAULT_CONFIG)
        self.write(defaults)
        return defaults

    # ── 内部方法 ──────────────────────────────────────────

    @staticmethod
    def _merge_defaults(data: dict[str, Any]) -> dict[str, Any]:
        """将已有配置与默认配置合并，补全缺失的键。"""
        merged = dict(DEFAULT_CONFIG)
        merged.update(data)
        # 递归合并 general 节
        if "general" in data and isinstance(data["general"], dict):
            merged["general"] = {**merged["general"], **data["general"]}
        # 合并 profiles 节
        if "profiles" in data and isinstance(data["profiles"], dict):
            merged["profiles"] = {**merged["profiles"], **data["profiles"]}
        return merged


# ── 异常定义 ─────────────────────────────────────────────


class ConfigError(Exception):
    """配置服务基础异常。"""


class ConfigWriteError(ConfigError):
    """配置写入失败。"""


class ConfigNotFoundError(ConfigError):
    """配置文件未找到。"""
