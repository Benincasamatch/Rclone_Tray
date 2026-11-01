"""界面截图证据工具：在隔离沙箱中打开主窗口，逐页截图并退出。

用法：
    .venv\\Scripts\\python.exe tools\\capture_ui.py [输出目录] [--light]

默认输出到 docs/evidence/，文件名 stencil-<page>.png（或 stencil-light-<page>.png）。
沙箱使用临时目录与 local remote，不触碰用户配置、不启动挂载；仅证明可见表面，
保存/进程/凭据检查以验收记录中的实际读回为准。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sandbox = Path(tempfile.mkdtemp(prefix="rclone-capture-"))
os.environ["RCLONE_TRAY_DATA_DIR"] = str(sandbox / "app")
os.environ["RCLONE_CONFIG"] = str(sandbox / "rclone.conf")
(sandbox / "rclone.conf").write_text("[localdemo]\ntype = local\n", encoding="utf-8")

import tkinter as tk  # noqa: E402

from PIL import ImageGrab  # noqa: E402

from app.controller import Controller  # noqa: E402
from core.config import AppConfig, MountConfig, save_config  # noqa: E402
from ui import theme as T  # noqa: E402
from ui.settings_window import SettingsWindow  # noqa: E402

args = [a for a in sys.argv[1:] if not a.startswith("--")]
light = "--light" in sys.argv
out = Path(args[0]) if args else ROOT / "docs" / "evidence"
out.mkdir(parents=True, exist_ok=True)
prefix = "stencil-light-" if light else "stencil-"

(sandbox / "source").mkdir()
save_config(AppConfig(
    theme="light" if light else "dark",
    rclone_path=str(ROOT / "rclone" / "rclone.exe"),
    mounts=[
        MountConfig(id="documents", name="工作文档", remote="localdemo:" + str(sandbox / "source"),
                    mount_point=str(sandbox / "mount-documents")),
        MountConfig(id="archive", name="归档备份 · 长名称与路径", remote="localdemo:archive/2026/projects/backups",
                    mount_point=str(sandbox / "mount-archive")),
        MountConfig(id="readonly", name="只读资料", remote="localdemo:reference",
                    mount_point=str(sandbox / "mount-reference"), read_only=True),
    ],
))
T.enable_dpi_awareness()
root = tk.Tk()
controller = Controller(str(ROOT / "main.py"))
window = SettingsWindow(root, controller)
controller.refresh_environment()
root.attributes("-topmost", True)
root.lift()
root.focus_force()
failures: list[str] = []


def pump() -> None:
    for event in controller.drain_events():
        window.handle_event(event)
    window.refresh(controller.snapshot())
    root.after(120, pump)


def capture(name: str, win: tk.Misc | None = None) -> None:
    target = win or root
    target.update_idletasks()
    x, y = target.winfo_rootx(), target.winfo_rooty()
    ImageGrab.grab(bbox=(x, y, x + target.winfo_width(), y + target.winfo_height())).save(out / f"{prefix}{name}.png")
    print("CAPTURE", out / f"{prefix}{name}.png", flush=True)


def guarded(fn):
    def run():
        try:
            fn()
        except Exception as exc:  # 记录而不中断，最后汇总
            failures.append(repr(exc))
            print("FAIL", repr(exc), flush=True)
    return run


def scenario() -> None:
    t = 0
    for page in ("mounts", "service", "settings", "about"):
        root.after(t, guarded(lambda p=page: window.show(p)))
        t += 500
        root.after(t, guarded(lambda p=page: capture(p)))
        t += 200

    def open_editor() -> None:
        window.show("mounts")
        window.mount_list.selection_set("documents")
        window._edit_mount()

    root.after(t, guarded(open_editor))
    t += 900
    root.after(t, guarded(lambda: capture("editor", window.editor.win)))
    t += 300

    def narrow() -> None:
        window.editor.request_close()
        root.geometry(f"{window.th.px(720)}x{window.th.px(480)}")
        window.show("mounts")

    root.after(t, guarded(narrow))
    t += 900
    root.after(t, guarded(lambda: capture("mounts-min")))
    t += 1500
    root.after(t, finish)


def finish() -> None:
    controller.pm.cancel.set()
    controller.pm.stop_all()
    controller.close_executor()
    root.destroy()


root.after(120, pump)
root.after(1500, scenario)
root.mainloop()
shutil.rmtree(sandbox, ignore_errors=True)
print("CAPTURE", "FAIL" if failures else "PASS", flush=True)
sys.exit(1 if failures else 0)
