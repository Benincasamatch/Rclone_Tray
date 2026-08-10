"""tkinter 设置对话框（运行于独立线程，避免阻塞托盘主循环）。"""
from __future__ import annotations

import os
import string
import sys
import threading
import tkinter as tk
import uuid
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Optional

from core import startup as startup_mod
from core.config import (
    AppConfig,
    MountConfig,
    find_rclone_conf,
    list_remotes,
    save_config,
)
from core.rclone import find_rclone, get_version

VFS_MODES = ["off", "minimal", "writes", "full"]


def available_drive_letters() -> list[str]:
    used = {d for d in string.ascii_uppercase if os.path.exists(f"{d}:\\")}
    return [f"{c}:" for c in string.ascii_uppercase if c not in used]


def open_settings(
    cfg: AppConfig,
    pm,
    on_saved: Optional[Callable[[], None]] = None,
    add_mount: bool = False,
) -> None:
    threading.Thread(
        target=_run_dialog, args=(cfg, pm, on_saved, add_mount),
        daemon=True, name="settings-dialog",
    ).start()


def _run_dialog(cfg, pm, on_saved, add_mount) -> None:
    root = tk.Tk()
    SettingsDialog(root, cfg, pm, on_saved, add_mount)
    root.mainloop()


def _script_path() -> str:
    return os.path.abspath(sys.argv[0])


class SettingsDialog:
    def __init__(self, root, cfg, pm, on_saved, add_mount):
        self.root = root
        self.cfg = cfg
        self.pm = pm
        self.on_saved = on_saved
        self._mounts = list(cfg.mounts)
        root.title("rclone_tray 设置")
        root.resizable(False, False)
        self._build()
        if add_mount:
            root.after(200, self._add_mount)
        root.protocol("WM_DELETE_WINDOW", root.destroy)

    # ---------- 界面 ----------
    def _build(self) -> None:
        root = self.root
        pad = {"padx": 8, "pady": 3}
        main = ttk.Frame(root, padding=10)
        main.pack(fill="both", expand=True)

        # rclone 路径
        f1 = ttk.LabelFrame(main, text="rclone", padding=8)
        f1.pack(fill="x", **pad)
        ttk.Label(f1, text="可执行文件：").grid(row=0, column=0, sticky="w")
        self.var_path = tk.StringVar(value=self.cfg.rclone_path)
        ttk.Entry(f1, textvariable=self.var_path, width=44).grid(
            row=0, column=1, sticky="we", padx=4
        )
        ttk.Button(f1, text="自动搜索", command=self._auto_search).grid(row=0, column=2)
        ttk.Button(f1, text="浏览...", command=self._browse).grid(row=0, column=3)
        self.lbl_version = ttk.Label(f1, text="", foreground="#666666")
        self.lbl_version.grid(row=1, column=1, columnspan=3, sticky="w")
        f1.columnconfigure(1, weight=1)
        self._update_version()

        # 开机
        f2 = ttk.LabelFrame(main, text="开机", padding=8)
        f2.pack(fill="x", **pad)
        self.var_startup = tk.BooleanVar(value=startup_mod.is_enabled())
        ttk.Checkbutton(f2, text="开机自启（注册表 HKCU Run）", variable=self.var_startup).grid(
            row=0, column=0, sticky="w"
        )
        self.var_auto_mount = tk.BooleanVar(value=self.cfg.auto_mount_on_boot)
        ttk.Checkbutton(f2, text="开机自动挂载", variable=self.var_auto_mount).grid(
            row=1, column=0, sticky="w"
        )
        ttk.Label(f2, text="挂载延迟（秒）：").grid(row=2, column=0, sticky="w")
        self.var_delay = tk.IntVar(value=self.cfg.mount_delay_seconds)
        ttk.Spinbox(f2, from_=0, to=120, textvariable=self.var_delay, width=8).grid(
            row=2, column=1, sticky="w", padx=4
        )

        # rclone GUI
        f3 = ttk.LabelFrame(main, text="rclone GUI", padding=8)
        f3.pack(fill="x", **pad)
        self.var_gui_auto = tk.BooleanVar(value=self.cfg.gui_auto_start)
        ttk.Checkbutton(f3, text="启动时自动开启 GUI", variable=self.var_gui_auto).grid(
            row=0, column=0, columnspan=2, sticky="w"
        )
        self.var_no_auth = tk.BooleanVar(value=self.cfg.gui_no_auth)
        ttk.Checkbutton(f3, text="免认证（仅本机 localhost）", variable=self.var_no_auth).grid(
            row=1, column=0, columnspan=2, sticky="w"
        )
        ttk.Label(f3, text="用户名：").grid(row=2, column=0, sticky="w")
        self.var_user = tk.StringVar(value=self.cfg.gui_user)
        ttk.Entry(f3, textvariable=self.var_user, width=20).grid(
            row=2, column=1, sticky="w", padx=4
        )
        ttk.Label(f3, text="密码：").grid(row=3, column=0, sticky="w")
        self.var_pass = tk.StringVar(value=self.cfg.gui_pass)
        ttk.Entry(f3, textvariable=self.var_pass, show="*", width=20).grid(
            row=3, column=1, sticky="w", padx=4
        )

        # 挂载
        f4 = ttk.LabelFrame(main, text="挂载", padding=8)
        f4.pack(fill="both", expand=True, **pad)
        self.mount_list = tk.Listbox(f4, height=5, width=52)
        self.mount_list.pack(side="left", fill="both", expand=True)
        btns = ttk.Frame(f4)
        btns.pack(side="left", fill="y", padx=(8, 0))
        ttk.Button(btns, text="添加", command=self._add_mount).pack(fill="x", pady=2)
        ttk.Button(btns, text="编辑", command=self._edit_mount).pack(fill="x", pady=2)
        ttk.Button(btns, text="删除", command=self._del_mount).pack(fill="x", pady=2)
        self._reload_mount_list()

        # 底部
        bt = ttk.Frame(main)
        bt.pack(fill="x", pady=(10, 0))
        ttk.Button(bt, text="保存", command=self._save).pack(side="right", padx=(6, 0))
        ttk.Button(bt, text="取消", command=root.destroy).pack(side="right")

    def _reload_mount_list(self) -> None:
        self.mount_list.delete(0, tk.END)
        for m in self._mounts:
            mode = "" if m.vfs_cache_mode == "off" else f" [{m.vfs_cache_mode}]"
            net = " 网络" if m.network_mode else ""
            self.mount_list.insert(tk.END, f"{m.name} → {m.mount_point}{mode}{net}")

    # ---------- rclone ----------
    def _auto_search(self) -> None:
        exe = find_rclone()
        if exe:
            self.var_path.set(exe)
            self._update_version()
            messagebox.showinfo("rclone_tray", f"找到 rclone：\n{exe}", parent=self.root)
        else:
            messagebox.showwarning(
                "rclone_tray", "未找到 rclone，请手动指定路径。", parent=self.root
            )

    def _browse(self) -> None:
        cur = self.var_path.get()
        p = filedialog.askopenfilename(
            parent=self.root,
            title="选择 rclone.exe",
            filetypes=[("rclone", "rclone.exe"), ("所有文件", "*.*")],
            initialdir=os.path.dirname(cur) if cur else None,
        )
        if p:
            self.var_path.set(p)
            self._update_version()

    def _update_version(self) -> None:
        exe = self.var_path.get().strip()
        ver = get_version(exe) if exe else None
        self.lbl_version.config(text=f"版本：{ver}" if ver else "（未能读取版本）")

    # ---------- 挂载 ----------
    def _remotes(self) -> list[str]:
        return list_remotes(find_rclone_conf()) or ["remote:"]

    def _add_mount(self) -> None:
        dlg = MountDialog(self.root, self._remotes())
        self.root.wait_window(dlg.win)
        if dlg.result:
            self._mounts.append(dlg.result)
            self._reload_mount_list()

    def _edit_mount(self) -> None:
        sel = self.mount_list.curselection()
        if not sel:
            return
        idx = sel[0]
        dlg = MountDialog(self.root, self._remotes(), mount=self._mounts[idx])
        self.root.wait_window(dlg.win)
        if dlg.result:
            self._mounts[idx] = dlg.result
            self._reload_mount_list()

    def _del_mount(self) -> None:
        sel = self.mount_list.curselection()
        if not sel:
            return
        del self._mounts[sel[0]]
        self._reload_mount_list()

    # ---------- 保存 ----------
    def _save(self) -> None:
        cfg = self.cfg
        cfg.rclone_path = self.var_path.get().strip()
        cfg.gui_no_auth = self.var_no_auth.get()
        cfg.gui_user = self.var_user.get().strip()
        cfg.gui_pass = self.var_pass.get().strip()
        cfg.gui_auto_start = self.var_gui_auto.get()
        cfg.auto_mount_on_boot = self.var_auto_mount.get()
        try:
            cfg.mount_delay_seconds = max(0, int(self.var_delay.get() or 0))
        except ValueError:
            cfg.mount_delay_seconds = 15
        cfg.mounts = self._mounts

        want = self.var_startup.get()
        if want and not startup_mod.is_enabled():
            startup_mod.enable(_script_path())
        elif not want and startup_mod.is_enabled():
            startup_mod.disable()
        cfg.startup_enabled = want

        save_config(cfg)
        self.root.destroy()
        if self.on_saved:
            self.on_saved()


class MountDialog:
    """添加/编辑单个挂载。"""

    def __init__(self, parent, remotes: list[str], mount: Optional[MountConfig] = None):
        self.mount = mount
        self.result: Optional[MountConfig] = None
        self.win = tk.Toplevel(parent)
        self.win.title("编辑挂载" if mount else "添加挂载")
        self.win.resizable(False, False)
        self.win.transient(parent)
        self.win.grab_set()

        frm = ttk.Frame(self.win, padding=10)
        frm.pack(fill="both", expand=True)

        m = mount
        letters = available_drive_letters() or ["X:"]
        self.var_name = tk.StringVar(value=m.name if m else "")
        self.var_remote = tk.StringVar(
            value=m.remote if m else (remotes[0] if remotes else "remote:")
        )
        self.var_mp = tk.StringVar(value=m.mount_point if m else letters[0])
        self.var_mode = tk.StringVar(value=m.vfs_cache_mode if m else "writes")
        self.var_network = tk.BooleanVar(value=m.network_mode if m else False)
        self.var_ro = tk.BooleanVar(value=m.read_only if m else False)
        self.var_auto = tk.BooleanVar(value=m.auto_mount if m else True)
        self.var_volname = tk.StringVar(value=m.volname if m else "")

        row = 0
        ttk.Label(frm, text="名称：").grid(row=row, column=0, sticky="w", pady=2)
        ttk.Entry(frm, textvariable=self.var_name, width=30).grid(
            row=row, column=1, sticky="we", pady=2
        )
        row += 1
        ttk.Label(frm, text="Remote：").grid(row=row, column=0, sticky="w", pady=2)
        ttk.Combobox(frm, textvariable=self.var_remote, values=remotes, width=28).grid(
            row=row, column=1, sticky="we", pady=2
        )
        row += 1
        ttk.Label(frm, text="挂载点（盘符或路径）：").grid(row=row, column=0, sticky="w", pady=2)
        ttk.Combobox(
            frm, textvariable=self.var_mp, values=letters + ["C:\\mount\\dir"], width=28
        ).grid(row=row, column=1, sticky="we", pady=2)
        row += 1
        ttk.Label(frm, text="VFS 缓存模式：").grid(row=row, column=0, sticky="w", pady=2)
        ttk.Combobox(
            frm, textvariable=self.var_mode, values=VFS_MODES, width=28, state="readonly"
        ).grid(row=row, column=1, sticky="we", pady=2)
        row += 1
        ttk.Label(frm, text="卷标（可选）：").grid(row=row, column=0, sticky="w", pady=2)
        ttk.Entry(frm, textvariable=self.var_volname, width=30).grid(
            row=row, column=1, sticky="we", pady=2
        )
        row += 1
        ttk.Checkbutton(
            frm, text="网络驱动器模式（--network-mode）", variable=self.var_network
        ).grid(row=row, column=0, columnspan=2, sticky="w", pady=2)
        row += 1
        ttk.Checkbutton(frm, text="只读", variable=self.var_ro).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=2
        )
        row += 1
        ttk.Checkbutton(frm, text="开机自动挂载", variable=self.var_auto).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=2
        )
        row += 1

        bt = ttk.Frame(frm)
        bt.grid(row=row, column=0, columnspan=2, sticky="e", pady=(8, 0))
        ttk.Button(bt, text="确定", command=self._ok).pack(side="right", padx=(6, 0))
        ttk.Button(bt, text="取消", command=self.win.destroy).pack(side="right")

    def _ok(self) -> None:
        name = self.var_name.get().strip() or self.var_remote.get().strip().rstrip(":")
        remote = self.var_remote.get().strip()
        if not name:
            messagebox.showwarning("rclone_tray", "请填写名称。", parent=self.win)
            return
        if not remote:
            messagebox.showwarning("rclone_tray", "请填写 Remote（如 gdrive:）。", parent=self.win)
            return
        if not remote.endswith(":"):
            remote += ":"
        mp = self.var_mp.get().strip()
        if not mp:
            messagebox.showwarning(
                "rclone_tray", "请填写挂载点（如 X: 或 D:\\mount\\dir）。", parent=self.win
            )
            return
        self.result = MountConfig(
            id=self.mount.id if self.mount else uuid.uuid4().hex[:12],
            name=name,
            remote=remote,
            mount_point=mp,
            vfs_cache_mode=self.var_mode.get(),
            network_mode=self.var_network.get(),
            read_only=self.var_ro.get(),
            auto_mount=self.var_auto.get(),
            volname=self.var_volname.get().strip(),
        )
        self.win.destroy()
