"""Application operations: one committed configuration, retained instances, queued results."""
from __future__ import annotations

import copy
import os
import queue
import re
import threading
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from core import startup
from core.config import (AppConfig, ConfigError, MountConfig, config_checksum,
                         find_rclone_conf, list_remotes, load_config, save_config)
from core.processes import ProcessManager, RuntimeStatus
from core.rclone import find_rclone, get_version, winfsp_installed


class Controller:
    def __init__(self, script_path: str):
        self.script_path = script_path
        self._lock = threading.RLock()
        self._events: queue.Queue[dict] = queue.Queue()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rclone-operation")
        self._busy: set[str] = set()
        self._closing = False
        self._timer: threading.Timer | None = None
        self._revision = 0
        self._config_error = ""
        self._environment_error = ""
        self._version = ""
        self._remotes: list[str] = []
        self._winfsp = False
        try:
            self._cfg = load_config()
        except ConfigError as exc:
            self._cfg = AppConfig()
            self._config_error = str(exc)
        try:
            self._cfg.startup_enabled = startup.is_enabled()
        except OSError:
            self._environment_error = "无法读取系统自启状态，请检查当前账户权限。"
        try:
            self._fingerprint = config_checksum()
        except ConfigError as exc:
            self._fingerprint = None
            self._config_error = str(exc)
        self.pm = ProcessManager(self._cfg)


    def snapshot(self) -> dict:
        with self._lock:
            gui, mounts = self.pm.statuses()
            return {"config": copy.deepcopy(self._cfg), "revision": self._revision,
                    "config_error": self._config_error, "gui": gui, "mounts": mounts,
                    "busy": set(self._busy), "version": self._version,
                    "remotes": list(self._remotes), "environment_error": self._environment_error,
                    "winfsp": self._winfsp, "closing": self._closing}

    def drain_events(self) -> list[dict]:
        result = []
        while True:
            try:
                result.append(self._events.get_nowait())
            except queue.Empty:
                return result

    def _submit(self, key: str, action: str, operation: Callable, *, allow_closing=False) -> bool:
        with self._lock:
            if self._closing and not allow_closing:
                self._events.put({"kind": "result", "key": key, "action": action,
                                  "ok": False, "message": "应用正在退出，未执行新操作。"})
                return False
            if key in self._busy or (key != "shutdown" and "shutdown" in self._busy):
                return False
            self._busy.add(key)

        def run():
            try:
                message = operation()
                ok = True
            except Exception as exc:
                # Do not log exception repr/arguments; operational messages contain no credentials.
                message = str(exc) if isinstance(exc, (ValueError, ConfigError, OSError)) else "操作失败；请查看对应日志并重新读取状态。"
                ok = False
            with self._lock:
                self._busy.discard(key)
            self._events.put({"kind": "result", "key": key, "action": action,
                              "ok": ok, "message": message or "操作已完成。"})
        self._executor.submit(run)
        return True

    def _check_revision(self, revision: int):
        if self._config_error:
            raise ConfigError("配置未保存：原文件无法读取。请修复后点击重新读取配置。")
        if revision != self._revision:
            raise ValueError("配置已被另一操作修改，未保存当前草稿。请取消后重新打开编辑；输入仍保留。")
        if config_checksum() != self._fingerprint:
            raise ConfigError("配置文件已在外部修改，未覆盖文件。请重新读取配置后再编辑。")

    def _commit(self, candidate: AppConfig, revision: int, registry=False):
        with self._lock:
            self._check_revision(revision)
            previous = copy.deepcopy(self._cfg)
            old_entry = startup.read_entry() if registry else None
            old_startup = old_entry is not None if registry else previous.startup_enabled
            # update_config checks active connection changes before committing runtime configuration.
            self.pm.update_config(candidate)
            try:
                if registry and candidate.startup_enabled != old_startup:
                    if candidate.startup_enabled:
                        startup.enable(self.script_path)
                    else:
                        startup.disable()
                    if startup.is_enabled() != candidate.startup_enabled:
                        raise ConfigError("系统自启设置未生效，配置未保存。")
                save_config(candidate, expected_checksum=self._fingerprint)
            except Exception as failure:
                self.pm.update_config(previous)
                if registry:
                    try:
                        startup.restore_entry(old_entry)
                    except OSError:
                        try:
                            self._cfg.startup_enabled = startup.is_enabled()
                        except OSError:
                            self._environment_error = "系统自启状态无法读回，请检查注册表权限。"
                        raise ConfigError("配置未保存，且系统自启回滚失败。请在设置中核对当前实际状态。") from failure
                if registry and isinstance(failure, OSError):
                    raise ConfigError("系统自启设置未保存：Windows 拒绝注册表写入。请检查当前账户权限；原配置与启动项已保留。") from failure
                raise
            self._cfg = copy.deepcopy(candidate)
            self._revision += 1
            self._fingerprint = config_checksum()

    def refresh_environment(self):
        def operation():
            with self._lock:
                cfg = copy.deepcopy(self._cfg)
            errors = []
            exe = find_rclone(cfg.rclone_path)
            version = get_version(exe) if exe else None
            if not version:
                errors.append("未找到有效 rclone；请在设置中选择 rclone.exe。")
            conf = find_rclone_conf(cfg.rclone_conf)
            remotes = []
            try:
                remotes = list_remotes(conf)
                if cfg.rclone_conf and not conf:
                    errors.append("指定的 rclone 配置文件不存在。")
            except (ConfigError, OSError, ValueError):
                errors.append("rclone 配置无法读取；请检查文件或使用 rclone config 修复。")
            with self._lock:
                self._version = version or ""
                self._remotes = remotes
                self._winfsp = winfsp_installed()
                self._environment_error = "\n".join(errors)
                if version and not cfg.rclone_path:
                    cfg.rclone_path = str(exe)
                    self.pm.update_config(cfg)
                    self._cfg.rclone_path = str(exe)
            return "环境已刷新。" if not errors else "\n".join(errors)
        return self._submit("environment", "refresh_environment", operation)

    def reload_config(self):
        def operation():
            candidate = load_config()
            candidate.startup_enabled = startup.is_enabled()
            with self._lock:
                self.pm.update_config(candidate)
                self._cfg = candidate
                self._config_error = ""
                self._fingerprint = config_checksum()
                self._revision += 1
            self.refresh_environment()
            return "配置已重新读取，未覆盖任何文件。"
        return self._submit("settings", "reload_config", operation)

    @staticmethod
    def _validate_settings(cfg: AppConfig):
        if cfg.theme not in ("system", "light", "dark"):
            raise ValueError("外观模式无效。")
        if isinstance(cfg.mount_delay_seconds, bool) or not isinstance(cfg.mount_delay_seconds, int) or not 0 <= cfg.mount_delay_seconds <= 86400:
            raise ValueError("自动挂载延迟必须为 0–86400 秒的整数。")
        exe = find_rclone(cfg.rclone_path)
        if not exe or not get_version(exe):
            raise ValueError("rclone 路径无效：请指定可运行的 rclone.exe。配置未保存。")
        cfg.rclone_path = str(exe)
        if cfg.rclone_conf and not Path(cfg.rclone_conf).is_file():
            raise ValueError("rclone 配置路径无效：请选择现有文件。")
        if not cfg.gui_no_auth and (not cfg.gui_user or not cfg.gui_pass):
            raise ValueError("启用认证时，用户名和密码都必须填写。")
        if any(char in cfg.gui_user + cfg.gui_pass for char in "\r\n\x00"):
            raise ValueError("用户名与密码不能包含换行或空字符。")

    def save_settings(self, draft: AppConfig, revision: int):
        candidate = copy.deepcopy(draft)
        def operation():
            self._validate_settings(candidate)
            with self._lock:
                candidate.mounts = copy.deepcopy(self._cfg.mounts)
            self._commit(candidate, revision, registry=True)
            self._cancel_timer()
            self.refresh_environment()
            return "设置已保存；现有运行实例未重新启动。"
        return self._submit("settings", "save_settings", operation)

    def save_mount(self, mount: MountConfig, revision: int):
        draft = copy.deepcopy(mount)
        def operation():
            draft.name = draft.name.strip()
            draft.remote = draft.remote.strip()
            draft.mount_point = draft.mount_point.strip()
            if not draft.name:
                raise ValueError("请填写挂载名称。")
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", draft.id):
                raise ValueError("挂载 ID 无效，未保存。")
            if ":" not in draft.remote or not draft.remote.split(":", 1)[0]:
                raise ValueError("Remote 格式应为名称:路径，例如 cloud:folder。")
            remotes = list_remotes(find_rclone_conf(self._cfg.rclone_conf))
            if draft.remote.split(":", 1)[0] + ":" not in remotes:
                raise ValueError("Remote 不存在于当前 rclone 配置；请配置后刷新。")
            point = draft.mount_point
            drive = bool(re.fullmatch(r"[C-Zc-z]:", point))
            if not drive:
                target = Path(point)
                if not target.is_absolute() or not target.parent.is_dir():
                    raise ValueError("挂载点须为 C:–Z: 盘符或父目录存在的绝对目录路径。")
                existing = next((item for item in self._cfg.mounts if item.id == draft.id), None)
                active = self.pm.statuses()[1].get(draft.id)
                same_running_point = existing is not None and existing.mount_point == point and active is not None and active.pid is not None
                if target.exists() and not same_running_point:
                    if not target.is_dir() or target.is_symlink() or target.is_junction() or any(target.iterdir()):
                        raise ValueError("现有挂载目录必须为空且不是链接；不会删除目录中的文件。")
            elif os.path.exists(point + "\\"):
                existing = next((item for item in self._cfg.mounts if item.id == draft.id), None)
                active = self.pm.statuses()[1].get(draft.id)
                if not existing or existing.mount_point.casefold() != point.casefold() or not active or active.pid is None:
                    raise ValueError("所选盘符已被占用，请选择未使用盘符。")
            if any(char in point + draft.remote + draft.volname for char in "\r\n\x00"):
                raise ValueError("挂载参数不能包含换行或空字符。")
            if draft.vfs_cache_mode not in ("off", "minimal", "writes", "full"):
                raise ValueError("VFS 缓存模式无效。")
            with self._lock:
                candidate = copy.deepcopy(self._cfg)
                for existing in candidate.mounts:
                    if existing.id == draft.id:
                        continue
                    if existing.name.casefold() == draft.name.casefold():
                        raise ValueError("挂载名称已存在，请使用不同名称。")
                    if os.path.normcase(os.path.normpath(existing.mount_point)) == os.path.normcase(os.path.normpath(point)):
                        raise ValueError("挂载点已被其他配置使用。")
                    if draft.volname and (draft.network_mode or draft.volname.startswith("\\\\")) and (existing.network_mode or existing.volname.startswith("\\\\")):
                        def share_name(item):
                            value = item.volname or f"\\\\rclone-tray\\{item.id}"
                            return (value if value.startswith("\\\\") else "\\\\server\\" + value).rstrip("\\/").casefold()
                        if share_name(existing) == share_name(draft):
                            raise ValueError("网络共享名已被其他挂载使用，请修改卷标。")
                index = next((i for i, item in enumerate(candidate.mounts) if item.id == draft.id), None)
                if index is None:
                    candidate.mounts.append(draft)
                else:
                    candidate.mounts[index] = draft
            self._commit(candidate, revision)
            return f"「{draft.name}」配置已保存；未自动启动挂载。"
        return self._submit(draft.id, "save_mount", operation)

    def delete_mount(self, mid: str, revision: int):
        def operation():
            with self._lock:
                candidate = copy.deepcopy(self._cfg)
                target = next((item for item in candidate.mounts if item.id == mid), None)
                if target is None:
                    raise ValueError("挂载配置已不存在，请刷新列表。")
                candidate.mounts = [item for item in candidate.mounts if item.id != mid]
            self._commit(candidate, revision)
            return f"「{target.name}」配置已删除，未删除任何远端文件。"
        return self._submit(mid, "delete_mount", operation)

    @staticmethod
    def _status_result(status: RuntimeStatus):
        if status.state not in ("ready", "stopped"):
            raise ValueError(status.message or "操作未完成，请查看日志。")
        return status.message

    def start_mount(self, mid):
        return self._submit(mid, "start_mount", lambda: self._status_result(self.pm.start_mount(mid)))

    def stop_mount(self, mid):
        return self._submit(mid, "stop_mount", lambda: self._status_result(self.pm.stop_mount(mid)))

    def start_gui(self):
        return self._submit("gui", "start_gui", lambda: self._status_result(self.pm.start_gui()))

    def stop_gui(self):
        return self._submit("gui", "stop_gui", lambda: self._status_result(self.pm.stop_gui()))

    @staticmethod
    def _batch_result(results: dict[str, RuntimeStatus]):
        failures = [(key, status.message) for key, status in results.items() if status.state not in ("ready", "stopped")]
        if failures:
            raise ValueError(f"完成 {len(results) - len(failures)}/{len(results)} 项；失败项：\n" + "\n".join(f"{key}：{message}" for key, message in failures))
        return f"已完成 {len(results)} 项。"

    def start_all_mounts(self):
        return self._submit("mounts", "start_all_mounts", lambda: self._batch_result(self.pm.start_all_mounts()))

    def stop_all_mounts(self):
        def operation():
            with self._lock:
                mids = [item.id for item in self._cfg.mounts]
            return self._batch_result({mid: self.pm.stop_mount(mid) for mid in mids})
        return self._submit("mounts", "stop_all_mounts", operation)

    def open_gui(self):
        def operation():
            url = self.pm.gui_url()
            if not url:
                raise ValueError("GUI 尚未就绪，请先启动服务。")
            if not webbrowser.open(url):
                raise OSError("浏览器未打开，请检查系统默认浏览器。")
            return "已打开当前 GUI；认证模式请在网页中输入凭据。"
        return self._submit("gui", "open_gui", operation)

    def open_mount(self, mid):
        def operation():
            status = self.pm.statuses()[1].get(mid)
            if not status or status.state != "ready":
                raise ValueError("挂载尚未就绪，请先启动并等待。")
            with self._lock:
                mount = next(item for item in self._cfg.mounts if item.id == mid)
            point = mount.mount_point
            os.startfile(point + "\\" if re.fullmatch(r"[A-Za-z]:", point) else point)
            return "已在资源管理器打开挂载位置。"
        return self._submit(mid, "open_mount", operation)

    def open_path(self, path):
        def operation():
            if str(path).startswith(("https://", "http://")):
                if not webbrowser.open(str(path)):
                    raise OSError("默认浏览器无法打开链接，请检查系统设置。")
                return "已打开官方链接。"
            target = Path(path)
            if not target.exists():
                raise ValueError("目标文件或目录不存在，请检查路径。")
            os.startfile(str(target))
            return "已打开目标位置。"
        return self._submit("path", "open_path", operation)

    def open_rclone_config(self):
        conf = find_rclone_conf(self._cfg.rclone_conf)
        if conf:
            return self.open_path(conf)
        self._events.put({"kind": "result", "key": "path", "action": "open_rclone_config", "ok": False,
                          "message": "未找到 rclone.conf；请使用 rclone config 配置远端后刷新。"})
        return False

    def set_startup(self, enabled):
        snap = self.snapshot()
        snap["config"].startup_enabled = bool(enabled)
        return self.save_settings(snap["config"], snap["revision"])

    def set_auto_mount(self, enabled):
        snap = self.snapshot()
        snap["config"].auto_mount_on_boot = bool(enabled)
        return self.save_settings(snap["config"], snap["revision"])

    def _cancel_timer(self):
        with self._lock:
            if self._timer:
                self._timer.cancel()
                self._timer = None

    def schedule_startup(self, autostart: bool):
        self._cancel_timer()
        if self._config_error:
            return
        def run():
            if autostart:
                with self._lock:
                    enabled = self._cfg.auto_mount_on_boot
                if enabled:
                    self._submit("mounts", "auto_mount", lambda: self._batch_result(self.pm.start_all_mounts(auto_only=True)))
        with self._lock:
            if autostart and self._cfg.auto_mount_on_boot:
                self._timer = threading.Timer(self._cfg.mount_delay_seconds, run)
                self._timer.daemon = True
                self._timer.start()
            if self._cfg.gui_auto_start:
                self.start_gui()

    def queue_navigation(self, page="mounts", mount_id=""):
        self._events.put({"kind": "navigate", "page": page, "mount_id": mount_id})

    def request_exit(self):
        with self._lock:
            if "shutdown" in self._busy:
                return False
            self._closing = True
            self.pm.cancel.set()
        self._cancel_timer()
        def operation():
            try:
                results = self.pm.stop_all()
                gui, mounts = self.pm.statuses()
                if gui.pid is not None or any(item.pid is not None for item in mounts.values()):
                    raise ValueError("仍有本应用进程未停止；请查看日志并再次退出。")
                return self._batch_result(results)
            except Exception:
                with self._lock:
                    self._closing = False
                    self.pm.cancel.clear()
                raise
        return self._submit("shutdown", "shutdown", operation, allow_closing=True)

    def close_executor(self):
        self._cancel_timer()
        self._executor.shutdown(wait=True, cancel_futures=True)
