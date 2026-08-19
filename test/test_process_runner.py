import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from process_runner import ManagedProcess
from runtime_python import codegen_python_executable


class ManagedProcessTests(unittest.TestCase):
    def test_windows_taskkill_output_is_not_text_decoded(self):
        runner = ManagedProcess(["unused"])
        runner._process = SimpleNamespace(
            pid=123,
            poll=lambda: None,
            wait=lambda timeout=None: 0,
        )
        with patch("process_runner.os.name", "nt"), patch("process_runner.subprocess.run") as run:
            runner._terminate_tree()
        self.assertFalse(run.call_args.kwargs["text"])

    def test_reads_stdout_and_stderr_without_deadlock(self):
        code = (
            "import sys\n"
            "print('{\"event\":\"stdout_ready\"}', flush=True)\n"
            "print('x' * 200000, file=sys.stderr, flush=True)\n"
        )
        events = []
        result = ManagedProcess(
            [codegen_python_executable(), "-c", code],
            cwd=Path.cwd(),
            timeout_s=10,
            on_event=events.append,
        ).run()
        self.assertEqual(result.returncode, 0)
        self.assertIn("stdout_ready", [event.get("event") for event in events])
        self.assertGreater(len(result.stderr), 100000)

    def test_timeout_terminates_exact_process(self):
        runner = ManagedProcess(
            [codegen_python_executable(), "-c", "import time; time.sleep(60)"],
            cwd=Path.cwd(),
            timeout_s=0.2,
        )
        result = runner.run()
        self.assertTrue(result.timed_out)
        self.assertIsNotNone(result.pid)

    def test_jsonl_command_is_delivered_to_child_stdin(self):
        code = (
            "import sys\n"
            "line = sys.stdin.readline()\n"
            "print(line, end='', flush=True)\n"
        )
        runner = ManagedProcess(
            [codegen_python_executable(), "-c", code],
            cwd=Path.cwd(),
            timeout_s=5,
        )
        runner.start()
        runner.send_command({"command": "continue_after_auth"})
        result = runner.wait()
        self.assertIn("continue_after_auth", result.stdout)
