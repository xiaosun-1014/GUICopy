"""CodegenManager 单元测试：覆盖文件读写兜底与内容去重逻辑，不启动真实子进程。"""
import os
import tempfile
import threading
import time
import unittest

from codegen_manager import CodegenManager


class TestCodegenManagerFileIO(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, "rec.py")
        self.calls: list[str] = []
        self.mgr = CodegenManager(output_path=self.path, on_update=self.calls.append)

    def tearDown(self):
        try:
            self.mgr.stop()
        except Exception:
            pass

    def _write(self, content: str) -> None:
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(content)
        # mtime 精度在某些 FS 上是秒级，强制拉大避免抖动
        new_mtime = time.time() + 1
        os.utime(self.path, (new_mtime, new_mtime))

    def test_handle_change_emits_new_content(self):
        self._write("page.goto('a')")
        self.mgr._handle_change("test")
        self.assertEqual(self.calls, ["page.goto('a')"])

    def test_handle_change_dedups_identical_content(self):
        self._write("page.goto('a')")
        self.mgr._handle_change("test")
        self._write("page.goto('a')")  # 内容相同，mtime 不同
        self.mgr._handle_change("test")
        self.assertEqual(self.calls, ["page.goto('a')"])

    def test_safe_read_returns_empty_for_missing_file(self):
        missing = os.path.join(self.tmpdir, "nope.py")
        self.assertEqual(CodegenManager(missing, lambda _c: None)._safe_read(), "")

    def test_stop_is_idempotent(self):
        # 没启动过的 manager 调 stop 不应抛
        mgr = CodegenManager(self.path, lambda _c: None)
        mgr.stop()
        mgr.stop()  # 二次调用


if __name__ == "__main__":
    unittest.main()
