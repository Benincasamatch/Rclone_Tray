"""rclone 进程管理：GUI 服务进程 + 多个挂载进程。"""
from __future__ import annotations

import re
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from typing import Optional

from .config import AppConfig, MountConfig, logs_dir
from .rclone import get_version, no_window_flag, version_tuple

# rclone 启动时打印的地址行，用于把网页 GUI 绑定到本次实际运行的进程
_RE_GUI_AVAILABLE = re.compile(r"GUI available at (\S+)")
_RE_GUI_NAVIGATE = re.compile(r"Navigate to (\S+) to use")
_RE_RC_SERVING = re.compile(r"Serving remote control on (\S+)")


def _first_match(pattern: re.Pattern, text: str) -> str:
    """返回最后一次匹配（同一日志文件可能包含多次启动记录）。"""
    found = pattern.findall(text)
    return found[-1].rstrip(".,") if found else ""


def _with_rc_url(gui_url: str, rc_url: str) -> str:
    """把 RC API 地址作为 url 查询参数拼到 GUI 登录地址上。

    免认证模式下 rclone 打印的 GUI 地址不带任何参数，网页端因此不知道
    RC API 在哪个端口，会提示 "URL is not configured"。
    """
    parts = urllib.parse.urlsplit(gui_url)
    query = urllib.parse.parse_qsl(parts.query)
    query.append(("url", rc_url))
    path = parts.path if parts.path not in ("", "/") else "/login"
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, path, urllib.parse.urlencode(query), parts.fragment)
    )


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
    # rclone 绑定 localhost 时实际监听 127.0.0.1，网页端的 CORS 校验按
    # 127.0.0.1 下发 Access-Control-Allow-Origin，所以这里必须用同一主机名，
    # 否则浏览器会拦截 GUI 到 RC API 的请求。
    HOST = "127.0.0.1"

    def __init__(self, exe: str, cfg: AppConfig):
        super().__init__(exe, "rclone GUI", "gui")
        self.cfg = cfg
        self._base_url = f"http://{self.HOST}:{self.GUI_PORT}/"
        self._rc_url = f"http://{self.HOST}:{self.API_PORT}/"
        self._open_url = ""

    def _auth_args(self, prefix: str) -> list[str]:
        if self.cfg.gui_no_auth:
            return [f"{prefix}-no-auth"]
        return [f"{prefix}-user", self.cfg.gui_user or "admin",
                f"{prefix}-pass", self.cfg.gui_pass or "admin"]

    def start(self) -> bool:
        if self.running():
            return False
        version = get_version(self.exe)
        self._open_url = ""
        if version_tuple(version) >= (1, 74):
            self._base_url = f"http://{self.HOST}:{self.GUI_PORT}/"
            self._rc_url = f"http://{self.HOST}:{self.API_PORT}/"
            args = [self.exe, "gui", "--no-open-browser",
                    "--addr", f"{self.HOST}:{self.GUI_PORT}",
                    "--api-addr", f"{self.HOST}:{self.API_PORT}"] + self._auth_args("-")
        else:
            # 旧版本回退：rclone rcd --rc-web-gui（网页与 RC API 同端口）
            self._base_url = f"http://{self.HOST}:{self.API_PORT}/"
            self._rc_url = self._base_url
            args = [self.exe, "rcd", "--rc-web-gui", "--rc-web-gui-no-open-browser",
                    "--rc-addr", f"{self.HOST}:{self.API_PORT}"] + self._auth_args("-rc-")
        # 每次启动清空日志，避免解析到上一次运行残留的地址
        self._log_file.write_bytes(b"")
        with open(self._log_file, "ab") as f:
            self.proc = subprocess.Popen(
                args, stdout=f, stderr=subprocess.STDOUT,
                creationflags=no_window_flag(),
            )
        return True

    def stop(self, wait: float = 5.0) -> None:
        super().stop(wait)
        self._open_url = ""

    def _read_log(self) -> str:
        try:
            return self._log_file.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return ""

    def _parse_open_url(self) -> str:
        """从本次启动的日志里解析出可直接打开的、已绑定当前进程的网页地址。"""
        log = self._read_log()
        if not log:
            return ""
        gui_url = _first_match(_RE_GUI_AVAILABLE, log) or _first_match(_RE_GUI_NAVIGATE, log)
        if not gui_url:
            return ""
        rc_url = _first_match(_RE_RC_SERVING, log) or self._rc_url
        if "url=" in gui_url:
            # 认证模式：rclone 已把 url/user/pass 一并写进登录地址
            return gui_url
        if urllib.parse.urlsplit(gui_url).netloc == urllib.parse.urlsplit(rc_url).netloc:
            # rcd 回退模式：网页与 RC API 同源，无需额外参数
            return gui_url
        return _with_rc_url(gui_url, rc_url)

    def url(self, timeout: float = 5.0) -> str:
        """返回网页 GUI 地址；已绑定本次 rclone 进程的 RC API 与凭据。

        免认证模式下 rclone 不会把 RC API 地址写进 GUI 地址，网页端会提示
        "URL is not configured"，因此这里补上 url 查询参数。
        """
        if self._open_url:
            return self._open_url
        deadline = time.monotonic() + max(timeout, 0.0)
        while True:
            parsed = self._parse_open_url()
            if parsed:
                self._open_url = parsed
                return parsed
            if not self.running() or time.monotonic() >= deadline:
                break
            time.sleep(0.2)
        # 兜底：日志尚未写出时按约定端口拼接
        if self._base_url == self._rc_url:
            return self._base_url
        return _with_rc_url(self._base_url, self._rc_url)

    def health_ok(self) -> bool:
        """通过 GUI 服务地址做健康检查（v1.74 为 GUI 端口；rcd 回退为 RC 端口）。"""
        if not self.running():
            return False
        try:
            with urllib.request.urlopen(self._base_url, timeout=2) as resp:
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
