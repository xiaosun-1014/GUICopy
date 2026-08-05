import ast
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import agent
from pipeline_adapter import generate_completed_adapter


SOURCE = '''def run():
    # [MARKER: Meta 信息工具]
    page.locator("#dicom").click()
'''


class PipelineAdapterTests(unittest.TestCase):
    def test_generation_publishes_only_syntax_valid_output_and_trace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "processed.py"
            output = root / "adapter" / "completed.py"
            source.write_text(SOURCE, encoding="utf-8")
            result = generate_completed_adapter(
                source, output, model="fixture-model", retry_count=2
            )
            ast.parse(output.read_text(encoding="utf-8"))
        self.assertEqual(result.status, "success")
        self.assertEqual(result.model, "fixture-model")
        self.assertEqual(result.marker_names, ("Meta 信息工具",))
        self.assertEqual(len(result.output_sha256), 64)

    def test_failed_generation_does_not_publish_completed_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "processed.py"
            output = root / "completed.py"
            source.write_text(
                'def run():\n    # [MARKER: 序列选择]\n    page.click()\n',
                encoding="utf-8",
            )
            with patch.object(agent, "call_llm", return_value="```python\nbad = '\n```"):
                with self.assertRaises(RuntimeError):
                    generate_completed_adapter(source, output, retry_count=1)
            self.assertFalse(output.exists())
