import unittest
from pathlib import Path
from unittest.mock import patch

from runtime_python import CODEGEN_MARKER_PYTHON, codegen_python_executable


class RuntimePythonTests(unittest.TestCase):
    def test_interpreter_is_pinned_to_documented_environment(self):
        self.assertEqual(
            CODEGEN_MARKER_PYTHON,
            Path("D:/Anaconda/envs/codegen-marker/python.exe"),
        )

    def test_missing_pinned_interpreter_fails_without_sys_python_fallback(self):
        with patch.object(Path, "is_file", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "codegen-marker"):
                codegen_python_executable()
