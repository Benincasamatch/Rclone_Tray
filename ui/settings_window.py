"""主窗口：Tern · Stencil 外壳（侧栏 / 内容区 / 右侧详情 / 底部状态条）与四个页面。

Controller 是唯一的持久化、运行与外部打开边界；refresh 只就地更新事实，
草稿输入与阅读位置不被后台刷新重建。
"""
from __future__ import annotations

import copy
import tkinter as tk
from tkinter import filedialog

from PIL import ImageTk

from core.config import config_path, logs_dir, find_rclone_conf
from core.resources import icon_path
from . import theme as T
from .animate import set_reduce_motion
from .brand import app_mark
from . import dialogs as messagebox
from .glass import Backdrop, Clear, GlassPanel, rerender_within
from .mount_sheet import MountSheet
from .widgets import (Button, Caption, Divider, DropDown, Heading, IconButton, Label,
                      MountList, ScrollFrame, TextArea, TextField, ToggleRow, Tooltip)
from .window import apply_native_chrome, center_on_work_area, flash_taskbar

PAGES = (("mounts", "挂载"), ("service", "rclone GUI"),
         ("settings", "设置"), ("about", "关于与诊断"))
PAGE_ICONS = {"mounts": T.Icons.DRIVE, "service": T.Icons.CLOUD,
              "settings": T.Icons.SETTINGS, "about": T.Icons.INFO}
STATE_NAMES = {"stopped": "已停止", "starting": "启动中", "ready": "就绪",
               "stopping": "停止中", "failed": "失败"}
STATE_TONES = {"stopped": "idle", "starting": "caution", "ready": "success",
               "stopping": "caution", "failed": "critical"}
STATE_TOKENS = {"ready": "SUCCESS", "failed": "CRITICAL", "starting": "CAUTION", "stopping": "CAUTION"}
THEME_OPTIONS = (("跟随系统", "system"), ("浅色", "light"), ("深色", "dark"))
ALL_STATES = "全部状态"


class SettingsWindow:
    """Public lifecycle: show/hide/refresh/handle_event/request_exit.

    Automation handles: nav_buttons[page], search_field (TextField; .entry) /
    search_var, state_filter (DropDown) / state_var, mount_list (MountList,
    row id = stable mount ID), selected_id, mount_buttons, gui_buttons,
    fields[page][config_field], vars[page][config_field], save_button /
    discard_button / rebase_button, reload_button, message_var, editor
    (MountSheet or None), about_text (TextArea), pages[page]. Buttons expose
    invoke() and enabled.
    """

    def __init__(self, root: tk.Tk, controller):
        self.root = root
        self.controller = controller
        self.snapshot = controller.snapshot()
        self.page = "mounts"
        self.selected_id = ""
        self.editor: MountSheet | None = None
        self._loading = False
        self._saving_page: str | None = None
        self._detail_text = ""
        self.vars: dict[str, dict[str, tk.Variable]] = {"settings": {}, "service": {}}
        self.fields: dict[str, dict[str, tk.Widget]] = {"settings": {}, "service": {}}
        self.baselines: dict[str, dict] = {}
        self.revisions: dict[str, int] = {}
        self.form_errors = {"settings": "", "service": ""}
        self.message_var = tk.StringVar(root)
        self.config_error_var = tk.StringVar(root)
        self.environment_var = tk.StringVar(root)
        self.footer_var = tk.StringVar(root)
        self.gui_status_var = tk.StringVar(root)
        self.gui_message_var = tk.StringVar(root)
        self.gui_saved_var = tk.StringVar(root)
        self.summary_var = tk.StringVar(root)
        self.empty_var = tk.StringVar(root)
        self.selection_var = tk.StringVar(root)
        self.search_var = tk.StringVar(root)
        self.state_var = tk.StringVar(root, value=ALL_STATES)
        self.status_var = tk.StringVar(root)
        self.metric_vars = {key: tk.StringVar(root, value="—") for key in ("mounts", "ready", "remotes")}
        self._known_mount_ids: set[str] = set()
        self._nav_collapsed = False
        self._compact_nav = False
        self._detail_visible = True
        self._wide_mounts = None
        self._rerender_job = None
        cfg = self.snapshot["config"]
        T.set_mode(cfg.theme)
        set_reduce_motion(cfg.reduce_motion)
        self.th = T.theme(root)
        root.title("rclone_tray")
        root.configure(bg=T.BASE)
        root.minsize(self.th.px(720), self.th.px(480))
        center_on_work_area(root, min(self.th.px(1120), T.work_area()[2] - 24),
                            min(self.th.px(780), T.work_area()[3] - 24))
        path = icon_path()
        if path:
            try:
                root.iconbitmap(default=path)
            except tk.TclError:
                pass
        root.protocol("WM_DELETE_WINDOW", self.hide)
        root.bind("<Escape>", self._escape, add="+")
        root.bind("<Control-s>", self._save_key, add="+")
        root.bind("<Control-f>", self._search_key, add="+")
        root.bind("<Control-n>", self._add_key, add="+")
        self._build_shell()
        self._build_mounts()
        self._build_service()
        self._build_settings()
        self._build_about()
        self._load_form("settings", self.snapshot)
        self._load_form("service", self.snapshot)
        self._route("mounts")
        self.refresh(self.snapshot)
        self._apply_theme()

    # ------------------------------------------------------------------ 外壳
    def _build_shell(self) -> None:
        px = self.th.px
        self.backdrop = Backdrop(self.root)
        self.backdrop.pack(fill="both", expand=True)
        # 侧栏：半透明、与内容区之间一条微弱分隔线
        self.sidebar = GlassPanel(self.backdrop, "SIDEBAR", radius=0, border=False, width=px(T.NAV_WIDTH))
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)
        self.brand_row = Clear(self.sidebar)
        self.brand_row.pack(fill="x", padx=px(T.SPACE_LG), pady=(px(T.SPACE_XL), px(T.SPACE_XS)))
        self._brand_photo = ImageTk.PhotoImage(app_mark(px(28)), master=self.root)
        self.brand_mark = tk.Label(self.brand_row, image=self._brand_photo, bd=0, padx=0, pady=0)
        self.brand_mark.pack(side="left")
        self.brand = Heading(self.brand_row, "rclone", size=18)
        self.brand.pack(side="left", padx=(px(T.SPACE_SM), 0))
        self.brand_note = Caption(self.sidebar, "本机存储工作空间", token="TEXT_TERTIARY")
        self.brand_note.pack(anchor="w", padx=px(T.SPACE_LG), pady=(0, px(T.SPACE_XL)))
        self.nav_buttons = {}
        for page, label in PAGES:
            button = Button(self.sidebar, label, lambda key=page: self.show(key), variant="ghost",
                            size="lg", glyph=PAGE_ICONS[page], align="left", width=T.NAV_WIDTH - T.SPACE_MD * 2)
            button.pack(fill="x", padx=px(T.SPACE_MD), pady=px(2))
            self.nav_buttons[page] = button
            Tooltip(button, label)
        self.collapse_button = Button(self.sidebar, "收起导航", self._toggle_nav, variant="ghost",
                                      glyph=T.Icons.CHEVRON_LEFT, align="left", width=T.NAV_WIDTH - T.SPACE_MD * 2,
                                      font_size=T.FONT_CAPTION)
        self.collapse_button.pack(side="bottom", fill="x", padx=px(T.SPACE_MD), pady=(px(2), px(T.SPACE_MD)))
        Tooltip(self.collapse_button, "折叠 / 展开导航（窄窗口自动折叠）")
        self.quit_button = Button(self.sidebar, "退出应用…", self.request_exit, variant="ghost",
                                  glyph=T.Icons.POWER, align="left", width=T.NAV_WIDTH - T.SPACE_MD * 2)
        self.quit_button.pack(side="bottom", fill="x", padx=px(T.SPACE_MD), pady=px(2))
        Tooltip(self.quit_button, "退出应用")
        self.nav_note = Caption(self.sidebar, "关闭窗口后继续驻留托盘", token="TEXT_TERTIARY", wrap=px(170))
        self.nav_note.pack(side="bottom", anchor="w", padx=px(T.SPACE_LG), pady=px(T.SPACE_MD))
        Divider(self.backdrop, vertical=True).pack(side="left", fill="y")
        # 工作区
        self.workspace = Clear(self.backdrop)
        self.workspace.pack(side="left", fill="both", expand=True)
        header = Clear(self.workspace)
        header.pack(fill="x", padx=px(T.SPACE_XL), pady=(px(T.SPACE_MD), px(T.SPACE_SM)))
        Caption(header, "工作空间 / 本机", token="TEXT_TERTIARY").pack(side="left")
        self.hide_button = Button(header, "隐藏至托盘", self.hide, variant="ghost", glyph=T.Icons.HIDE, size="sm")
        self.hide_button.pack(side="right")
        self.reload_button = Button(header, "重新载入配置", self._reload_config, variant="ghost", glyph=T.Icons.REFRESH, size="sm")
        self.reload_button.pack(side="right", padx=(0, px(T.SPACE_SM)))
        self.config_bar = Clear(self.workspace)
        Label(self.config_bar, T.resolve_icon(T.Icons.WARNING), token="CRITICAL", size=14, icon=True).pack(side="left", padx=(0, px(T.SPACE_SM)))
        self.config_label = Label(self.config_bar, textvariable=self.config_error_var, token="CRITICAL", wrap=px(600))
        self.config_label.pack(side="left")
        self.message_label = Caption(self.workspace, textvariable=self.message_var, token="TEXT_SECONDARY", wrap=px(600))
        self.message_var.trace_add("write", lambda *_: self._show_message())
        # 底部状态条
        status = Clear(self.workspace)
        status.pack(side="bottom", fill="x")
        Divider(status).pack(fill="x")
        self.status_label = Caption(status, textvariable=self.status_var, token="TEXT_TERTIARY", size=T.FONT_LABEL)
        self.status_label.pack(anchor="w", padx=px(T.SPACE_XL), pady=px(T.SPACE_SM))
        # 表单保存条（仅设置/服务页显示）
        self.form_footer = Clear(self.workspace)
        self.save_button = Button(self.form_footer, "保存更改", self._save_form, variant="primary")
        self.save_button.pack(side="right")
        self.discard_button = Button(self.form_footer, "放弃更改", self._discard_form, variant="ghost")
        self.discard_button.pack(side="right", padx=px(T.SPACE_SM))
        self.rebase_button = Button(self.form_footer, "保留输入，更新版本", self._rebase_form, variant="secondary")
        self.footer_label = Caption(self.form_footer, textvariable=self.footer_var, wrap=px(480))
        self.footer_label.pack(side="left", padx=(0, px(T.SPACE_LG)))
        self.content = Clear(self.workspace)
        self.content.pack(fill="both", expand=True, padx=px(T.SPACE_XL), pady=(px(T.SPACE_SM), px(T.SPACE_LG)))
        self.content.rowconfigure(0, weight=1)
        self.content.columnconfigure(0, weight=1)
        self.pages: dict[str, tk.Widget] = {}
        self.root.bind("<Configure>", self._resize, add="+")

    def _schedule_rerender(self) -> None:
        """布局变化（折叠导航、切页、详情栏位置）后重绘玻璃面板。"""
        if self._rerender_job is None:
            self._rerender_job = self.root.after_idle(self._rerender)

    def _rerender(self) -> None:
        self._rerender_job = None
        self.backdrop.rerender()

    def _show_message(self) -> None:
        if self.message_var.get():
            self.message_label.pack(anchor="w", padx=self.th.px(T.SPACE_XL), pady=(0, self.th.px(T.SPACE_SM)), before=self.content)
        else:
            self.message_label.pack_forget()

    def _toggle_nav(self) -> None:
        self._nav_collapsed = not self._nav_collapsed
        self._layout_nav()

    def _layout_nav(self) -> None:
        px = self.th.px
        collapsed = self._nav_collapsed or self._compact_nav
        self.sidebar.configure(width=px(T.NAV_COLLAPSED if collapsed else T.NAV_WIDTH))
        width = px((T.NAV_COLLAPSED if collapsed else T.NAV_WIDTH) - T.SPACE_MD * 2)
        for page, label in PAGES:
            self.nav_buttons[page].configure(width=width)
            self.nav_buttons[page].set_text("" if collapsed else label)
        for button, label in ((self.collapse_button, "收起导航"), (self.quit_button, "退出应用…")):
            button.configure(width=width)
            button.set_text("" if collapsed else label)
        self.collapse_button.set_glyph(T.Icons.CHEVRON_RIGHT if collapsed else T.Icons.CHEVRON_LEFT)
        if collapsed:
            self.brand.pack_forget()
            self.brand_note.pack_forget()
            self.nav_note.pack_forget()
            self.brand_row.pack_configure(padx=px(T.SPACE_LG))
        else:
            self.brand.pack(side="left", padx=(px(T.SPACE_SM), 0))
            self.brand_note.pack(anchor="w", padx=px(T.SPACE_LG), pady=(0, px(T.SPACE_XL)), after=self.brand_row)
            self.nav_note.pack(side="bottom", anchor="w", padx=px(T.SPACE_LG), pady=px(T.SPACE_MD), after=self.quit_button)
        self._schedule_rerender()

    def _heading(self, parent, title: str, subtitle: str = "") -> Clear:
        row = Clear(parent)
        row.pack(fill="x", pady=(0, self.th.px(T.SPACE_LG)))
        text = Clear(row)
        text.pack(side="left", fill="x", expand=True)
        Heading(text, title, size=T.FONT_PAGE).pack(anchor="w")
        if subtitle:
            caption = Caption(text, subtitle, wrap=self.th.px(650))
            caption.pack(anchor="w", pady=(self.th.px(T.SPACE_XS), 0))
            text.bind("<Configure>", lambda event: caption.configure(wraplength=max(180, event.width)))
        return row

    def _new_scroll_page(self, key: str, title: str, subtitle: str) -> tk.Widget:
        page = ScrollFrame(self.content)
        page.grid(row=0, column=0, sticky="nsew")
        self.pages[key] = page
        self._heading(page.body, title, subtitle)
        return page.body

    def _card(self, parent, title: str = "") -> GlassPanel:
        card = GlassPanel(parent, "PANEL")
        card.pack(fill="x", pady=(0, self.th.px(T.SPACE_LG)))
        if title:
            Heading(card, title).pack(fill="x", padx=self.th.px(T.SPACE_XL - 4), pady=(self.th.px(T.SPACE_LG), self.th.px(T.SPACE_SM)))
        return card

    # ------------------------------------------------------------------ 挂载页
    def _build_mounts(self) -> None:
        px = self.th.px
        page = Clear(self.content)
        page.grid(row=0, column=0, sticky="nsew")
        self.pages["mounts"] = page
        heading = self._heading(page, "挂载", "连接远端存储，管理本机挂载。")
        self.mount_buttons = {}
        add = Button(heading, "添加挂载", self._add_mount, variant="primary", glyph=T.Icons.ADD)
        add.pack(side="right", padx=(px(T.SPACE_MD), 0))
        self.mount_buttons["add"] = add
        self.mount_metrics = Clear(page)
        self.mount_metrics.pack(fill="x", pady=(0, px(T.SPACE_LG)))
        for column, (key, label) in enumerate((("mounts", "挂载配置"), ("ready", "当前就绪"), ("remotes", "可用 Remote"))):
            self.mount_metrics.columnconfigure(column, weight=1, uniform="metric")
            cell = GlassPanel(self.mount_metrics, "PANEL")
            cell.grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else px(T.SPACE_SM), 0))
            Caption(cell, label, token="TEXT_TERTIARY", size=T.FONT_LABEL).pack(fill="x", padx=px(T.SPACE_LG), pady=(px(T.SPACE_MD), 0))
            value = Heading(cell, size=T.FONT_PAGE, textvariable=self.metric_vars[key],
                            token="SUCCESS" if key == "ready" else "TEXT")
            value.pack(fill="x", padx=px(T.SPACE_LG), pady=(px(2), px(T.SPACE_MD)))
        toolbar = Clear(page)
        toolbar.pack(fill="x", pady=(0, px(T.SPACE_SM)))
        self.search_field = TextField(toolbar, self.search_var, placeholder="搜索名称、remote 或挂载位置（Ctrl+F）")
        self.search_field.pack(side="left", fill="x", expand=True)
        self.search_entry = self.search_field.entry
        self.state_filter = DropDown(toolbar, self.state_var, [(ALL_STATES, ALL_STATES)] + [(v, v) for v in STATE_NAMES.values()], width=116)
        self.state_filter.pack(side="left", padx=(px(T.SPACE_SM), 0))
        self.clear_filter_button = IconButton(toolbar, T.Icons.CLEAR, self._clear_filter, tooltip="清除搜索与筛选")
        self.clear_filter_button.pack(side="left", padx=(px(T.SPACE_XS), 0))
        commands = Clear(page)
        commands.pack(fill="x", pady=(0, px(T.SPACE_XS)))
        for key, label, glyph, command in (
                ("start_all", "启动全部", T.Icons.PLAY, lambda: self._invoke("start_all_mounts")),
                ("stop_all", "停止全部", T.Icons.STOP, lambda: self._invoke("stop_all_mounts")),
                ("refresh", "刷新环境", T.Icons.REFRESH, lambda: self._invoke("refresh_environment"))):
            button = Button(commands, label, command, variant="ghost", glyph=glyph, size="sm")
            button.pack(side="left", padx=(0, px(T.SPACE_XS)))
            self.mount_buttons[key] = button
        self.detail_toggle = Button(commands, "隐藏详情", self._toggle_detail, variant="ghost", glyph=T.Icons.PANEL_RIGHT, size="sm")
        self.detail_toggle.pack(side="right")
        Caption(page, textvariable=self.summary_var, token="TEXT_TERTIARY").pack(anchor="w", pady=(0, px(T.SPACE_SM)))
        self.mount_actions = Clear(page)
        self.mount_actions.pack(side="bottom", fill="x", pady=(px(T.SPACE_MD), 0))
        for column, (key, label, glyph, variant, command) in enumerate((
                ("start", "启动", T.Icons.PLAY, "primary", lambda: self._mount_action("start_mount")),
                ("stop", "停止", T.Icons.STOP, "secondary", lambda: self._mount_action("stop_mount")),
                ("open", "打开位置", T.Icons.FOLDER, "secondary", lambda: self._mount_action("open_mount")),
                ("log", "日志", T.Icons.DOC, "secondary", self._open_mount_log),
                ("edit", "编辑", T.Icons.EDIT, "secondary", self._edit_mount),
                ("delete", "删除配置", T.Icons.DELETE, "danger", self._delete_mount))):
            button = Button(self.mount_actions, label, command, variant=variant, glyph=glyph, width=10)
            button.grid(row=0, column=column, sticky="ew", padx=(0, px(T.SPACE_XS)))
            self.mount_actions.columnconfigure(column, weight=1, uniform="action")
            self.mount_buttons[key] = button
        self.empty_label = Caption(page, textvariable=self.empty_var, wrap=px(650))
        self.empty_label.pack(side="bottom", anchor="w", pady=(px(T.SPACE_SM), 0))
        self.mount_workspace = Clear(page)
        self.mount_workspace.pack(fill="both", expand=True)
        self.mount_workspace.rowconfigure(0, weight=1)
        self.mount_workspace.columnconfigure(0, weight=1)
        self.mount_card = GlassPanel(self.mount_workspace, "PANEL")
        self.mount_card.grid(row=0, column=0, sticky="nsew")
        self.mount_list = MountList(self.mount_card, on_select=self._selected, on_activate=lambda _id: self._edit_mount())
        self.mount_list.pack(fill="both", expand=True, padx=px(T.SPACE_XS), pady=px(T.SPACE_XS))
        self.mount_list.canvas.bind("<Delete>", lambda e: self._delete_mount())
        self.mount_detail = GlassPanel(self.mount_workspace, "PANEL", width=px(280))
        self.mount_detail.pack_propagate(False)
        Caption(self.mount_detail, "挂载详情", token="TEXT_TERTIARY", size=T.FONT_LABEL).pack(fill="x", padx=px(T.SPACE_LG), pady=(px(T.SPACE_MD), px(T.SPACE_XS)))
        self.selection_label = Label(self.mount_detail, textvariable=self.selection_var, weight="semibold", size=14, wrap=px(240))
        self.selection_label.pack(fill="x", padx=px(T.SPACE_LG), pady=(0, px(T.SPACE_SM)))
        self.detail_text = TextArea(self.mount_detail, height=6)
        self.detail_text.pack(fill="both", expand=True, padx=px(T.SPACE_LG), pady=(0, px(T.SPACE_MD)))
        self.search_var.trace_add("write", lambda *_: self._refresh_mounts())
        self.state_var.trace_add("write", lambda *_: self._refresh_mounts())
        self.mount_workspace.bind("<Configure>", lambda e: self._layout_mounts(e.width, e.height))

    def _toggle_detail(self) -> None:
        self._detail_visible = not self._detail_visible
        self._wide_mounts = None
        self._layout_mounts(self.mount_workspace.winfo_width(), self.mount_workspace.winfo_height())

    def _layout_mounts(self, width: int, height: int) -> None:
        """宽窗详情在右栏；窄窗放列表下方；窄且矮时自动收起详情，列表优先。"""
        px = self.th.px
        wide = width >= px(820)
        roomy = height >= px(300)
        layout = (wide, self._detail_visible, roomy)
        if layout == self._wide_mounts:
            return
        self._wide_mounts = layout
        self.mount_detail.grid_remove()
        if self._detail_visible and (wide or roomy):
            if wide:
                self.mount_detail.configure(width=px(280), height=1)
                self.mount_detail.grid(row=0, column=1, sticky="nsew", padx=(px(T.SPACE_MD), 0), pady=0)
                self.detail_text.text.configure(height=6)
                self.selection_label.configure(wraplength=px(240))
            else:
                self.mount_detail.configure(width=1, height=px(124))
                self.mount_detail.grid(row=1, column=0, sticky="ew", padx=0, pady=(px(T.SPACE_SM), 0))
                self.detail_text.text.configure(height=2)
                self.selection_label.configure(wraplength=max(180, width - px(T.SPACE_XXL)))
        self.detail_toggle.set_text("隐藏详情" if self._detail_visible else "显示详情")
        self.detail_toggle.set_enabled(wide or roomy)
        self._schedule_rerender()

    # ------------------------------------------------------------------ 表单
    def _add_field(self, page: str, card, key: str, label: str, *, kind="entry",
                   values=(), hint="", browse=None) -> tk.Widget:
        px = self.th.px
        pad = px(T.SPACE_XL - 4)
        variable = tk.BooleanVar(self.root) if kind == "check" else tk.StringVar(self.root)
        self.vars[page][key] = variable
        if kind == "check":
            field = ToggleRow(card, label, variable, hint=hint)
            field.pack(fill="x", padx=pad, pady=(px(T.SPACE_SM), px(T.SPACE_SM)))
        else:
            row = Clear(card)
            row.pack(fill="x", padx=pad, pady=(px(T.SPACE_SM), px(T.SPACE_SM)))
            Label(row, label).pack(anchor="w", pady=(0, px(6)))
            controls = Clear(row)
            controls.pack(fill="x")
            if browse:
                Button(controls, "浏览…", browse, variant="secondary", glyph=T.Icons.FOLDER).pack(side="right", padx=(px(T.SPACE_SM), 0))
            if kind == "combo":
                field = DropDown(controls, variable, values, width=220)
                field.pack(side="left")
            else:
                field = TextField(controls, variable, show="•" if kind == "password" else "",
                                  width=160 if kind == "number" else None)
                field.pack(side="left", fill="x" if kind != "number" else "none", expand=kind != "number")
            if hint:
                note = Caption(row, hint, wrap=px(560))
                note.pack(fill="x", pady=(px(6), 0))
                row.bind("<Configure>", lambda event, label=note: label.configure(wraplength=max(180, event.width - 8)))
            if isinstance(field, TextField):
                field.entry.bind("<FocusIn>", lambda e, p=page, f=field: self._ensure_visible(p, f), add="+")
        self.fields[page][key] = field
        variable.trace_add("write", lambda *_args, p=page, k=key: self._form_changed(p, k))
        return field

    def _form_group(self, parent, title: str) -> GlassPanel:
        return self._card(parent, title)

    def _build_service(self) -> None:
        px = self.th.px
        pad = px(T.SPACE_XL - 4)
        body = self._new_scroll_page("service", "rclone GUI", "本机网页服务。就绪后可在浏览器中打开。")
        runtime = self._form_group(body, "服务状态")
        self.gui_status_label = Heading(runtime, textvariable=self.gui_status_var, size=T.FONT_MODULE)
        self.gui_status_label.pack(fill="x", padx=pad, pady=(0, px(T.SPACE_XS)))
        self.gui_message_label = Caption(runtime, textvariable=self.gui_message_var, wrap=px(650))
        self.gui_message_label.pack(fill="x", padx=pad, pady=(0, px(T.SPACE_MD)))
        actions = Clear(runtime)
        actions.pack(fill="x", padx=pad, pady=(0, px(T.SPACE_MD)))
        self.gui_buttons = {}
        for key, label, glyph, action in (("start", "启动 GUI", T.Icons.PLAY, "start_gui"), ("stop", "停止 GUI", T.Icons.STOP, "stop_gui"),
                                          ("open", "打开网页", T.Icons.GLOBE, "open_gui"), ("log", "查看日志", T.Icons.DOC, None)):
            button = Button(actions, label, (lambda a=action: self._invoke(a)) if action else self._open_gui_log,
                            variant="primary" if key == "start" else "secondary", glyph=glyph)
            button.pack(side="left", padx=(0, px(T.SPACE_SM)))
            self.gui_buttons[key] = button
        Caption(runtime, textvariable=self.gui_saved_var, wrap=px(650)).pack(fill="x", padx=pad, pady=(0, px(T.SPACE_LG)))
        auth = self._form_group(body, "启动与认证")
        self._add_field("service", auth, "gui_auto_start", "随应用启动 GUI", kind="check", hint="每次启动应用时生效，与开机自动挂载无关。")
        self._add_field("service", auth, "gui_no_auth", "免认证（仅限可信本机）", kind="check", hint="默认需要用户名和密码。勾选后本机任何进程都能调用 rclone RC API，等同于本机命令执行权限。")
        self._add_field("service", auth, "gui_user", "用户名")
        self._add_field("service", auth, "gui_pass", "密码", kind="password", hint="密码不会放入网页 URL。保存设置不会自动重启服务。")
        Clear(auth, height=px(T.SPACE_SM)).pack(fill="x")
        Caption(body, "启停即时执行；放弃草稿不会撤销运行操作。", wrap=px(650)).pack(anchor="w", pady=(0, px(T.SPACE_LG)))

    def _build_settings(self) -> None:
        px = self.th.px
        pad = px(T.SPACE_XL - 4)
        body = self._new_scroll_page("settings", "设置", "管理运行环境、启动策略与外观。本页独立保存。")
        runtime = self._form_group(body, "运行环境")
        self._add_field("settings", runtime, "rclone_path", "rclone 可执行文件", browse=self._browse_exe, hint="留空自动发现。指定路径无效时不会回退到其他程序。")
        self._add_field("settings", runtime, "rclone_conf", "rclone 配置文件", browse=self._browse_conf, hint="留空使用 RCLONE_CONFIG 或标准位置；浏览文件不会立即保存。")
        environment = Clear(runtime)
        environment.pack(fill="x", padx=pad, pady=(px(T.SPACE_XS), 0))
        self.detect_button = Button(environment, "重新检测", lambda: self._invoke("refresh_environment"), variant="secondary", glyph=T.Icons.REFRESH)
        self.detect_button.pack(side="left")
        self.auto_detect_button = Button(environment, "自动发现", lambda: self.vars["settings"]["rclone_path"].set(""), variant="secondary")
        self.auto_detect_button.pack(side="left", padx=px(T.SPACE_SM))
        self.open_conf_button = Button(environment, "打开 rclone 配置", lambda: self._invoke("open_rclone_config"), variant="secondary", glyph=T.Icons.DOC)
        self.open_conf_button.pack(side="left")
        self.environment_label = Caption(runtime, textvariable=self.environment_var, wrap=px(650))
        self.environment_label.pack(fill="x", padx=pad, pady=(px(T.SPACE_MD), px(T.SPACE_LG)))
        startup = self._form_group(body, "启动行为")
        self._add_field("settings", startup, "startup_enabled", "Windows 登录时启动托盘", kind="check", hint="保存后更新当前用户的开机启动项。")
        self._add_field("settings", startup, "auto_mount_on_boot", "开机启动后自动挂载", kind="check", hint="仅在系统登录启动时生效，还需挂载项启用自动挂载。")
        self._add_field("settings", startup, "mount_delay_seconds", "挂载延迟（秒，0–86400）", kind="number", hint="等待网络就绪；修改不会立即启动挂载。")
        Clear(startup, height=px(T.SPACE_SM)).pack(fill="x")
        appearance = self._form_group(body, "外观与动效")
        self._add_field("settings", appearance, "theme", "主题", kind="combo", values=THEME_OPTIONS, hint="深色为规范默认；浅色与跟随系统为派生配色。预览在放弃后恢复。")
        self._add_field("settings", appearance, "reduce_motion", "减少动态效果", kind="check", hint="关闭装饰过渡，不影响状态更新。面板保持实色合成，文字始终清晰。")
        Clear(appearance, height=px(T.SPACE_SM)).pack(fill="x")

    def _build_about(self) -> None:
        px = self.th.px
        pad = px(T.SPACE_XL - 4)
        body = self._new_scroll_page("about", "关于与诊断", "rclone_tray · Windows 单用户托盘工具。诊断不显示认证密码。")
        links = Clear(body)
        links.pack(fill="x", pady=(0, px(T.SPACE_SM)))
        self.about_buttons = {}
        for key, label, glyph, command in (
                ("config", "应用配置", T.Icons.DOC, lambda: self._invoke("open_path", str(config_path()))),
                ("rclone_conf", "rclone 配置", T.Icons.DOC, lambda: self._invoke("open_rclone_config")),
                ("logs", "日志目录", T.Icons.FOLDER, lambda: self._invoke("open_path", str(logs_dir()))),
                ("refresh", "刷新诊断", T.Icons.REFRESH, lambda: self._invoke("refresh_environment"))):
            button = Button(links, label, command, variant="secondary", glyph=glyph)
            button.pack(side="left", padx=(0, px(T.SPACE_SM)))
            self.about_buttons[key] = button
        help_links = Clear(body)
        help_links.pack(fill="x", pady=(0, px(T.SPACE_LG)))
        for label, url in (("rclone 官方文档", "https://rclone.org/docs/"),
                           ("WinFsp 安装与文档", "https://winfsp.dev/")):
            Button(help_links, label, lambda path=url: self._invoke("open_path", path), variant="ghost", glyph=T.Icons.LINK, size="sm").pack(side="left", padx=(0, px(T.SPACE_XS)))
        self.about_buttons["config_dir"] = Button(help_links, "rclone 配置目录", self._open_config_dir, variant="ghost", glyph=T.Icons.FOLDER, size="sm")
        self.about_buttons["config_dir"].pack(side="left", padx=(0, px(T.SPACE_XS)))
        diagnostics = self._card(body, "诊断信息")
        self.about_text = TextArea(diagnostics, height=15)
        self.about_text.pack(fill="both", expand=True, padx=pad, pady=(0, px(T.SPACE_SM)))
        self.copy_button = Button(diagnostics, "复制诊断信息", self.about_text.copy_selection, variant="ghost", glyph=T.Icons.COPY, size="sm")
        self.copy_button.pack(anchor="w", padx=px(T.SPACE_MD), pady=(0, px(T.SPACE_MD)))
        exit_row = Clear(body)
        exit_row.pack(fill="x", pady=(0, px(T.SPACE_LG)))
        self.exit_button = Button(exit_row, "退出应用并停止本应用任务", self.request_exit, variant="danger", glyph=T.Icons.POWER)
        self.exit_button.pack(side="left")

    # ------------------------------------------------------------------ 尺寸与主题
    def _resize(self, event) -> None:
        if event.widget is not self.root or not hasattr(self, "gui_message_label"):
            return
        compact = event.width < self.th.px(940)
        if compact != self._compact_nav:
            self._compact_nav = compact
            self._layout_nav()
        if event.height < self.th.px(620):
            self.mount_metrics.pack_forget()
        elif not self.mount_metrics.winfo_manager():
            self.mount_metrics.pack(fill="x", pady=(0, self.th.px(T.SPACE_LG)), before=self.search_field.master)
        self._apply_wraps(event.width)

    def _apply_wraps(self, root_width: int) -> None:
        """长文本标签的折行宽度跟随内容区；未布局完成的父容器按内容区宽度处理。"""
        width = max(220, root_width - self.sidebar.winfo_width() - self.th.px(48))
        for label in (self.config_label, self.message_label, self.empty_label,
                      self.environment_label, self.gui_message_label, self.footer_label):
            label.configure(wraplength=width)
        self._wrap_labels(self.content, width)

    def _wrap_labels(self, widget, width: int) -> None:
        for child in widget.winfo_children():
            if isinstance(child, tk.Label) and int(child.cget("wraplength") or 0):
                master_w = child.master.winfo_width()
                limit = min(width, master_w - self.th.px(T.SPACE_XL)) if master_w > 1 else width
                child.configure(wraplength=max(180, limit))
            self._wrap_labels(child, width)

    def _apply_theme(self) -> None:
        self.root.configure(bg=T.BASE)
        self.backdrop.apply_theme()
        self._refresh_gui_status_token()
        if self.editor and not self.editor.closed:
            self.editor.apply_theme()
        apply_native_chrome(self.root, caption_color=T.BASE)

    def _refresh_gui_status_token(self) -> None:
        gui = self.snapshot["gui"]
        self.gui_status_label.set_token(STATE_TOKENS.get(gui.state, "TEXT"))

    # ------------------------------------------------------------------ 草稿
    def _load_form(self, page: str, snapshot: dict) -> None:
        self._loading = True
        cfg = snapshot["config"]
        try:
            for key, variable in self.vars[page].items():
                variable.set(getattr(cfg, key))
            self.baselines[page] = self._form_values(page)
            self.revisions[page] = snapshot["revision"]
            self.form_errors[page] = ""
        finally:
            self._loading = False
        if page == "service":
            self._sync_auth()
        if page == "settings":
            T.set_mode(cfg.theme)
            set_reduce_motion(cfg.reduce_motion)

    def _form_values(self, page: str) -> dict:
        return {key: variable.get() for key, variable in self.vars[page].items()}

    def _dirty(self, page: str) -> bool:
        return page in self.baselines and self._form_values(page) != self.baselines[page]

    def _form_changed(self, page: str, key: str) -> None:
        if self._loading:
            return
        if page == "service" and key == "gui_no_auth":
            self._sync_auth()
        if page == "settings" and key in ("theme", "reduce_motion"):
            if key == "theme":
                T.set_mode(self.vars[page][key].get())
                self._apply_theme()
            else:
                set_reduce_motion(self.vars[page][key].get())
        if page == "settings" and key == "mount_delay_seconds":
            self.fields[page][key].set_error(False)
        self._refresh_footer()

    def _sync_auth(self) -> None:
        disabled = self.vars["service"]["gui_no_auth"].get() or self._saving_page == "service"
        for key in ("gui_user", "gui_pass"):
            self.fields["service"][key].set_enabled(not disabled)

    def _ensure_visible(self, page: str, widget) -> None:
        if self.page == page:
            self.pages[page].ensure_visible(widget)

    # ------------------------------------------------------------------ 导航
    def show(self, page: str = "mounts", mount_id: str = "") -> bool:
        add_mount = page == "add_mount"
        page = "mounts" if add_mount else ("settings" if page == "general" else page)
        if page not in self.pages:
            page = "mounts"
        self.root.deiconify()
        flash_taskbar(self.root)
        self.root.after(250, lambda: self._apply_wraps(self.root.winfo_width()))
        if self.editor and not self.editor.closed:
            if not self.editor.request_close():
                self.editor.win.lift()
                return False
        if page != self.page and not self._guard_forms((self.page,)):
            return False
        self.refresh(self.controller.snapshot())
        self._route(page)
        if mount_id:
            self._clear_filter()
            if self.mount_list.exists(mount_id):
                self.selected_id = mount_id
                self.mount_list.selection_set(mount_id)
                self.mount_list.see(mount_id)
                self._refresh_detail()
            else:
                self.message_var.set("该挂载已不存在，请选择当前列表中的挂载。")
        if add_mount:
            self._add_mount()
        return True

    def _route(self, page: str) -> None:
        self.page = page
        for key, frame in self.pages.items():
            if key == page:
                frame.grid()
            else:
                frame.grid_remove()
            self.nav_buttons[key].set_selected(key == page)
        self.root.title(f"rclone_tray · {dict(PAGES)[page]}")
        self._refresh_footer()
        # 首次布局在 idle 期分多步完成；稍后再整体重绘一次，清除按旧位图裁剪的面板
        self.root.after(200, lambda: rerender_within(self.pages[page]))

    def _guard_forms(self, pages) -> bool:
        if self._saving_page is not None:
            self.message_var.set("正在保存设置，请等待结果后再离开。")
            return False
        dirty = [page for page in pages if page in self.vars and self._dirty(page)]
        if not dirty:
            return True
        labels = "、".join(dict(PAGES)[page] for page in dirty)
        if not messagebox.askyesno("放弃未保存输入？", f"{labels}有未保存输入。确认放弃并继续？\n"
                                  "已经执行的启停操作不会被撤销。", parent=self.root, default="no"):
            return False
        for page in dirty:
            self._load_form(page, self.controller.snapshot())
        self._apply_theme()
        return True

    def hide(self) -> bool:
        if self.editor and not self.editor.closed and not self.editor.request_close():
            return False
        if not self._guard_forms(("settings", "service")):
            return False
        self.root.withdraw()
        return True

    def request_exit(self) -> bool:
        if self.editor and not self.editor.closed and not self.editor.request_close():
            return False
        if not self._guard_forms(("settings", "service")):
            return False
        snapshot = self.controller.snapshot()
        if snapshot.get("closing"):
            self.message_var.set("正在停止本应用持有的任务，请等待退出结果。")
            return False
        statuses = [snapshot["gui"], *snapshot["mounts"].values()]
        active = sum(1 for status in statuses if status.pid is not None or status.state in ("starting", "ready", "stopping"))
        if self.root.state() == "withdrawn":
            self.root.deiconify()
            flash_taskbar(self.root)
        prompt = (f"将停止本应用持有的 {active} 个运行任务并取消延迟启动，然后退出。\n停止失败时应用会保留以便重试。"
                  if active else "将取消延迟启动并退出应用。")
        if not messagebox.askyesno("退出 rclone_tray？", prompt, parent=self.root, default="no"):
            return False
        self.message_var.set("正在确认本应用任务已停止…")
        self._invoke("request_exit")
        self.refresh(self.controller.snapshot())
        return True

    # ------------------------------------------------------------------ 刷新
    def refresh(self, snapshot: dict) -> None:
        self.snapshot = snapshot
        old_theme = (T.mode(), T.resolved())
        for page in ("settings", "service"):
            if not self._dirty(page) and self._saving_page != page:
                values = {key: (getattr(snapshot["config"], key) if isinstance(variable, tk.BooleanVar)
                                else str(getattr(snapshot["config"], key)))
                          for key, variable in self.vars[page].items()}
                if values != self.baselines[page]:
                    self._load_form(page, snapshot)
                else:
                    self.revisions[page] = snapshot["revision"]
        mode = self.vars["settings"]["theme"].get()
        T.set_mode(mode)
        set_reduce_motion(self.vars["settings"]["reduce_motion"].get())
        if old_theme != (T.mode(), T.resolved()):
            self._apply_theme()
        error = snapshot.get("config_error", "")
        self.config_error_var.set(f"配置读取失败；所有保存已禁用。{error}" if error else "")
        busy = snapshot.get("busy", set())
        if error:
            self.config_bar.pack(fill="x", padx=self.th.px(T.SPACE_XL), pady=(0, self.th.px(T.SPACE_SM)), before=self.content)
        else:
            self.config_bar.pack_forget()
        self.reload_button.set_enabled(not (snapshot.get("closing") or "settings" in busy))
        self._refresh_mounts()
        gui = snapshot["gui"]
        self.gui_status_var.set(f"服务：{STATE_NAMES.get(gui.state, gui.state)}" + (f" · PID {gui.pid}" if gui.pid else ""))
        self.gui_message_var.set(gui.message or "仅管理本应用启动的服务实例。")
        cfg = snapshot["config"]
        self.gui_saved_var.set("已保存服务配置：" + ("免认证" if cfg.gui_no_auth else "需要认证（凭据不显示）") +
                               ("；随应用启动。" if cfg.gui_auto_start else "；手动启动。") +
                               (" 当前草稿尚未生效。" if self._dirty("service") else "") +
                               " 修改连接设置需先停止；保存不自动启动或重启。")
        gui_busy = "gui" in busy or snapshot.get("closing")
        for key, enabled in (("start", gui.state in ("stopped", "failed")),
                             ("stop", gui.state != "stopped" or gui.pid is not None),
                             ("open", gui.state == "ready"), ("log", bool(gui.log_path))):
            self.gui_buttons[key].set_enabled(enabled and not gui_busy)
        self._refresh_gui_status_token()
        self.gui_buttons["start"].set_text("启动中…" if gui.state == "starting" else "启动 GUI")
        self.gui_buttons["stop"].set_text("停止中…" if gui.state == "stopping" else "停止 GUI")
        environment = (f"rclone：{snapshot.get('version') or '未检测到有效版本'}\n"
                       + (f"程序：{snapshot['exe']}\n" if snapshot.get("exe") else "")
                       + f"WinFsp：{'已安装' if snapshot.get('winfsp') else '未检测到（挂载依赖）'} · "
                       f"Remote：{len(snapshot.get('remotes', []))} 个")
        if snapshot.get("environment_error"):
            environment += "\n" + snapshot["environment_error"]
        self.environment_var.set(environment)
        self.detect_button.set_enabled(not ("environment" in busy or snapshot.get("closing")))
        status = "正在退出" if snapshot.get("closing") else ("操作进行中" if busy else "空闲")
        self.status_var.set(f"{snapshot.get('version') or 'rclone 未检测'}  ·  GUI {STATE_NAMES.get(gui.state, gui.state)}  ·  {status}")
        self._refresh_diagnostics(environment)
        self._refresh_footer()
        if self.editor and not self.editor.closed:
            self.editor.refresh(snapshot)

    def _refresh_mounts(self) -> None:
        if not hasattr(self, "mount_list"):
            return
        snapshot = self.snapshot
        mounts = snapshot["config"].mounts
        ids = {mount.id for mount in mounts}
        query = self.search_var.get().casefold()
        desired_state = self.state_var.get()
        rows = []
        for mount in mounts:
            status = snapshot["mounts"].get(mount.id)
            raw_state = status.state if status else "stopped"
            state = STATE_NAMES.get(raw_state, raw_state)
            matches = (not query or query in " ".join((mount.name, mount.remote, mount.mount_point)).casefold())
            matches = matches and (desired_state == ALL_STATES or state == desired_state)
            if matches:
                rows.append({"id": mount.id, "name": mount.name, "remote": mount.remote,
                             "point": mount.mount_point, "state": state,
                             "tone": STATE_TONES.get(raw_state, "idle")})
        visible = [row["id"] for row in rows]
        self.mount_list.set_rows(rows, known_ids=ids)
        self._known_mount_ids = ids
        if self.selected_id not in ids:
            self.selected_id = ""
        if not self.selected_id and visible:
            self.selected_id = visible[0]
        if self.selected_id in visible and self.mount_list.selection() != (self.selected_id,):
            self.mount_list.selection_set(self.selected_id)
        ready = sum(1 for status in snapshot["mounts"].values() if status.state == "ready")
        for key, value in (("mounts", len(mounts)), ("ready", ready), ("remotes", len(snapshot.get("remotes", [])))):
            self.metric_vars[key].set(str(value))
        self.summary_var.set(f"{len(mounts)} 个挂载 · {ready} 个就绪 · 筛选显示 {len(visible)} 个")
        if not mounts:
            text = "还没有挂载。添加挂载前请先配置真实 remote 和 WinFsp。"
        elif not visible:
            text = "没有匹配挂载。可清除搜索与状态筛选；原配置未被删除。"
        elif not snapshot.get("remotes"):
            text = "未检测到 remote；可在设置中打开 rclone 配置。已有挂载配置仍然保留。"
        else:
            text = ""
        self.empty_var.set(text)
        busy = snapshot.get("busy", set())
        closing = snapshot.get("closing", False)
        for key, enabled in (("add", not snapshot.get("config_error") and "settings" not in busy),
                             ("start_all", bool(mounts) and "mounts" not in busy),
                             ("stop_all", bool(mounts) and "mounts" not in busy),
                             ("refresh", "environment" not in busy)):
            self.mount_buttons[key].set_enabled(enabled and not closing)
        self._refresh_detail()

    def _selected(self, iid: str) -> None:
        if iid != self.selected_id:
            self.selected_id = iid
            self._refresh_detail()

    def _selected_mount(self):
        return next((mount for mount in self.snapshot["config"].mounts if mount.id == self.selected_id), None)

    def _refresh_detail(self) -> None:
        mount = self._selected_mount()
        snapshot = self.snapshot
        status = snapshot["mounts"].get(self.selected_id)
        if mount:
            filtered = self.selected_id not in self.mount_list.get_children()
            self.selection_var.set(mount.name + ("（当前被筛选隐藏，仍保留选择）" if filtered else ""))
            state = STATE_NAMES.get(status.state, status.state) if status else "已停止"
            options = ", ".join((f"VFS {mount.vfs_cache_mode}", "网络模式" if mount.network_mode else "本地模式",
                                  "只读" if mount.read_only else "可读写", "开机自动" if mount.auto_mount else "手动启动"))
            detail = f"Remote\n{mount.remote}\n\n挂载位置\n{mount.mount_point}\n\n{state} · {options}"
            if mount.volname:
                detail += f" · 卷标 {mount.volname}"
            if status and status.message and status.message != state:
                detail += "\n" + status.message
        else:
            self.selection_var.set("选择挂载以查看详情")
            detail = "启动、停止、打开位置、日志、编辑和删除均针对所选挂载。"
        if detail != self._detail_text:
            self._detail_text = detail
            self.detail_text.set_text(detail)
        busy = snapshot.get("busy", set())
        locked = snapshot.get("closing") or self.selected_id in busy or "mounts" in busy
        state = status.state if status else "stopped"
        for key, enabled in (("start", bool(mount) and state in ("stopped", "failed") and not (status and status.pid)),
                             ("stop", bool(mount) and (state != "stopped" or bool(status and status.pid))),
                             ("open", bool(mount) and state == "ready"),
                             ("log", bool(mount) and bool(status and status.log_path)),
                             ("edit", bool(mount) and not snapshot.get("config_error") and "settings" not in busy),
                             ("delete", bool(mount) and not snapshot.get("config_error") and "settings" not in busy)):
            self.mount_buttons[key].set_enabled(enabled and not locked)
        self.mount_buttons["start"].set_text("启动中…" if state == "starting" else "启动")
        self.mount_buttons["stop"].set_text("停止中…" if state == "stopping" else "停止")

    def _refresh_footer(self) -> None:
        if self.page not in self.vars:
            self.form_footer.pack_forget()
            return
        if not self.form_footer.winfo_manager():
            self.form_footer.pack(side="bottom", fill="x", padx=self.th.px(T.SPACE_XL), pady=self.th.px(T.SPACE_MD), before=self.content)
        dirty = self._dirty(self.page)
        busy = "settings" in self.snapshot.get("busy", set()) or self._saving_page is not None
        blocked = bool(self.snapshot.get("config_error")) or self.snapshot.get("closing", False)
        note = self.form_errors[self.page]
        if self._saving_page == self.page:
            note = "正在保存本页设置…"
        elif not note:
            note = "有未保存输入；仅保存本页。" if dirty else "本页与已保存设置一致。"
        if dirty and self.revisions[self.page] != self.snapshot["revision"]:
            note += " 配置版本已变化。"
        self.footer_var.set(note)
        self.footer_label.set_token("CRITICAL" if self.form_errors[self.page] else "TEXT_SECONDARY")
        self.save_button.set_enabled(dirty and not busy and not blocked)
        self.discard_button.set_enabled(dirty and not busy)
        conflict = dirty and not busy and not blocked and self.revisions[self.page] != self.snapshot["revision"]
        if conflict:
            self.rebase_button.pack(side="right", padx=(0, self.th.px(T.SPACE_SM)))
        else:
            self.rebase_button.pack_forget()
        self.rebase_button.set_enabled(conflict)
        self.save_button.set_text("正在保存…" if self._saving_page == self.page else "保存更改")

    # ------------------------------------------------------------------ 保存
    def _save_form(self) -> None:
        page = self.page
        snapshot = self.controller.snapshot()
        if page not in self.vars or self._saving_page or "settings" in snapshot.get("busy", set()):
            return
        if snapshot.get("config_error") or snapshot.get("closing"):
            self.form_errors[page] = "配置读取失败或应用正在退出；未保存，输入已保留。"
            self._refresh_footer()
            return
        draft = copy.deepcopy(snapshot["config"])
        values = self._form_values(page)
        if page == "settings":
            raw = values["mount_delay_seconds"]
            try:
                if not raw.strip() or not raw.strip().isdigit():
                    raise ValueError
                delay = int(raw)
                if not 0 <= delay <= 86400:
                    raise ValueError
            except ValueError:
                self.form_errors[page] = "挂载延迟必须是 0–86400 的整数；输入已保留。"
                self.fields[page]["mount_delay_seconds"].set_error(True)
                self.fields[page]["mount_delay_seconds"].focus_set()
                self._refresh_footer()
                return
            values["mount_delay_seconds"] = delay
        elif not values["gui_no_auth"] and (not values["gui_user"] or not values["gui_pass"]):
            self.form_errors[page] = "启用认证时用户名与密码均不能为空；输入已保留。"
            self._refresh_footer()
            return
        for key, value in values.items():
            setattr(draft, key, value)
        self._saving_page = page
        self.form_errors[page] = ""
        self._lock_form(page, True)
        try:
            accepted = self.controller.save_settings(draft, self.revisions[page])
            if accepted is False:
                self._saving_page = None
                self._lock_form(page, False)
                self.form_errors[page] = "设置正在执行其他操作，未提交保存；请稍后重试。"
        except Exception as exc:
            self._saving_page = None
            self._lock_form(page, False)
            self.form_errors[page] = str(exc)
        self.refresh(self.controller.snapshot())

    def _lock_form(self, page: str, lock: bool) -> None:
        for field in self.fields[page].values():
            field.set_enabled(not lock)
        if page == "service":
            self._sync_auth()
        elif page == "settings":
            self.auto_detect_button.set_enabled(not lock)

    def _discard_form(self) -> None:
        if self.page in self.vars and self._guard_forms((self.page,)):
            self._load_form(self.page, self.controller.snapshot())
            self._apply_theme()
            self._refresh_footer()

    def _rebase_form(self) -> None:
        page = self.page
        if page not in self.vars or self._saving_page:
            return
        snapshot = self.controller.snapshot()
        if snapshot.get("config_error"):
            return
        if messagebox.askyesno("更新保存版本", "保留本页输入并使用最新配置版本？\n"
                              "下次保存仅应用本页字段；其他设置与挂载采用最新值。", parent=self.root, default="no"):
            self.revisions[page] = snapshot["revision"]
            self.form_errors[page] = "已更新版本；输入保留，请再次保存。"
            self.refresh(snapshot)

    def _reload_config(self) -> None:
        if self._saving_page or (self.editor and self.editor.pending):
            self.message_var.set("正在保存，请等待结果后重新载入配置。")
            return
        if any(self._dirty(page) for page in ("settings", "service")) or self.editor:
            if not messagebox.askyesno("重新载入配置", "重新读取配置并保留所有当前输入？\n"
                                      "未保存草稿保持原版本；重新载入后可显式更新保存版本。",
                                      parent=self.editor.win if self.editor else self.root, default="no"):
                return
        self._invoke("reload_config")

    # ------------------------------------------------------------------ 挂载操作
    def _add_mount(self) -> None:
        self._open_editor(None)

    def _edit_mount(self) -> None:
        mount = self._selected_mount()
        if mount:
            self._open_editor(mount)

    def _open_editor(self, mount) -> None:
        if self.editor and not self.editor.closed:
            self.editor.win.lift()
            return
        snapshot = self.controller.snapshot()
        if snapshot.get("config_error") or snapshot.get("closing") or "settings" in snapshot.get("busy", set()):
            self.message_var.set("当前不能编辑：请先处理配置读取错误或等待保存完成。")
            return
        self.editor = MountSheet(self.root, self.controller, mount, snapshot["revision"], self._editor_closed)

    def _editor_closed(self, saved_id: str | None) -> None:
        self.editor = None
        self.refresh(self.controller.snapshot())
        if saved_id:
            self.selected_id = saved_id
            self._clear_filter()
            self._refresh_mounts()
            if self.mount_list.exists(saved_id):
                self.mount_list.selection_set(saved_id)
                self.mount_list.see(saved_id)
            self.mount_list.focus_set()

    def _delete_mount(self) -> None:
        mount = self._selected_mount()
        if not mount:
            return
        status = self.snapshot["mounts"].get(mount.id)
        if status and (status.pid is not None or status.state in ("starting", "ready", "stopping")):
            self.message_var.set(f"请先停止“{mount.name}”并确认已停止，再删除配置。")
            return
        if messagebox.askyesno("删除挂载配置？", f"删除“{mount.name}”的挂载配置？\n"
                              "这不会删除 remote 或远端文件。", parent=self.root, default="no"):
            self._invoke("delete_mount", mount.id, self.snapshot["revision"])

    def _mount_action(self, action: str) -> None:
        if self._selected_mount():
            self._invoke(action, self.selected_id)

    def _open_mount_log(self) -> None:
        status = self.snapshot["mounts"].get(self.selected_id)
        if status and status.log_path:
            self._invoke("open_path", status.log_path)

    def _open_gui_log(self) -> None:
        path = self.snapshot["gui"].log_path
        if path:
            self._invoke("open_path", path)

    def _invoke(self, action: str, *args) -> None:
        try:
            getattr(self.controller, action)(*args)
        except Exception as exc:
            self.message_label.set_token("CRITICAL")
            self.message_var.set(str(exc))
        self.refresh(self.controller.snapshot())

    # ------------------------------------------------------------------ 事件
    def handle_event(self, event: dict) -> None:
        if event.get("kind") == "navigate":
            if event.get("page") == "exit":
                self.request_exit()
            else:
                self.show(event.get("page", "mounts"), event.get("mount_id", ""))
            return
        action = event.get("action", "")
        if self.editor and not self.editor.closed:
            self.editor.handle_event(event)
        if action == "save_settings" and self._saving_page:
            page, self._saving_page = self._saving_page, None
            self._lock_form(page, False)
            if event.get("ok"):
                self._load_form(page, self.controller.snapshot())
                self._apply_theme()
            else:
                self.form_errors[page] = event.get("message") or "保存失败，输入已保留。"
        if action == "reload_config" and event.get("ok"):
            for page in ("settings", "service"):
                if not self._dirty(page):
                    self._load_form(page, self.controller.snapshot())
            self._apply_theme()
            if self.editor and not self.editor.closed:
                self.editor.error_var.set("配置已重新载入；挂载输入保留，请更新保存版本。")
        message = event.get("message") or ("操作完成。" if event.get("ok") else "操作失败，请查看状态与日志。")
        labels = {"save_settings": "保存设置", "save_mount": "保存挂载", "delete_mount": "删除挂载",
                  "start_mount": "启动挂载", "stop_mount": "停止挂载", "start_gui": "启动 GUI",
                  "stop_gui": "停止 GUI", "reload_config": "重新载入配置", "shutdown": "退出",
                  "refresh_environment": "刷新环境", "open_gui": "打开网页", "open_mount": "打开挂载",
                  "open_path": "打开位置", "start_all_mounts": "启动全部", "stop_all_mounts": "停止全部",
                  "auto_mount": "开机挂载"}
        if action != "refresh_environment" or not event.get("ok") or not self.message_var.get():
            summary = message if len(message) <= 180 else message[:180] + "… 完整原因见对象详情与日志。"
            self.message_label.set_token("SUCCESS" if event.get("ok") else "CRITICAL")
            self.message_var.set(f"{labels.get(action, '操作')}：{summary}")
        if action == "shutdown" and not event.get("ok"):
            self.root.deiconify()
            flash_taskbar(self.root)
        self.refresh(self.controller.snapshot())

    def _refresh_diagnostics(self, environment: str) -> None:
        cfg = self.snapshot["config"]
        resolved = str(self.snapshot.get("exe") or "")
        try:
            conf = find_rclone_conf(cfg.rclone_conf)
        except Exception:
            conf = None
        text = (f"rclone_tray\n{environment}\n\n应用配置：{config_path()}\n"
                f"rclone 可执行文件：{cfg.rclone_path or (resolved + '（自动发现）' if resolved else '自动发现')}\n"
                f"rclone 配置：{conf or cfg.rclone_conf or '未发现'}\n日志目录：{logs_dir()}\n"
                f"配置版本：{self.snapshot['revision']}\n挂载配置：{len(cfg.mounts)} 个\n"
                f"Windows 登录自启：{'启用' if cfg.startup_enabled else '关闭'}\n"
                "\n密码不显示、不复制到诊断；日志由运行层脱敏。\n"
                "损坏配置不会重置；修复应用配置文件后点击“重新载入配置”。\n"
                "关闭窗口隐藏到托盘；退出应用会确认本应用任务停止。")
        if self.snapshot.get("config_error"):
            text += "\n\n配置错误：" + self.snapshot["config_error"]
        self.about_text.set_text(text)

    # ------------------------------------------------------------------ 杂项
    def _browse_exe(self) -> None:
        if self._saving_page == "settings":
            return
        path = filedialog.askopenfilename(parent=self.root, title="选择 rclone 可执行文件",
                                          filetypes=(("可执行文件", "*.exe"), ("所有文件", "*.*")))
        if path:
            self.vars["settings"]["rclone_path"].set(path)

    def _browse_conf(self) -> None:
        if self._saving_page == "settings":
            return
        path = filedialog.askopenfilename(parent=self.root, title="选择 rclone 配置",
                                          filetypes=(("rclone 配置", "*.conf"), ("所有文件", "*.*")))
        if path:
            self.vars["settings"]["rclone_conf"].set(path)

    def _open_config_dir(self) -> None:
        try:
            conf = find_rclone_conf(self.snapshot["config"].rclone_conf)
        except Exception as exc:
            self.message_label.set_token("CRITICAL")
            self.message_var.set(str(exc))
            return
        self._invoke("open_path", str(conf.parent if conf else config_path().parent))

    def _clear_filter(self) -> None:
        self.search_var.set("")
        self.state_var.set(ALL_STATES)

    def _escape(self, _event=None) -> str:
        if self.root.grab_current() is None:
            self.hide()
        return "break"

    def _save_key(self, _event=None) -> str:
        if self.root.grab_current() is None:
            self._save_form()
        return "break"

    def _search_key(self, _event=None) -> str:
        if self.root.grab_current() is None and self.show("mounts"):
            self.search_field.focus_set()
        return "break"

    def _add_key(self, _event=None) -> str:
        if self.root.grab_current() is None and self.show("mounts"):
            self._add_mount()
        return "break"

