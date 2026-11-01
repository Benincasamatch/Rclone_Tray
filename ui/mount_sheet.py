"""Single-root, asynchronous mount editor. Persistence belongs to Controller."""
from __future__ import annotations

import copy
import os
import string
import tkinter as tk
import uuid
from tkinter import filedialog
from typing import Callable

from core.config import MountConfig
from . import theme as T
from . import dialogs as messagebox
from .glass import Backdrop, Clear, GlassPanel
from .widgets import (Button, Caption, ComboField, DropDown, Heading, Label, ScrollFrame,
                      TextField, ToggleRow)
from .window import apply_native_chrome, center_on_owner

CARD_PAD = 20  # 卡片内边距（逻辑像素）


def available_drive_letters() -> list[str]:
    return [f"{letter}:" for letter in string.ascii_uppercase[2:]
            if not os.path.exists(f"{letter}:\\")]


class MountSheet:
    """A modeless API with a modal grab; results arrive through handle_event.

    Public automation handles: win, fields (name/remote/mount_point/volname/
    vfs_cache_mode/network_mode/read_only/auto_mount), vars, save_button,
    cancel_button, rebase_button, error_var, note_var; apply_theme() re-renders
    after a theme switch. on_closed receives saved mount ID or None. No
    wait_window is needed in the application event pump.
    """

    def __init__(self, parent: tk.Misc, controller, mount: MountConfig | None,
                 revision: int, on_closed: Callable[[str | None], None] | None = None):
        self.parent = parent.winfo_toplevel()
        self.controller = controller
        self.mount = copy.deepcopy(mount)
        self.mount_id = mount.id if mount else uuid.uuid4().hex[:12]
        self.revision = revision
        self.on_closed = on_closed
        self.pending = False
        self.closed = False
        self._old_focus = self.parent.focus_get()
        self._old_grab = self.parent.grab_current()
        self.win = tk.Toplevel(parent)
        self.win.withdraw()
        self.th = T.theme(self.win)
        self.win.title("编辑挂载" if mount else "添加挂载")
        self.win.configure(bg=T.BASE)
        self.win.transient(self.parent)
        self.win.minsize(self.th.px(480), self.th.px(380))
        self.win.protocol("WM_DELETE_WINDOW", self.request_close)
        self.win.bind("<Escape>", self._escape)
        self.win.bind("<Control-s>", self._save_key)
        self.win.bind("<Tab>", self._tab)
        self.win.bind("<Shift-Tab>", lambda e: self._tab(e, backwards=True))
        self.win.bind("<ISO_Left_Tab>", lambda e: self._tab(e, backwards=True))
        values = {
            "name": mount.name if mount else "",
            "remote": mount.remote if mount else "",
            "mount_point": mount.mount_point if mount else "",
            "volname": mount.volname if mount else "",
            "vfs_cache_mode": mount.vfs_cache_mode if mount else "writes",
            "network_mode": mount.network_mode if mount else False,
            "read_only": mount.read_only if mount else False,
            "auto_mount": mount.auto_mount if mount else True,
        }
        self.vars = {key: (tk.BooleanVar(self.win, value=value) if isinstance(value, bool)
                           else tk.StringVar(self.win, value=value))
                     for key, value in values.items()}
        self.baseline = values.copy()
        self.fields: dict[str, tk.Widget] = {}
        self.error_var = tk.StringVar(self.win)
        self.note_var = tk.StringVar(self.win, value="仅保存配置，不会自动启动挂载。")
        self._build()
        for variable in self.vars.values():
            variable.trace_add("write", self._changed)
        self.refresh(controller.snapshot())
        self.win.update_idletasks()
        _left, _top, area_w, area_h = T.work_area()
        w = min(self.th.px(600), area_w - 24)
        h = min(self.th.px(720), area_h - 24)
        center_on_owner(self.win, self.parent, w, h)
        self.win.deiconify()
        apply_native_chrome(self.win, caption_color=T.BASE)
        self.win.grab_set()
        self.fields["name"].focus_set()

    def _build(self) -> None:
        px = self.th.px
        pad = px(T.SPACE_XL)
        inner = px(CARD_PAD)
        self.backdrop = Backdrop(self.win)
        self.backdrop.pack(fill="both", expand=True)
        header = Clear(self.backdrop)
        header.pack(fill="x", padx=pad, pady=(pad, px(T.SPACE_LG)))
        Heading(header, "编辑挂载" if self.mount else "添加挂载", size=T.FONT_PAGE).pack(anchor="w")
        self.note = Caption(header, textvariable=self.note_var, wrap=px(530))
        self.note.pack(anchor="w", pady=(px(T.SPACE_XS), 0))
        # 底部自上而下：错误信息 → 版本恢复行（仅冲突时）→ 操作按钮
        footer = Clear(self.backdrop)
        footer.pack(side="bottom", fill="x", padx=pad, pady=(px(T.SPACE_MD), pad))
        self.error_label = Label(footer, textvariable=self.error_var, size=T.FONT_CAPTION,
                                 token="CRITICAL", wrap=px(530))
        self.error_label.pack(anchor="w", pady=(0, px(T.SPACE_SM)))
        self.actions = Clear(footer)
        self.actions.pack(fill="x")
        self.save_button = Button(self.actions, "保存挂载", self.save, variant="primary")
        self.save_button.pack(side="right")
        self.cancel_button = Button(self.actions, "取消", self.request_close, variant="secondary")
        self.cancel_button.pack(side="right", padx=(0, px(T.SPACE_SM)))
        self.recovery = Clear(footer)
        self.rebase_button = Button(self.recovery, "保留输入，更新版本", self.rebase, variant="secondary")
        self.rebase_button.pack(side="left")
        # 主体：可滚动的两张玻璃卡片
        self.scroll = ScrollFrame(self.backdrop)
        self.scroll.pack(fill="both", expand=True, padx=pad)
        body = self.scroll.body

        def card(title: str) -> GlassPanel:
            panel = GlassPanel(body, "PANEL")
            panel.pack(fill="x", pady=(0, px(T.SPACE_LG)))
            Heading(panel, title).pack(anchor="w", padx=inner, pady=(inner, px(T.SPACE_MD)))
            return panel

        def field(panel: GlassPanel, key: str, label: str, make: Callable[[tk.Misc], tk.Widget]) -> tk.Widget:
            Label(panel, label).pack(anchor="w", padx=inner, pady=(0, px(6)))
            widget = make(panel)
            widget.pack(fill="x", padx=inner, pady=(0, px(T.SPACE_MD)))
            self.fields[key] = widget
            return widget

        connection = card("连接位置")
        field(connection, "name", "名称", lambda m: TextField(m, self.vars["name"]))
        field(connection, "remote", "Remote · remote:path", lambda m: ComboField(m, self.vars["remote"]))
        field(connection, "mount_point", "挂载位置 · 盘符或空目录",
              lambda m: ComboField(m, self.vars["mount_point"], values=available_drive_letters()))
        field(connection, "volname", "卷标（可选）", lambda m: TextField(m, self.vars["volname"]))
        location_actions = tk.Frame(connection, bg=connection.fill, bd=0, highlightthickness=0)
        location_actions.pack(fill="x", padx=inner)
        self.browse_button = Button(location_actions, "选择空目录…", self._browse, variant="secondary")
        self.browse_button.pack(side="left")
        self.config_button = Button(location_actions, "打开 rclone 配置", self.controller.open_rclone_config,
                                    variant="secondary")
        self.config_button.pack(side="left", padx=(px(T.SPACE_SM), 0))
        self.remote_note = Caption(connection, "", wrap=px(480))
        self.remote_note.pack(anchor="w", padx=inner, pady=(px(T.SPACE_MD), inner))
        self.reload_button = Button(connection, "修复后重新载入应用配置", self._reload_config, variant="secondary")

        behavior = card("挂载行为")
        field(behavior, "vfs_cache_mode", "VFS 缓存模式",
              lambda m: DropDown(m, self.vars["vfs_cache_mode"],
                                 [(mode, mode) for mode in ("off", "minimal", "writes", "full")]))
        for key, label in (("network_mode", "网络驱动器模式"), ("read_only", "只读 · 禁止写入远端"),
                           ("auto_mount", "开机自动挂载")):
            row = ToggleRow(behavior, label, self.vars[key])
            row.pack(fill="x", padx=inner, pady=(0, px(T.SPACE_MD)))
            self.fields[key] = row
        self.behavior_note = Caption(behavior, "自动挂载还需设置页启用全局策略。", wrap=px(480))
        self.behavior_note.pack(anchor="w", padx=inner, pady=(px(T.SPACE_XS), inner))
        self.win.bind("<Configure>", self._resize, add="+")
        for widget in self.fields.values():
            focusable = getattr(widget, "entry", None) or getattr(widget, "switch", None) or widget
            focusable.bind("<FocusIn>", lambda _e, target=widget: self._ensure_visible(target), add="+")

    def _resize(self, event) -> None:
        if event.widget is self.win:
            outer = max(240, event.width - self.th.px(2 * T.SPACE_XL))
            inner = max(200, outer - self.th.px(2 * CARD_PAD))
            self.note.configure(wraplength=outer)
            self.error_label.configure(wraplength=outer)
            self.remote_note.configure(wraplength=inner)
            self.behavior_note.configure(wraplength=inner)

    def _ensure_visible(self, field: tk.Widget) -> None:
        if not self.closed:
            self.scroll.ensure_visible(field)

    def _values(self) -> dict:
        return {key: variable.get() for key, variable in self.vars.items()}

    @property
    def dirty(self) -> bool:
        return self._values() != self.baseline

    def _changed(self, *_args) -> None:
        if not self.pending:
            self.note_var.set("有未保存的挂载输入。" if self.dirty else
                              "保存不会启动挂载；取消不会撤销已执行的运行操作。")

    def _browse(self) -> None:
        path = filedialog.askdirectory(parent=self.win, title="选择空的挂载目录")
        if path:
            self.vars["mount_point"].set(path)

    def _reload_config(self) -> None:
        if self.pending:
            return
        if messagebox.askyesno("重新载入配置", "重新读取修复后的应用配置？当前挂载输入会保留。\n"
                              "重新载入成功后请更新保存版本再保存。", parent=self.win, default="no"):
            self.controller.reload_config()

    def refresh(self, snapshot: dict) -> None:
        if self.closed:
            return
        self._snapshot = snapshot
        remotes = snapshot.get("remotes", [])
        self.fields["remote"].set_values(remotes)
        self.remote_note.configure(text=(f"已检测到 {len(remotes)} 个 remote；可直接输入其子路径。" if remotes
                                        else "没有检测到 remote。先在 rclone 配置中创建 remote；这里不会创建远端。"))
        busy = snapshot.get("busy", set())
        disabled = (self.pending or self.mount_id in busy or "mounts" in busy or "settings" in busy
                    or bool(snapshot.get("config_error")) or snapshot.get("closing", False))
        self.save_button.set_enabled(not disabled)
        self.cancel_button.set_enabled(not self.pending)
        conflict = (not self.pending and not snapshot.get("config_error") and not snapshot.get("closing")
                    and self.revision != snapshot["revision"])
        self.rebase_button.set_enabled(conflict)
        if conflict:
            self.recovery.pack(fill="x", pady=(0, self.th.px(T.SPACE_SM)), before=self.actions)
        else:
            self.recovery.pack_forget()
        self.save_button.set_text("正在保存…" if self.pending else "保存挂载")
        self.reload_button.set_enabled(not (self.pending or "settings" in busy or snapshot.get("closing")))
        inner = self.th.px(CARD_PAD)
        if snapshot.get("config_error"):
            self.error_var.set("配置读取失败，保存已禁用。请修复后在主窗口重新载入配置。")
            self.remote_note.pack_configure(pady=(self.th.px(T.SPACE_MD), self.th.px(T.SPACE_SM)))
            self.reload_button.pack(anchor="w", padx=inner, pady=(0, inner))
        else:
            self.reload_button.pack_forget()
            self.remote_note.pack_configure(pady=(self.th.px(T.SPACE_MD), inner))

    def rebase(self) -> None:
        if self.pending:
            return
        snapshot = self.controller.snapshot()
        if snapshot.get("config_error"):
            return
        if messagebox.askyesno("更新保存版本", "保留当前输入并使用最新配置版本？\n"
                              "下一次保存会以这些输入更新此挂载，其他挂载不会被覆盖。", parent=self.win, default="no"):
            self.revision = snapshot["revision"]
            self.error_var.set("已更新版本，输入保留；请再次保存。")
            self.refresh(snapshot)

    def save(self) -> None:
        snapshot = self.controller.snapshot()
        if (self.pending or snapshot.get("closing") or snapshot.get("config_error")
                or self.mount_id in snapshot.get("busy", set()) or "mounts" in snapshot.get("busy", set())
                or "settings" in snapshot.get("busy", set())):
            self.refresh(snapshot)
            return
        values = self._values()
        for key, label in (("name", "名称"), ("remote", "Remote"), ("mount_point", "挂载点")):
            if not values[key].strip():
                self.error_var.set(f"请填写{label}。")
                self.fields[key].focus_set()
                return
        if ":" not in values["remote"] or not values["remote"].split(":", 1)[0].strip():
            self.error_var.set("Remote 必须是 remote:path 格式，例如 cloud: 或 cloud:folder。")
            self.fields["remote"].focus_set()
            return
        mount = MountConfig(id=self.mount_id, **values)
        self.pending = True
        self.error_var.set("")
        self.note_var.set("正在保存挂载配置…")
        self._lock_fields(True)
        self.refresh(snapshot)
        try:
            accepted = self.controller.save_mount(mount, self.revision)
            if accepted is False:
                self.pending = False
                self._lock_fields(False)
                self.error_var.set("此挂载正在执行其他操作，未提交保存；请稍后重试。")
                self._changed()
                self.refresh(self.controller.snapshot())
        except Exception as exc:
            self.pending = False
            self._lock_fields(False)
            self.error_var.set(str(exc))
            self._changed()
            self.refresh(self.controller.snapshot())

    def _lock_fields(self, lock: bool) -> None:
        for field in self.fields.values():
            field.set_enabled(not lock)
        self.browse_button.set_enabled(not lock)

    def handle_event(self, event: dict) -> bool:
        if (self.closed or not self.pending or event.get("action") != "save_mount"
                or event.get("key") != self.mount_id):
            return False
        self.pending = False
        if event.get("ok"):
            self._close(self.mount_id)
        else:
            self._lock_fields(False)
            self.error_var.set(event.get("message") or "保存失败，输入已保留。")
            self._changed()
            self.refresh(self.controller.snapshot())
        return True

    def request_close(self) -> bool:
        if self.closed:
            return True
        if self.pending:
            self.error_var.set("正在保存，请等待操作结果后再关闭。")
            return False
        if self.dirty and not messagebox.askyesno("放弃挂载输入？", "当前挂载有未保存输入。确认放弃？",
                                                 parent=self.win, default="no"):
            return False
        self._close(None)
        return True

    def apply_theme(self) -> None:
        """主题切换：重建环境光并级联重绘子控件，标题栏颜色同步。"""
        if self.closed:
            return
        self.win.configure(bg=T.BASE)
        self.backdrop.apply_theme()
        apply_native_chrome(self.win, caption_color=T.BASE)

    def _close(self, saved_id: str | None) -> None:
        if self.closed:
            return
        self.closed = True
        self.win.grab_release()
        self.win.destroy()
        if self._old_grab is not None and self._old_grab.winfo_exists():
            self._old_grab.grab_set()
        if self._old_focus is not None and self._old_focus.winfo_exists():
            self._old_focus.focus_set()
        else:
            self.parent.focus_set()
        if self.on_closed:
            self.on_closed(saved_id)

    def _escape(self, _event=None) -> str:
        if self.win.grab_current() is self.win:
            self.request_close()
        return "break"

    def _save_key(self, _event=None) -> str:
        if self.win.grab_current() is self.win:
            self.save()
        return "break"

    def _tab(self, event, backwards: bool = False) -> str:
        if self.win.grab_current() is not self.win:
            return "break"
        target = event.widget.tk_focusPrev() if backwards else event.widget.tk_focusNext()
        if target is not None and str(target).startswith(str(self.win) + "."):
            target.focus_set()
        else:
            (self.cancel_button if backwards else self.fields["name"]).focus_set()
        return "break"
