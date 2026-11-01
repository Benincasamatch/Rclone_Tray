"""Owned, themed confirmations with deterministic grab/focus restoration."""
from __future__ import annotations

import tkinter as tk

from . import theme as T
from .glass import Backdrop, Clear, GlassPanel
from .widgets import Button, Heading, Label
from .window import apply_native_chrome, center_on_owner


def askyesno(title: str, message: str, *, parent: tk.Misc, default="no",
             confirm_text="确认", cancel_text="取消") -> bool:
    owner = parent.winfo_toplevel()
    previous_focus = owner.focus_get()
    previous_grab = owner.grab_current()
    window = tk.Toplevel(owner, name="confirmation")
    window.withdraw()
    window.title(title)
    window.transient(owner)
    window.configure(bg=T.BASE)
    window.resizable(False, False)
    th = T.theme(window)
    px = th.px
    result = False
    backdrop = Backdrop(window)
    backdrop.pack(fill="both", expand=True)
    card = GlassPanel(backdrop, "FLOAT")
    card.pack(fill="both", expand=True, padx=px(T.SPACE_LG), pady=px(T.SPACE_LG))
    Heading(card, title, size=T.FONT_MODULE).pack(anchor="w", padx=px(T.SPACE_XL),
                                                  pady=(px(T.SPACE_XL), px(T.SPACE_SM)))
    Label(card, message, size=T.FONT_BODY, token="TEXT_SECONDARY",
          wrap=px(460)).pack(anchor="w", padx=px(T.SPACE_XL), pady=(0, px(T.SPACE_XL)))
    buttons = Clear(card)
    buttons.pack(fill="x", padx=px(T.SPACE_XL), pady=(0, px(T.SPACE_XL)))

    def finish(value: bool) -> None:
        nonlocal result
        result = value
        window.grab_release()
        window.destroy()

    yes = Button(buttons, confirm_text, lambda: finish(True), variant="primary", name="yes")
    yes.pack(side="right")
    no = Button(buttons, cancel_text, lambda: finish(False), variant="secondary", name="no")
    no.pack(side="right", padx=(0, px(T.SPACE_SM)))
    window.protocol("WM_DELETE_WINDOW", lambda: finish(False))
    window.bind("<Escape>", lambda event: (finish(False), "break")[1])

    def tab(event, backwards=False):
        current = window.focus_get()
        (no if current is yes else yes).focus_set()
        return "break"

    window.bind("<Tab>", tab)
    window.bind("<Shift-Tab>", lambda event: tab(event, True))
    window.bind("<Return>", lambda event: (finish(window.focus_get() is yes), "break")[1])
    window.update_idletasks()
    width = max(px(380), window.winfo_reqwidth())
    height = window.winfo_reqheight()
    center_on_owner(window, owner, width, height)
    window.deiconify()
    apply_native_chrome(window, caption_color=T.BASE)
    window.grab_set()
    (yes if default == "yes" else no).focus_set()
    try:
        owner.wait_window(window)
    finally:
        if previous_grab is not None and previous_grab.winfo_exists():
            previous_grab.grab_set()
        if previous_focus is not None and previous_focus.winfo_exists():
            previous_focus.focus_set()
        elif owner.winfo_exists():
            owner.focus_set()
    return result
