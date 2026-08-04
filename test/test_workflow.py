"""端到端工作流测试：模拟一次录制过程，验证文件变化能经过监听层投递到回调。

不真正启动 playwright codegen：用 mock 替换 subprocess.Popen，避免依赖浏览器。
"""
import os
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch, MagicMock

from codegen_manager import CodegenManager, POLL_INTERVAL_SEC


class TestRecordingWorkflow(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, "recorded.py")
        self.calls: list[str] = []
        self.cv = threading.Condition()

    def tearDown(self):
        # 任何残留 manager 都要清理
        if hasattr(self, "mgr"):
            self.mgr.stop()

    def _wait_for_call(self, predicate, timeout: float = 4.0) -> None:
        """等回调累计到满足 predicate，最多 timeout 秒。"""
        deadline = time.time() + timeout
        with self.cv:
            while not predicate():
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                self.cv.wait(timeout=min(remaining, 0.2))

    def test_file_change_drives_callback(self):
        fake_proc = MagicMock()
        fake_proc.poll.return_value = None  # 假装进程活着
        fake_proc.returncode = None

        with patch.object(subprocess, "Popen", return_value=fake_proc):
            mgr = CodegenManager(output_path=self.path, on_update=self.calls.append)
            self.mgr = mgr
            mgr.start("https://example.com")

            # 录制模拟：先写一段，再追加一段
            with open(self.path, "w", encoding="utf-8") as f:
                f.write("page.goto('a')\n")
            new_mtime = time.time() + 2
            os.utime(self.path, (new_mtime, new_mtime))

            def seen_first():
                return any("goto('a')" in c for c in self.calls)

            with self.cv:
                self._wait_for_call(seen_first, timeout=POLL_INTERVAL_SEC * 3 + 2)

            self.assertTrue(seen_first(), f"未捕获到首段内容，回调列表：{self.calls}")

            # 再写一段，验证连续触发
            with open(self.path, "w", encoding="utf-8") as f:
                f.write("page.goto('b')\n")
            os.utime(self.path, (time.time() + 4, time.time() + 4))

            def seen_second():
                return any("goto('b')" in c for c in self.calls)

            with self.cv:
                self._wait_for_call(seen_second, timeout=POLL_INTERVAL_SEC * 3 + 2)

            self.assertTrue(seen_second(), f"未捕获到第二段，回调列表：{self.calls}")

    def test_viewport_fits_inside_available_desktop_area(self):
        viewport = CodegenManager.fit_viewport_to_desktop(1920, 1040)

        self.assertEqual(viewport, (1888, 880))

    def test_callback_errors_do_not_kill_watcher(self):
        """GUI 端 on_update 抛异常时，监听线程不应崩溃，应继续捕获下一次变更。"""
        fake_proc = MagicMock()
        fake_proc.poll.return_value = None

        def bad_cb(_code: str) -> None:
            raise RuntimeError("GUI boom")

        with patch.object(subprocess, "Popen", return_value=fake_proc):
            mgr = CodegenManager(output_path=self.path, on_update=bad_cb)
            self.mgr = mgr
            mgr.start("https://example.com")

            # 第一次写文件：回调会抛，但 manager 应吞掉
            with open(self.path, "w", encoding="utf-8") as f:
                f.write("first\n")
            os.utime(self.path, (time.time() + 1, time.time() + 1))
            time.sleep(POLL_INTERVAL_SEC + 1.5)

            # 第二次写文件：线程应仍在跑，is_running 看的是 Popen，不影响
            with open(self.path, "w", encoding="utf-8") as f:
                f.write("second\n")
            os.utime(self.path, (time.time() + 5, time.time() + 5))
            time.sleep(POLL_INTERVAL_SEC + 1.5)

            self.assertTrue(mgr.is_running(), "子进程看守失效")
            # 轮询线程应该还活着
            self.assertIsNotNone(mgr._poll_thread)
            self.assertTrue(mgr._poll_thread.is_alive(), "轮询线程在回调异常后死亡")


if __name__ == "__main__":
    unittest.main()
