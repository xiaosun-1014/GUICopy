"""后台启动 Playwright Codegen 子进程并监听输出文件变化。

设计要点：
1. 文件监听与 GUI 在不同线程，回调函数不要直接操作 Qt 控件；上层把回调包装成 Qt 信号即可。
2. 文件可能晚于监听启动才被创建，所以先确保文件存在再开始 Observer。
3. 兜底轮询：Windows 上 watchdog 对某些网络盘 / 防病毒场景会丢事件，加一个低频轮询作为保险。
4. 只做「原文透传」：标记插入交由 GUI 层处理，本模块不引入规则引擎。
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from typing import Callable, Optional

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer


# 每次轮询时如果 mtime 变化，触发更新
POLL_INTERVAL_SEC = 1.0
BROWSER_FRAME_WIDTH = 32
BROWSER_FRAME_HEIGHT = 160


class CodegenFileHandler(FileSystemEventHandler):
    """watchdog 事件回调。"""

    def __init__(self, on_change: Callable[[str], None], target_path: str):
        super().__init__()
        self._on_change = on_change
        self._target = os.path.abspath(target_path)

    def _maybe_emit(self, src_path: str) -> None:
        if os.path.abspath(src_path) == self._target:
            self._on_change("watchdog")

    def on_modified(self, event):
        if not event.is_directory:
            self._maybe_emit(event.src_path)

    def on_created(self, event):
        if not event.is_directory:
            self._maybe_emit(event.src_path)


class CodegenManager:
    """管理 Codegen 进程 + 文件监听，仅做原文投递，不做内容加工。"""

    def __init__(
        self,
        output_path: str,
        on_update: Callable[[str], None],
        desktop_size: tuple[int, int] | None = None,
    ):
        self.output_path = os.path.abspath(output_path)
        self.on_update = on_update
        self.desktop_size = desktop_size

        self._proc: Optional[subprocess.Popen] = None
        self._observer: Optional[Observer] = None
        self._poll_thread: Optional[threading.Thread] = None
        self._poll_stop = threading.Event()
        self._last_mtime: Optional[float] = None
        self._last_content: str = ""
        self._lock = threading.Lock()

    # ---------- 生命周期 ----------
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self, url: str) -> None:
        if self.is_running():
            return
        self._ensure_output_file()

        # 使用可用工作区而非物理屏幕尺寸，并为 Chromium 外框/工具栏留出空间。
        desktop_w, desktop_h = self.desktop_size or self._get_screen_size()
        viewport_w, viewport_h = self.fit_viewport_to_desktop(desktop_w, desktop_h)
        cmd = [
            "playwright",
            "codegen",
            "--target",
            "python",
            "--viewport-size",
            f"{viewport_w},{viewport_h}",
            "-o",
            self.output_path,
            url,
        ]
        # Windows 下需要 CREATE_NEW_PROCESS_GROUP 才能干净终止
        creationflags = 0
        if sys.platform == "win32":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]

        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )

        self._start_observer()
        self._start_polling()

    def stop(self) -> None:
        self._poll_stop.set()
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=2)
            self._observer = None
        if self._proc is not None and self._proc.poll() is None:
            try:
                if sys.platform == "win32":
                    self._proc.send_signal(signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
                else:
                    self._proc.terminate()
                self._proc.wait(timeout=3)
            except Exception:
                self._proc.kill()
        self._proc = None

    # ---------- 内部 ----------
    def _ensure_output_file(self) -> None:
        os.makedirs(os.path.dirname(self.output_path) or ".", exist_ok=True)
        if not os.path.exists(self.output_path):
            with open(self.output_path, "w", encoding="utf-8") as f:
                f.write("")

    @staticmethod
    def _get_screen_size():
        """返回当前可用桌面区域，供非 Qt 调用方计算安全 viewport。"""
        if sys.platform == "win32":
            import ctypes

            class Rect(ctypes.Structure):
                _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

            user32 = ctypes.windll.user32
            work_area = Rect()
            if user32.SystemParametersInfoW(48, 0, ctypes.byref(work_area), 0):
                return work_area.right - work_area.left, work_area.bottom - work_area.top
            return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
        try:
            import tkinter as tk
            root = tk.Tk()
            root.withdraw()
            w = root.winfo_screenwidth()
            h = root.winfo_screenheight()
            root.destroy()
            return w, h
        except Exception:
            return 1904, 1000

    @staticmethod
    def fit_viewport_to_desktop(desktop_width: int, desktop_height: int) -> tuple[int, int]:
        """Reserve browser chrome so the recorded page remains fully visible."""
        return (
            max(1, desktop_width - BROWSER_FRAME_WIDTH),
            max(1, desktop_height - BROWSER_FRAME_HEIGHT),
        )

    def _start_observer(self) -> None:
        watch_dir = os.path.dirname(self.output_path) or "."
        handler = CodegenFileHandler(self._handle_change, self.output_path)
        observer = Observer()
        observer.schedule(handler, path=watch_dir, recursive=False)
        observer.daemon = True
        observer.start()
        self._observer = observer

    def _start_polling(self) -> None:
        self._poll_stop.clear()
        self._last_mtime = self._safe_mtime()
        self._last_content = self._safe_read()
        t = threading.Thread(target=self._poll_loop, daemon=True)
        t.start()
        self._poll_thread = t

    def _poll_loop(self) -> None:
        while not self._poll_stop.is_set():
            time.sleep(POLL_INTERVAL_SEC)
            mtime = self._safe_mtime()
            if mtime is None:
                continue
            if self._last_mtime is None or mtime > self._last_mtime + 1e-3:
                self._handle_change("poll")

    def _safe_mtime(self) -> Optional[float]:
        try:
            return os.path.getmtime(self.output_path)
        except OSError:
            return None

    def _safe_read(self) -> str:
        try:
            with open(self.output_path, "r", encoding="utf-8") as f:
                return f.read()
        except OSError:
            return ""

    def _handle_change(self, source: str) -> None:
        """任何来源（watchdog / poll）的文件变更都走到这里：读取原文并直接投递。"""
        with self._lock:
            content = self._safe_read()
            if content == self._last_content:
                return
            self._last_content = content
            self._last_mtime = self._safe_mtime()
        try:
            self.on_update(content)
        except Exception:
            # GUI 端异常不能让监听线程退出
            pass
