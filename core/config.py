"""应用配置管理：读写 %APPDATA%\\rclone_tray\\config.json，并解析 rclone.conf 枚举 remote。"""
from __future__ import annotations

import configparser
import json
import os
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

APP_NAME = "rclone_tray"


def app_data_dir() -> Path:
    base = os.environ.get("APPDATA")
    p = Path(base) / APP_NAME if base else Path.home() / f".{APP_NAME}"
    p.mkdir(parents=True, exist_ok=True)
    return p


def config_path() -> Path:
    return app_data_dir() / "config.json"


def logs_dir() -> Path:
    d = app_data_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


@dataclass
class MountConfig:
    """单个挂载配置。mount_point 可为盘符（X:）或目录路径。"""
    id: str
    name: str                 # 显示名
    remote: str               # 例如 gdrive: 或 gdrive:path
    mount_point: str          # 盘符 "X:" 或路径
    vfs_cache_mode: str = "writes"
    network_mode: bool = False
    read_only: bool = False
    auto_mount: bool = True
    volname: str = ""

    @staticmethod
    def new(name: str, remote: str, mount_point: str, **kw) -> "MountConfig":
        return MountConfig(
            id=uuid.uuid4().hex[:12],
            name=name,
            remote=remote,
            mount_point=mount_point,
            **kw,
        )


@dataclass
class AppConfig:
    rclone_path: str = ""
    gui_no_auth: bool = True
    gui_user: str = ""
    gui_pass: str = ""
    gui_auto_start: bool = False
    startup_enabled: bool = False
    auto_mount_on_boot: bool = True
    mount_delay_seconds: int = 15
    mounts: list = field(default_factory=list)  # list[MountConfig]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["mounts"] = [asdict(m) for m in self.mounts]
        return d

    @staticmethod
    def from_dict(d: Optional[dict]) -> "AppConfig":
        d = d or {}
        mounts = []
        for m in d.get("mounts", []):
            m = dict(m)
            m.setdefault("vfs_cache_mode", "writes")
            m.setdefault("auto_mount", True)
            mounts.append(MountConfig(**m))
        return AppConfig(
            rclone_path=d.get("rclone_path", ""),
            gui_no_auth=d.get("gui_no_auth", True),
            gui_user=d.get("gui_user", ""),
            gui_pass=d.get("gui_pass", ""),
            gui_auto_start=d.get("gui_auto_start", False),
            startup_enabled=d.get("startup_enabled", False),
            auto_mount_on_boot=d.get("auto_mount_on_boot", True),
            mount_delay_seconds=int(d.get("mount_delay_seconds", 15)),
            mounts=mounts,
        )


def load_config() -> AppConfig:
    p = config_path()
    if p.exists():
        try:
            return AppConfig.from_dict(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            pass
    return AppConfig()


def save_config(cfg: AppConfig) -> None:
    config_path().write_text(
        json.dumps(cfg.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ---------- rclone.conf 解析 ----------

def rclone_config_candidates() -> list[Path]:
    cands: list[Path] = []
    if os.environ.get("APPDATA"):
        cands.append(Path(os.environ["APPDATA"]) / "rclone" / "rclone.conf")
    if os.environ.get("XDG_CONFIG_HOME"):
        cands.append(Path(os.environ["XDG_CONFIG_HOME"]) / "rclone" / "rclone.conf")
    cands.append(Path.home() / ".config" / "rclone" / "rclone.conf")
    cands.append(Path.home() / ".rclone.conf")
    return cands


def find_rclone_conf(explicit: str = "") -> Optional[Path]:
    if explicit:
        p = Path(explicit)
        if p.exists():
            return p
    for c in rclone_config_candidates():
        if c.exists():
            return c
    return None


def list_remotes(conf_path: Optional[Path]) -> list[str]:
    """返回 rclone.conf 中配置的 remote 名列表（形如 gdrive:）。"""
    if not conf_path or not conf_path.exists():
        return []
    cp = configparser.RawConfigParser()
    for enc in ("utf-8", "utf-8-sig", "gbk"):
        try:
            cp.read(conf_path, encoding=enc)
            break
        except Exception:
            continue
    return [s.strip() + ":" for s in cp.sections() if s.strip()]
