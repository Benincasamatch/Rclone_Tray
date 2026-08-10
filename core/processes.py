"""rclone 进程管理：GUI 服务进程 + 多个挂载进程。"""
from __future__ import annotations

import subprocess
import threading
import urllib.request
from typing import Optional

from .config import AppConfig, MountConfig, logs_dir
from .rclone import get_version, no_window_flag, version_tuple


class BaseProcess:
    """管理单个 rclone 子进程的基类。"""

    def __init__(self, exe: str, name: str, log_name: str):
        self.exe = exe
        self.name = name
        self.log_name = log_name
        self.proc: Optional[subprocess.Popen] = None
        self._log_file = logs_dir() / f"{log_name}.log"

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self, wait: float = 5.0) -> None:
        p = self.proc
        if p is None:
            return
        try:
            p.terminate()
        except Exception:
            pass
        try:
            p.wait(timeout=wait)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
        self.proc = None


class GuiService(BaseProcess):
    """rclone gui / rclone rcd --rc-web-gui 服务进程。"""

    GUI_PORT = 5570
    API_PORT = 5572
    URL = f"http://localhost:{GUI_PORT}/"

    def __init__(self, exe: str, cfg: AppConfig):
        super().__init__(exe, "rclone GUI", "gui")
        self.cfg = cfg

    def _auth_args(self, prefix: str) -> list[str]:
        if self.cfg.gui_no_auth:
            return [f"{prefix}-no-auth"]
        return [f"{prefix}-user", self.cfg.gui_user or "admin",
                f"{prefix}-pass", self.cfg.gui_pass or "admin"]

    def start(self) -> bool:
        if self.running():
            return False
        version = get_version(self.exe)
        if version_tuple(version) >= (1, 74):
            args = [self.exe, "gui", "--no-open-browser",
                    "--addr", f"localhost:{self.GUI_PORT}",
                    "--api-addr", f"localhost:{self.API_PORT}"] + self._auth_args("-")
        else:
            # 旧版本回退：rclone rcd --rc-web-gui
            self.URL = f"http://localhost:{self.API_PORT}/"
            args = [self.exe, "rcd", "--rc-web-gui", "--rc-web-gui-no-open-browser",
                    "--rc-addr", f"localhost:{self.API_PORT}"] + self._auth_args("-rc-")
        with open(self._log_file, "ab") as f:
            self.proc = subprocess.Popen(
                args, stdout=f, stderr=subprocess.STDOUT,
                creationflags=no_window_flag(),
            )
        return True

    def url(self) -> str:
        return self.URL

    def health_ok(self) -> bool:
        """通过 GUI 服务 URL 做健康检查（v1.74 为 5570 端口；rcd 回退为 RC 端口）。"""
        if not self.running():
            return False
        try:
            with urllib.request.urlopen(self.URL, timeout=2) as resp:
                return resp.status < 400
        except Exception:
            return False


class MountProcess(BaseProcess):
    """单个 rclone mount 挂载进程。"""

    def __init__(self, exe: str, mount: MountConfig):
        super().__init__(exe, f"mount-{mount.name}", f"mount-{mount.id}")
        self.mount = mount

    def _mount_args(self) -> list[str]:
        m = self.mount
        args = [self.exe, "mount", m.remote, m.mount_point]
        if m.vfs_cache_mode and m.vfs_cache_mode != "off":
            args += ["--vfs-cache-mode", m.vfs_cache_mode]
        if m.network_mode:
            args.append("--network-mode")
        if m.read_only:
            args.append("--read-only")
        if m.volname:
            args += ["--volname", m.volname]
        return args

    def start(self) -> bool:
        if self.running():
            return False
        with open(self._log_file, "ab") as f:
            self.proc = subprocess.Popen(
                self._mount_args(), stdout=f, stderr=subprocess.STDOUT,
                creationflags=no_window_flag(),
            )
        return True


class ProcessManager:
    """统一管理 GUI 服务与所有挂载进程。"""

    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.gui: Optional[GuiService] = None
        self.mounts: dict[str, MountProcess] = {}
        self._lock = threading.Lock()
        self.rebuild()

    def rebuild(self) -> None:
        with self._lock:
            exe = self.cfg.rclone_path
            self.gui = GuiService(exe, self.cfg) if exe else None
            self.mounts = (
                {m.id: MountProcess(exe, m) for m in self.cfg.mounts} if exe else {}
            )

    # --- GUI ---
    def gui_running(self) -> bool:
        return self.gui is not None and self.gui.running()

    def start_gui(self) -> bool:
        return self.gui is not None and self.gui.start()

    def stop_gui(self) -> None:
        if self.gui:
            self.gui.stop()

    # --- 挂载 ---
    def mount_running(self, mid: str) -> bool:
        mp = self.mounts.get(mid)
        return mp is not None and mp.running()

    def start_mount(self, mid: str) -> bool:
        mp = self.mounts.get(mid)
        return mp is not None and mp.start()

    def stop_mount(self, mid: str) -> None:
        mp = self.mounts.get(mid)
        if mp:
            mp.stop()

    def start_all_mounts(self) -> None:
        for mid in list(self.mounts):
            try:
                self.start_mount(mid)
            except Exception:
                pass

    def stop_all(self) -> None:
        self.stop_gui()
        for mid in list(self.mounts):
            self.stop_mount(mid)

    def summary(self) -> str:
        gui = "运行中" if self.gui_running() else ("已停止" if self.gui else "无 rclone")
        up = sum(1 for mid in self.mounts if self.mount_running(mid))
        return f"rclone GUI: {gui} | 挂载: {up}/{len(self.mounts)}"

    def state_signature(self) -> tuple:
        """用于判断状态是否变化的签名。"""
        return (
            self.gui_running(),
            tuple(self.mount_running(mid) for mid in sorted(self.mounts)),
        )
