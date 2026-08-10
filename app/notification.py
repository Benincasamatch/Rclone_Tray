"""Windows 原生右下角气泡通知（无需确认，运行于独立线程，非阻塞）。"""
from __future__ import annotations

import ctypes
import os
import sys
import threading
from typing import Any

APP_NAME = "rclone_tray"

_notify_api: Any
try:
    from plyer import notification as _notify_api
except Exception:  # pragma: no cover - plyer 未安装时静默降级
    _notify_api = None


def _resource_path(rel: str) -> str:
    """打包解包目录（sys._MEIPASS）或源码根目录下的资源绝对路径。"""
    base = getattr(sys, "_MEIPASS", None)
    if base is None:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


def _default_icon() -> str:
    """程序图标 assets/icon.ico 的绝对路径；文件不存在时返回空串。"""
    p = _resource_path(os.path.join("assets", "icon.ico"))
    return p if os.path.exists(p) else ""


def notify(title: str = APP_NAME, message: str = "", timeout: int = 5) -> None:
    """弹出 Windows 原生右下角气泡（toast），无需用户确认、自动消失。

    标题默认使用程序名 APP_NAME；气泡附带程序图标 assets/icon.ico。
    运行在独立守护线程中，不阻塞托盘主循环；plyer 缺失或调用失败时
    静默忽略，不影响主程序。
    """
    if _notify_api is None:
        return
    threading.Thread(
        target=_show,
        args=(title, message, max(int(timeout), 3), _default_icon()),
        daemon=True,
        name="native-notify",
    ).start()


def _set_app_id(app_id: str) -> None:
    """设置进程的 AppUserModelID，使 toast 头部来源应用名显示为程序名而非 "Python"。

    默认情况下 python.exe 弹出的通知在头部会显示 "Python"；设置显式
    AppUserModelID 后头部改为显示 app_id。失败时静默忽略。
    """
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except Exception:
        pass


def _show(title: str, message: str, timeout: int, icon: str) -> None:
    try:
        _set_app_id(APP_NAME)
        kwargs = dict(
            title=title,
            message=message,
            app_name=APP_NAME,
            timeout=timeout,
        )
        if icon:
            kwargs["app_icon"] = icon
        _notify_api.notify(**kwargs)
    except Exception:
        # 通知属于锦上添花，任何异常都不应影响托盘主程序
        pass
