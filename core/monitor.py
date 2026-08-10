"""后台状态轮询线程：周期性触发刷新回调。"""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional


class Monitor:
    def __init__(self, poll_interval: float = 3.0):
        self.poll_interval = poll_interval
        self.on_update: Optional[Callable[[], None]] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="status-monitor"
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                if self.on_update:
                    self.on_update()
            except Exception:
                pass
            self._stop.wait(self.poll_interval)
