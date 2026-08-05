import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pipeline_io import PipelineStore, create_run_layout


class PipelineIoTests(unittest.TestCase):
    def test_run_layout_is_isolated_and_event_log_is_append_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = create_run_layout(Path(tmp), "ftimage", "run-001")
            store = PipelineStore(layout)
            store.emit({"event": "stage_started", "stage": "preflight"})
            store.emit({"event": "stage_finished", "stage": "preflight"})
            events = [
                json.loads(line)
                for line in layout.events_path.read_text(encoding="utf-8").splitlines()
            ]
        self.assertEqual([event["event"] for event in events], [
            "stage_started", "stage_finished"
        ])

    def test_event_payload_redacts_query_values_and_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = create_run_layout(Path(tmp), "ftimage", "run-002")
            PipelineStore(layout).emit({
                "event": "failed",
                "url": "https://example.test/view?token=secret&study=123",
                "authorization": "Bearer hidden",
                "password": "hidden",
            })
            text = layout.events_path.read_text(encoding="utf-8")
        self.assertNotIn("secret", text)
        self.assertNotIn("Bearer hidden", text)
        self.assertNotIn('"password"', text)
        self.assertIn("REDACTED", text)
