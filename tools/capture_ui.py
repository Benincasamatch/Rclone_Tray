"""Capture and smoke-test the real Rust/Win32 application in an isolated sandbox.

Usage (after building dist/rclone_tray.exe):
    .venv\\Scripts\\python.exe tools\\capture_ui.py [output_dir] [--light] [--exe PATH]
    .venv\\Scripts\\python.exe tools\\capture_ui.py [output_dir] --before

Writes native-stencil-*.png and an observed-checks JSON report to docs/evidence
by default. --before captures only the four current pages (no sidebar toggle or
restyle smoke). Screenshots are foreground desktop crops, never PrintWindow.
Requires an unlocked interactive Windows desktop and matching process integrity.
No mount/service starts, saved edits, elevation, or existing-process termination.
"""
from __future__ import annotations

import argparse
import base64
import ctypes as C
from ctypes import wintypes as W
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter

from PIL import ImageGrab

ROOT = Path(__file__).resolve().parents[1]
WM_GETTEXT, WM_GETTEXTLENGTH, WM_SETTEXT, WM_COMMAND = 0xD, 0xE, 0xC, 0x111
WM_HSCROLL, WM_VSCROLL = 0x114, 0x115
LB_GETCOUNT, LB_GETCURSEL, LB_SETCURSEL, LB_GETITEMRECT = 0x18B, 0x188, 0x186, 0x198
CB_GETCURSEL, CB_SETCURSEL = 0x147, 0x14E
PAGES = (("mounts", 101, "挂载", 220), ("service", 102, "rclone GUI", 300),
         ("settings", 103, "设置", 406), ("about", 104, "关于与诊断", 501))


class SmokeError(RuntimeError):
    pass


class Blob(C.Structure):
    _fields_ = [("size", W.DWORD), ("data", C.POINTER(C.c_ubyte))]


class MouseInput(C.Structure):
    _fields_ = [("dx", W.LONG), ("dy", W.LONG), ("mouseData", W.DWORD),
                ("dwFlags", W.DWORD), ("time", W.DWORD), ("dwExtraInfo", C.c_size_t)]


class KeyboardInput(C.Structure):
    _fields_ = [("wVk", W.WORD), ("wScan", W.WORD), ("dwFlags", W.DWORD),
                ("time", W.DWORD), ("dwExtraInfo", C.c_size_t)]


class HardwareInput(C.Structure):
    _fields_ = [("uMsg", W.DWORD), ("wParamL", W.WORD), ("wParamH", W.WORD)]


class InputUnion(C.Union):
    _fields_ = [("mi", MouseInput), ("ki", KeyboardInput), ("hi", HardwareInput)]


class Input(C.Structure):
    _anonymous_ = ("payload",)
    _fields_ = [("type", W.DWORD), ("payload", InputUnion)]


class Native:
    def __init__(self) -> None:
        self.user = C.WinDLL("user32", use_last_error=True)
        self.kernel = C.WinDLL("kernel32", use_last_error=True)
        self.crypt = C.WinDLL("crypt32", use_last_error=True)
        self.advapi = C.WinDLL("advapi32", use_last_error=True)
        self.callback = C.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
        signatures = {
            "EnumWindows": ([self.callback, W.LPARAM], W.BOOL),
            "GetWindowThreadProcessId": ([W.HWND, C.POINTER(W.DWORD)], W.DWORD),
            "GetClassNameW": ([W.HWND, W.LPWSTR, C.c_int], C.c_int),
            "GetDlgItem": ([W.HWND, C.c_int], W.HWND),
            "GetWindowRect": ([W.HWND, C.POINTER(W.RECT)], W.BOOL),
            "GetClientRect": ([W.HWND, C.POINTER(W.RECT)], W.BOOL),
            "ClientToScreen": ([W.HWND, C.POINTER(W.POINT)], W.BOOL),
            "IsWindow": ([W.HWND], W.BOOL),
            "IsWindowVisible": ([W.HWND], W.BOOL),
            "IsWindowEnabled": ([W.HWND], W.BOOL),
            "SendMessageTimeoutW": ([W.HWND, W.UINT, W.WPARAM, W.LPARAM,
                                     W.UINT, W.UINT, C.POINTER(C.c_size_t)], W.LPARAM),
            "PostMessageW": ([W.HWND, W.UINT, W.WPARAM, W.LPARAM], W.BOOL),
            "GetForegroundWindow": ([], W.HWND),
            "SetForegroundWindow": ([W.HWND], W.BOOL),
            "BringWindowToTop": ([W.HWND], W.BOOL),
            "ShowWindow": ([W.HWND, C.c_int], W.BOOL),
            "AttachThreadInput": ([W.DWORD, W.DWORD, W.BOOL], W.BOOL),
            "SetWindowPos": ([W.HWND, W.HWND, C.c_int, C.c_int, C.c_int, C.c_int, W.UINT], W.BOOL),
            "SetCursorPos": ([C.c_int, C.c_int], W.BOOL),
            "WindowFromPoint": ([W.POINT], W.HWND),
            "IsChild": ([W.HWND, W.HWND], W.BOOL),
            "SendInput": ([W.UINT, C.POINTER(Input), C.c_int], W.UINT),
            "GetDpiForWindow": ([W.HWND], W.UINT),
            "SystemParametersInfoW": ([W.UINT, W.UINT, C.c_void_p, W.UINT], W.BOOL),
            "SetProcessDpiAwarenessContext": ([W.HANDLE], W.BOOL),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.user, name)
            function.argtypes, function.restype = args, result
        self.kernel.GetCurrentThreadId.restype = W.DWORD
        self.kernel.GetCurrentProcess.restype = W.HANDLE
        self.kernel.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
        self.kernel.OpenProcess.restype = W.HANDLE
        self.kernel.CloseHandle.argtypes = [W.HANDLE]
        self.kernel.CloseHandle.restype = W.BOOL
        self.kernel.LocalFree.argtypes = [C.c_void_p]
        self.kernel.LocalFree.restype = C.c_void_p
        self.crypt.CryptProtectData.argtypes = [C.POINTER(Blob), W.LPCWSTR, C.c_void_p,
                                                C.c_void_p, C.c_void_p, W.DWORD, C.POINTER(Blob)]
        self.crypt.CryptProtectData.restype = W.BOOL
        self.advapi.OpenProcessToken.argtypes = [W.HANDLE, W.DWORD, C.POINTER(W.HANDLE)]
        self.advapi.OpenProcessToken.restype = W.BOOL
        self.advapi.GetTokenInformation.argtypes = [W.HANDLE, C.c_int, C.c_void_p,
                                                   W.DWORD, C.POINTER(W.DWORD)]
        self.advapi.GetTokenInformation.restype = W.BOOL
        self.advapi.GetSidSubAuthorityCount.argtypes = [C.c_void_p]
        self.advapi.GetSidSubAuthorityCount.restype = C.POINTER(C.c_ubyte)
        self.advapi.GetSidSubAuthority.argtypes = [C.c_void_p, W.DWORD]
        self.advapi.GetSidSubAuthority.restype = C.POINTER(W.DWORD)
        # Physical desktop pixels must match the coordinates used by ImageGrab.
        if not self.user.SetProcessDpiAwarenessContext(C.c_void_p(-4)):
            self.error("Enabling per-monitor DPI awareness")

    @staticmethod
    def error(operation: str) -> None:
        code = C.get_last_error()
        if code == 5:
            raise SmokeError(f"{operation}: access denied (Windows integrity/UIPI). Run the capture "
                             "and application at the same integrity level on an unlocked desktop; "
                             "this tool never requests UAC elevation.")
        raise SmokeError(f"{operation}: Win32 error {code}: {C.FormatError(code).strip()}")

    def pid(self, hwnd: int) -> int:
        value = W.DWORD()
        self.user.GetWindowThreadProcessId(hwnd, C.byref(value))
        return value.value

    def windows(self, pid: int, class_name: str) -> list[int]:
        result: list[int] = []

        @self.callback
        def visit(hwnd, _):
            if self.pid(hwnd) == pid:
                name = C.create_unicode_buffer(256)
                self.user.GetClassNameW(hwnd, name, len(name))
                if name.value == class_name and self.user.IsWindowVisible(hwnd):
                    result.append(hwnd)
            return True

        if not self.user.EnumWindows(visit, 0):
            self.error("Enumerating launched application windows")
        return result

    def send(self, hwnd: int, message: int, wparam: int = 0, lparam: int = 0) -> int:
        result = C.c_size_t()
        C.set_last_error(0)
        if not self.user.SendMessageTimeoutW(hwnd, message, wparam, lparam, 0x22, 3000, C.byref(result)):
            self.error(f"Sending native message 0x{message:x}")
        return C.c_ssize_t(result.value).value

    def text(self, hwnd: int) -> str:
        length = self.send(hwnd, WM_GETTEXTLENGTH)
        buffer = C.create_unicode_buffer(max(1, length + 1))
        self.send(hwnd, WM_GETTEXT, len(buffer), C.addressof(buffer))
        return buffer.value

    def control(self, hwnd: int, control_id: int) -> int:
        child = self.user.GetDlgItem(hwnd, control_id)
        if not child:
            raise SmokeError(f"Missing real native control ID {control_id}")
        return child

    def rect(self, hwnd: int) -> tuple[int, int, int, int]:
        rect = W.RECT()
        if not self.user.GetWindowRect(hwnd, C.byref(rect)):
            self.error("Reading window bounds")
        return rect.left, rect.top, rect.right, rect.bottom

    def client_rect(self, hwnd: int) -> tuple[int, int, int, int]:
        rect, origin = W.RECT(), W.POINT()
        if not self.user.GetClientRect(hwnd, C.byref(rect)) or not self.user.ClientToScreen(hwnd, C.byref(origin)):
            self.error("Reading native client bounds")
        return origin.x, origin.y, origin.x + rect.right, origin.y + rect.bottom

    def integrity(self, process_handle: int) -> int:
        token = W.HANDLE()
        if not self.advapi.OpenProcessToken(process_handle, 8, C.byref(token)):
            self.error("Reading process integrity")
        try:
            length = W.DWORD()
            self.advapi.GetTokenInformation(token, 25, None, 0, C.byref(length))
            buffer = C.create_string_buffer(length.value)
            if not self.advapi.GetTokenInformation(token, 25, buffer, length, C.byref(length)):
                self.error("Reading process integrity token")
            sid = C.cast(buffer, C.POINTER(C.c_void_p))[0]
            count = self.advapi.GetSidSubAuthorityCount(sid)[0]
            return self.advapi.GetSidSubAuthority(sid, count - 1)[0]
        finally:
            self.kernel.CloseHandle(token)

    def check_integrity(self, pid: int) -> tuple[int, int]:
        handle = self.kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            self.error("Opening launched process for integrity check")
        try:
            ours = self.integrity(self.kernel.GetCurrentProcess())
            theirs = self.integrity(handle)
        finally:
            self.kernel.CloseHandle(handle)
        if ours != theirs:
            raise SmokeError(f"Process integrity mismatch (capture={ours:#x}, app={theirs:#x}); "
                             "native input/messages may be denied by UIPI. Use matching integrity "
                             "levels; this tool never elevates or requests UAC.")
        return ours, theirs

    def foreground(self, hwnd: int) -> None:
        self.user.ShowWindow(hwnd, 9)
        current = self.kernel.GetCurrentThreadId()
        foreground = self.user.GetForegroundWindow()
        target_thread = self.user.GetWindowThreadProcessId(hwnd, None)
        foreground_thread = self.user.GetWindowThreadProcessId(foreground, None) if foreground else 0
        attached = []
        try:
            for thread in {target_thread, foreground_thread} - {0, current}:
                if self.user.AttachThreadInput(current, thread, True):
                    attached.append(thread)
            self.user.BringWindowToTop(hwnd)
            self.user.SetForegroundWindow(hwnd)
        finally:
            for thread in attached:
                self.user.AttachThreadInput(current, thread, False)
        time.sleep(0.15)
        if self.user.GetForegroundWindow() != hwnd:
            raise SmokeError("Cannot foreground the launched native window. Unlock the interactive "
                             "desktop and use matching process integrity (UIPI); no UAC is requested.")

    def resize(self, hwnd: int, width: int, height: int) -> tuple[int, int, int, int]:
        scale = self.user.GetDpiForWindow(hwnd) / 96
        width, height = round(width * scale), round(height * scale)
        work = W.RECT()
        if not self.user.SystemParametersInfoW(0x30, 0, C.byref(work), 0):
            self.error("Reading desktop work area")
        if width > work.right - work.left or height > work.bottom - work.top:
            raise SmokeError(f"Interactive desktop work area cannot fit {width}x{height} physical "
                             "pixels at this DPI; use a larger desktop or lower display scaling.")
        x = work.left + (work.right - work.left - width) // 2
        y = work.top + (work.bottom - work.top - height) // 2
        if not self.user.SetWindowPos(hwnd, None, x, y, width, height, 0x14):
            self.error("Resizing native window")
        time.sleep(0.25)
        return self.rect(hwnd)

    def reachable(self, parent: int, child: int) -> bool:
        if not self.user.IsWindowVisible(child):
            return False
        x1, y1, x2, y2 = self.rect(child)
        a, b, c, d = self.client_rect(parent)
        footer = self.user.GetDlgItem(parent, 114)
        if footer and footer != child and x1 >= self.rect(footer)[0]:
            d = min(d, self.rect(footer)[1] - round(12 * self.user.GetDpiForWindow(parent) / 96))
        return x1 >= a and y1 >= b and x2 <= c and y2 <= d and x2 > x1 and y2 > y1

    def pointer(self, parent: int, child: int, point: tuple[int, int] | None = None) -> bool:
        if not self.reachable(parent, child):
            return False
        self.foreground(parent)
        x1, y1, x2, y2 = self.rect(child)
        x, y = point or ((x1 + x2) // 2, (y1 + y2) // 2)
        hit = self.user.WindowFromPoint(W.POINT(x, y))
        if hit != child and not self.user.IsChild(child, hit):
            raise SmokeError("Native pointer target is covered by another window; refusing to click.")
        if not self.user.SetCursorPos(x, y):
            self.error("Moving actual desktop pointer")
        events = (Input * 2)()
        events[0].mi.dwFlags, events[1].mi.dwFlags = 0x2, 0x4
        C.set_last_error(0)
        if self.user.SendInput(2, events, C.sizeof(Input)) != 2:
            raise SmokeError("Native pointer input denied (SendInput/integrity/UIPI or locked "
                             "desktop). Run at matching integrity; this tool never elevates.")
        time.sleep(0.18)
        return True

    def key(self, virtual_key: int) -> None:
        events = (Input * 2)()
        events[0].type = events[1].type = 1
        events[0].ki.wVk = events[1].ki.wVk = virtual_key
        events[1].ki.dwFlags = 2
        if self.user.SendInput(2, events, C.sizeof(Input)) != 2:
            raise SmokeError("Native keyboard input denied by the desktop")
        time.sleep(0.18)

    def command(self, parent: int, control_id: int, notification: int = 0,
                prefer_pointer: bool = True) -> str:
        child = self.control(parent, control_id)
        if not self.user.IsWindowEnabled(child):
            raise SmokeError(f"Native action ID {control_id} is disabled")
        if prefer_pointer and notification == 0 and self.pointer(parent, child):
            return "pointer"
        if not self.user.PostMessageW(parent, WM_COMMAND, control_id | (notification << 16), child):
            self.error(f"Posting native WM_COMMAND ID {control_id}")
        time.sleep(0.18)
        return "WM_COMMAND"

    def protected_empty_password(self) -> dict[str, str]:
        backing = (C.c_ubyte * 1)()
        source, result = Blob(0, backing), Blob()
        if not self.crypt.CryptProtectData(C.byref(source), "rclone_tray", None, None, None, 1, C.byref(result)):
            self.error("Protecting isolated empty fixture credential")
        try:
            return {"scheme": "dpapi", "data": base64.b64encode(C.string_at(result.data, result.size)).decode("ascii")}
        finally:
            self.kernel.LocalFree(result.data)


class Capture:
    def __init__(self, args: argparse.Namespace, native: Native) -> None:
        self.args, self.native = args, native
        self.out = args.output.resolve()
        self.prefix = "native-stencil-" + ("before-" if args.before else "") + ("light-" if args.light else "")
        self.report = {"status": "running", "executable": str(args.exe.resolve()),
                       "mode": "capture-only" if args.before else "native-capture-and-smoke",
                       "theme": "light" if args.light else "dark", "checks": [], "captures": []}
        self.process: subprocess.Popen | None = None
        self.sandbox: Path | None = None
        self.hwnd = 0

    def check(self, name: str, passed: bool, observed) -> None:
        self.report["checks"].append({"name": name, "passed": bool(passed), "observed": observed})
        if not passed:
            raise SmokeError(f"{name}: observed {observed!r}")

    def wait_for(self, description: str, predicate, seconds: float = 10):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            if self.process and self.process.poll() is not None:
                raise SmokeError(f"Application exited ({self.process.returncode}) while waiting for {description}")
            time.sleep(0.1)
        raise SmokeError(f"Timed out waiting for {description}")

    def text(self, control_id: int, parent: int | None = None) -> str:
        return self.native.text(self.native.control(parent or self.hwnd, control_id))

    def screenshot(self, name: str, hwnd: int | None = None) -> None:
        target = hwnd or self.hwnd
        self.native.foreground(target)
        # Move the pointer away from controls so hover does not obscure evidence.
        bounds = self.native.rect(target)
        self.native.user.SetCursorPos(bounds[0] + 8, bounds[1] + 8)
        time.sleep(0.3)
        image = ImageGrab.grab(bbox=bounds, all_screens=True)
        destination = self.out / f"{self.prefix}{name}.png"
        image.save(destination)
        self.report["captures"].append({"name": name, "path": str(destination),
                                         "hwnd": target, "bounds": list(bounds),
                                         "pixels": list(image.size), "method": "foreground desktop crop"})
        print(f"CAPTURE {destination}", flush=True)

    def navigate(self, name: str, control_id: int, caption: str, surface: int) -> None:
        method = self.native.command(self.hwnd, control_id)
        title = self.wait_for(f"{name} page title", lambda: self.native.text(self.hwnd)
                              if self.native.text(self.hwnd).endswith(caption) else None)
        visible = bool(self.native.user.IsWindowVisible(self.native.control(self.hwnd, surface)))
        self.check(f"page-{name}", visible, {"title": title, "surface_id": surface,
                                            "text": self.text(surface), "navigation": method})

    def summary(self, count: int) -> None:
        summary = self.text(220)
        rows = self.native.send(self.native.control(self.hwnd, 204), LB_GETCOUNT)
        self.check(f"summary-{count}-visible", rows == count and "3 个挂载" in summary
                   and "0 个就绪" in summary and f"显示 {count} 个" in summary,
                   {"text": summary, "list_count": rows})

    def launch(self) -> bytes:
        executable = self.args.exe.resolve()
        rclone = ROOT / "rclone" / "rclone.exe"
        if not executable.is_file() or not rclone.is_file():
            raise SmokeError(f"Missing real executable: {executable if not executable.is_file() else rclone}")
        self.sandbox = Path(tempfile.mkdtemp(prefix="rclone-native-capture-"))
        app = self.sandbox / "app"
        app.mkdir()
        conf = self.sandbox / "rclone.conf"
        conf.write_text("[localdemo]\ntype = local\n", encoding="utf-8")
        mounts = []
        fixtures = (("documents", "工作文档", "documents", "writes", False),
                    ("archive", "归档备份 · 2026 项目长期存档", "archive/2026/projects/backups", "full", False),
                    ("reference", "只读资料", "reference", "minimal", True))
        for identifier, name, relative, cache, readonly in fixtures:
            source = self.sandbox / "source" / relative
            source.mkdir(parents=True)
            mounts.append({"id": identifier, "name": name, "remote": f"localdemo:{source.as_posix()}",
                           "mount_point": str(self.sandbox / f"mount-{identifier}"),
                           "vfs_cache_mode": cache, "network_mode": False, "read_only": readonly,
                           "auto_mount": False, "volname": name})
        config = {"schema_version": 2, "rclone_path": str(rclone.resolve()), "rclone_conf": str(conf),
                  "gui_no_auth": False, "gui_user": "", "gui_pass": self.native.protected_empty_password(),
                  "gui_auto_start": False, "startup_enabled": False, "auto_mount_on_boot": False,
                  "mount_delay_seconds": 15, "theme": "light" if self.args.light else "dark",
                  "reduce_motion": False, "mounts": mounts}
        config_path = app / "config.json"
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        original = config_path.read_bytes()
        environment = os.environ.copy()
        environment.update(RCLONE_TRAY_DATA_DIR=str(app), RCLONE_CONFIG=str(conf))
        self.process = subprocess.Popen([str(executable), "--show"], cwd=str(executable.parent),
                                        env=environment, stdin=subprocess.DEVNULL,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.report["pid"] = self.process.pid
        self.report["sandbox"] = str(self.sandbox)
        integrity = self.native.check_integrity(self.process.pid)
        self.check("matching-process-integrity", integrity[0] == integrity[1], list(integrity))
        windows = self.wait_for("PID-scoped RcloneTray.Frontend", lambda:
                                self.native.windows(self.process.pid, "RcloneTray.Frontend"), 20)
        self.hwnd = windows[0]
        self.check("real-native-window", self.native.pid(self.hwnd) == self.process.pid,
                   {"class": "RcloneTray.Frontend", "hwnd": self.hwnd, "pid": self.process.pid})
        self.native.foreground(self.hwnd)
        if not self.args.before:
            self.native.resize(self.hwnd, 1180, 840)
        self.wait_for("real rclone environment probe", lambda:
                      self.text(114) if "空闲" in self.text(114)
                      and re.search(r"\b\d+\.\d+(?:\.\d+)?\b", self.text(114)) else None, 25)
        self.check("configuration-loaded", "配置读取失败" not in self.text(113), self.text(113))
        return original

    def select_row(self, index: int, expected: str) -> None:
        control = self.native.control(self.hwnd, 204)
        rect = W.RECT()
        self.native.send(control, LB_GETITEMRECT, index, C.addressof(rect))
        a, b, _, _ = self.native.client_rect(control)
        point = (a + rect.left + 30, b + (rect.top + rect.bottom) // 2)
        clicked = self.native.pointer(self.hwnd, control, point)
        if not clicked:
            self.native.send(control, LB_SETCURSEL, index)
            self.native.command(self.hwnd, 204, 1, False)
        detail = self.wait_for("selected mount detail", lambda:
                               self.text(205) if expected in self.text(205) else None)
        self.check("mount-selection", self.native.send(control, LB_GETCURSEL) == index,
                   {"index": index, "detail": detail, "method": "pointer" if clicked else "WM_COMMAND"})

    def scroll_to(self, control_id: int, vertical: int = 7, horizontal: int = 7) -> dict:
        child = self.native.control(self.hwnd, control_id)
        before = self.native.rect(child)
        self.native.send(self.hwnd, WM_VSCROLL, vertical)
        self.native.send(self.hwnd, WM_HSCROLL, horizontal)
        time.sleep(0.2)
        # Use the real window scroll handlers, then line-scroll any remaining
        # clipping. Controls need not all fit simultaneously on a narrow page.
        for _ in range(50):
            if self.native.reachable(self.hwnd, child):
                break
            x1, y1, x2, y2 = self.native.rect(child)
            a, b, c, d = self.native.client_rect(self.hwnd)
            previous = (x1, y1, x2, y2)
            if y1 < b or y2 > d:
                self.native.send(self.hwnd, WM_VSCROLL, 0 if y1 < b else 1)
            if x1 < a or x2 > c:
                self.native.send(self.hwnd, WM_HSCROLL, 0 if x1 < a else 1)
            if self.native.rect(child) == previous:
                break
        after = self.native.rect(child)
        return {"control_id": control_id, "before": list(before), "after": list(after),
                "reachable": self.native.reachable(self.hwnd, child)}

    def input_background(self) -> tuple[int, int, int]:
        bounds = self.native.rect(self.native.control(self.hwnd, 401))
        a, b, c, d = bounds
        image = ImageGrab.grab(bbox=(a + 5, b + 4, c - 5, d - 4), all_screens=True).convert("RGB")
        return Counter(image.getdata()).most_common(1)[0][0]

    def smoke(self, original: bytes) -> None:
        self.summary(3)
        self.select_row(1, "归档备份")
        search = self.native.control(self.hwnd, 201)
        self.native.pointer(self.hwnd, search)
        query = C.create_unicode_buffer("no-match-native-smoke-7f6912")
        self.native.send(search, WM_SETTEXT, 0, C.addressof(query))
        self.native.command(self.hwnd, 201, 0x300, False)
        self.wait_for("empty real search results", lambda: self.native.send(
            self.native.control(self.hwnd, 204), LB_GETCOUNT) == 0)
        self.summary(0)
        self.check("no-results-hint", "没有匹配项" in self.text(220), self.text(220))
        self.screenshot("mounts-no-results")
        method = self.native.command(self.hwnd, 203)
        self.wait_for("cleared real search", lambda: self.native.send(
            self.native.control(self.hwnd, 204), LB_GETCOUNT) == 3)
        self.summary(3)
        self.check("search-cleared", self.text(201) == "", {"text": self.text(201), "method": method})
        self.check("search-keeps-stable-selection", "归档备份" in self.text(205)
                   and self.native.send(self.native.control(self.hwnd, 204), LB_GETCURSEL) == 1,
                   {"detail": self.text(205), "selection_index": self.native.send(
                       self.native.control(self.hwnd, 204), LB_GETCURSEL)})
        self.native.pointer(self.hwnd, search)
        self.native.key(0x09)  # Tab from search to the status filter.
        self.native.key(0x28)  # Down selects stopped mounts via the native combo.
        self.check("keyboard-search-to-filter", self.native.send(self.native.control(self.hwnd, 202), CB_GETCURSEL) == 1,
                   {"filter_index": self.native.send(self.native.control(self.hwnd, 202), CB_GETCURSEL), "list_count": self.native.send(self.native.control(self.hwnd, 204), LB_GETCOUNT)})
        self.native.command(self.hwnd, 203)
        for page in PAGES:
            self.navigate(*page)
            self.screenshot(page[0])
        self.navigate(*PAGES[0])
        self.select_row(0, "工作文档")
        method = self.native.command(self.hwnd, 211)
        editor = self.wait_for("PID-scoped native editor", lambda:
                               self.native.windows(self.process.pid, "RcloneTray.MountEditor"))[0]
        self.native.foreground(editor)
        self.check("editor-selected-fields", self.text(601, editor) == "工作文档"
                   and "localdemo:" in self.text(602, editor),
                   {"name": self.text(601, editor), "remote": self.text(602, editor),
                    "mount_point": self.text(603, editor), "opened_by": method})
        self.screenshot("editor", editor)
        method = self.native.command(editor, 621)
        self.wait_for("native editor cancel destruction", lambda: not self.native.user.IsWindow(editor))
        self.check("editor-cancel", not self.native.user.IsWindow(editor), {"method": method})
        size = self.native.resize(self.hwnd, 720, 520)
        self.check("narrow-window-size", size[2] - size[0] == round(720 * self.native.user.GetDpiForWindow(self.hwnd) / 96)
                   and size[3] - size[1] == round(520 * self.native.user.GetDpiForWindow(self.hwnd) / 96), list(size))
        self.screenshot("mounts-narrow")
        observed = self.scroll_to(211)
        self.check("narrow-scrolling-edit-reachable", observed["reachable"], observed)
        # The edit action proves a usable real pointer target after scrolling, without starting mounts.
        method = self.native.command(self.hwnd, 211)
        self.check("narrow-edit-pointer", method == "pointer", {"method": method})
        editor = self.wait_for("narrow real editor", lambda:
                               self.native.windows(self.process.pid, "RcloneTray.MountEditor"))[0]
        self.native.command(editor, 621)
        self.wait_for("narrow editor cancelled", lambda: not self.native.user.IsWindow(editor))
        self.screenshot("mounts-narrow-scrolled")
        footer = self.scroll_to(114)
        self.check("narrow-footer-reachable", footer["reachable"] and "GUI 已停止" in self.text(114),
                   {**footer, "text": self.text(114)})
        actions = [self.scroll_to(control_id) for control_id in (211, 212, 213, 214, 215, 216)]
        self.check("narrow-all-mount-actions-reachable", all(action["reachable"] for action in actions), actions)
        for page in PAGES[1:]:
            self.navigate(*page)
            self.screenshot(f"{page[0]}-narrow")
            if page[0] == "settings":
                bounds = self.native.rect(self.native.control(self.hwnd, 114))
                footer_image = ImageGrab.grab(bbox=bounds, all_screens=True).convert("RGB")
                footer_color = Counter(footer_image.getdata()).most_common(1)[0][0]
                a, b, c, _ = self.native.client_rect(self.hwnd)
                scale = self.native.user.GetDpiForWindow(self.hwnd) / 96
                blank = (c - round(40 * scale), b + round(80 * scale))
                header_color = ImageGrab.grab(bbox=(*blank, blank[0] + 1, blank[1] + 1), all_screens=True).convert("RGB").getpixel((0, 0))
                self.check("narrow-status-strip-painted-above-content", footer_color == header_color,
                           {"footer_rgb": list(footer_color), "header_rgb": list(header_color), "footer_text": self.text(114)})
        self.native.resize(self.hwnd, 1180, 840)
        self.navigate(*PAGES[0])
        self.scroll_to(115, 6, 6)
        search_before = self.native.rect(self.native.control(self.hwnd, 201))
        first = self.native.command(self.hwnd, 115)
        search_after = self.native.rect(self.native.control(self.hwnd, 201))
        self.check("sidebar-toggle-collapses", search_after[0] < search_before[0],
                   {"before": list(search_before), "after": list(search_after), "method": first})
        self.screenshot("sidebar-collapsed")
        second = self.native.command(self.hwnd, 115)
        search_restored = self.native.rect(self.native.control(self.hwnd, 201))
        self.check("sidebar-toggle-restores", search_restored[0] == search_before[0],
                   {"restored": list(search_restored), "method": second})
        self.native.resize(self.hwnd, 1180, 840)
        self.scroll_to(115, 6, 6)
        self.navigate(*PAGES[2])
        self.native.foreground(self.hwnd)
        reduced = self.native.control(self.hwnd, 414)
        initial_check = self.native.send(reduced, 0xF0)
        self.native.command(self.hwnd, 414)
        self.check("reduced-effects-checkbox", self.native.send(reduced, 0xF0) != initial_check,
                   {"before": initial_check, "after": self.native.send(reduced, 0xF0)})
        self.native.command(self.hwnd, 414)
        self.check("reduced-effects-restored", self.native.send(reduced, 0xF0) == initial_check,
                   {"restored": self.native.send(reduced, 0xF0)})
        theme_menu = self.native.control(self.hwnd, 413)
        self.native.send(theme_menu, 0x14F, 1)
        self.screenshot("settings-menu")
        self.native.send(theme_menu, 0x14F, 0)
        before_color = self.input_background()
        combo = self.native.control(self.hwnd, 413)
        initial = self.native.send(combo, CB_GETCURSEL)
        target = 2 if self.args.light else 1
        self.native.send(combo, CB_SETCURSEL, target)
        self.native.command(self.hwnd, 413, 1, False)
        self.wait_for("theme draft enables save", lambda: self.native.user.IsWindowEnabled(
            self.native.control(self.hwnd, 420)))
        time.sleep(0.25)
        preview_color = self.input_background()
        self.check("theme-preview-painted", sum(preview_color) < sum(before_color) - 90 if self.args.light
                   else sum(preview_color) > sum(before_color) + 90,
                   {"initial_index": initial, "preview_index": self.native.send(combo, CB_GETCURSEL),
                    "before_input_rgb": list(before_color), "preview_input_rgb": list(preview_color),
                    "save_enabled": bool(self.native.user.IsWindowEnabled(self.native.control(self.hwnd, 420)))})
        self.screenshot("dark-preview" if self.args.light else "light-preview")
        self.check("theme-preview-not-saved", (self.sandbox / "app" / "config.json").read_bytes() == original,
                   {"sha256": hashlib.sha256((self.sandbox / "app" / "config.json").read_bytes()).hexdigest()})
        method = self.native.command(self.hwnd, 421)
        dialog = self.wait_for("real theme discard confirmation", lambda:
                               self.native.windows(self.process.pid, "#32770"))[0]
        self.check("theme-discard-confirmation", "放弃" in self.native.text(dialog), self.native.text(dialog))
        self.native.command(dialog, 6)
        self.wait_for("theme cancellation restores saved selection", lambda:
                      self.native.send(combo, CB_GETCURSEL) == initial and not self.native.user.IsWindowEnabled(
                          self.native.control(self.hwnd, 420)))
        time.sleep(0.25)
        restored_color = self.input_background()
        self.check("theme-cancel-restores-paint", restored_color == before_color,
                   {"restored_input_rgb": list(restored_color), "initial_input_rgb": list(before_color),
                    "theme_index": self.native.send(combo, CB_GETCURSEL), "method": method})
        self.screenshot("settings-cancelled")
        self.navigate(*PAGES[0])
        self.summary(3)
        for index, expected in enumerate(("工作文档", "归档备份", "只读资料")):
            self.select_row(index, expected)
            detail = self.text(205)
            self.check(f"mount-{index}-never-started", "已停止" in detail and "PID " not in detail, detail)
        footer = self.text(114)
        self.check("footer-idle-stopped", "GUI 已停止" in footer and "空闲" in footer, footer)
        self.check("sandbox-config-unchanged", (self.sandbox / "app" / "config.json").read_bytes() == original,
                   {"sha256": hashlib.sha256(original).hexdigest()})

    def shutdown(self) -> None:
        method = self.native.command(self.hwnd, 112)
        dialog = self.wait_for("real PID-scoped exit confirmation #32770", lambda:
                               self.native.windows(self.process.pid, "#32770"))[0]
        title = self.native.text(dialog)
        self.check("real-exit-confirmation", "退出" in title and self.native.pid(dialog) == self.process.pid,
                   {"class": "#32770", "pid": self.native.pid(dialog), "title": title,
                    "exit_command_id": 112, "method": method})
        self.native.foreground(dialog)
        yes = self.native.control(dialog, 6)
        if not self.native.pointer(dialog, yes):
            self.native.command(dialog, 6, prefer_pointer=False)
        try:
            code = self.process.wait(timeout=20)
        except subprocess.TimeoutExpired as exc:
            raise SmokeError("Launched application did not complete orderly confirmed exit") from exc
        self.check("orderly-process-exit", code == 0, {"exit_code": code, "confirmation_id": 6})

    def run(self) -> int:
        self.out.mkdir(parents=True, exist_ok=True)
        failed = False
        try:
            original = self.launch()
            if self.args.before:
                for page in PAGES:
                    self.navigate(*page)
                    self.screenshot(page[0])
            else:
                self.smoke(original)
            self.shutdown()
            self.report["status"] = "pass"
        except Exception as exc:
            failed = True
            self.report["status"] = "fail"
            self.report["failure"] = f"{type(exc).__name__}: {exc}"
            print(f"FAIL {self.report['failure']}", file=sys.stderr, flush=True)
        finally:
            # Never enumerate/kill existing app or rclone processes. Only our Popen child.
            if self.process is not None and self.process.poll() is None:
                try:
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        self.process.wait(timeout=10)
                    self.report["exception_cleanup_terminated_pid"] = self.process.pid
                except (OSError, subprocess.TimeoutExpired) as exc:
                    failed = True
                    self.report["status"] = "fail"
                    self.report["process_cleanup_failure"] = str(exc)
                    print(f"FAIL Launched-process cleanup: {exc}", file=sys.stderr, flush=True)
            if self.sandbox is not None:
                try:
                    shutil.rmtree(self.sandbox)
                    self.report["sandbox_deleted"] = True
                except OSError as exc:
                    failed = True
                    self.report["status"] = "fail"
                    self.report["cleanup_failure"] = str(exc)
                    print(f"FAIL Sandbox cleanup: {exc}", file=sys.stderr, flush=True)
            destination = self.out / f"{self.prefix}checks.json"
            destination.write_text(json.dumps(self.report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"REPORT {destination}", flush=True)
        return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("output", nargs="?", type=Path, default=ROOT / "docs" / "evidence")
    parser.add_argument("--light", action="store_true", help="seed the optional light-theme variant")
    parser.add_argument("--before", action="store_true", help="capture only four current pages, without restyle smoke/toggle")
    parser.add_argument("--exe", type=Path, default=ROOT / "dist" / "rclone_tray.exe", help="real native executable to launch")
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error("Native capture requires an interactive Windows desktop")
    try:
        native = Native()
    except Exception as exc:
        failure = f"{type(exc).__name__}: {exc}"
        print(f"FAIL {failure}", file=sys.stderr, flush=True)
        args.output.mkdir(parents=True, exist_ok=True)
        prefix = "native-stencil-" + ("before-" if args.before else "") + ("light-" if args.light else "")
        destination = args.output / f"{prefix}checks.json"
        destination.write_text(json.dumps({"status": "fail", "failure": failure,
                                          "checks": [], "captures": []}, indent=2), encoding="utf-8")
        print(f"REPORT {destination}", flush=True)
        return 1
    return Capture(args, native).run()


if __name__ == "__main__":
    raise SystemExit(main())
