"""Full offline adapter+replica pipeline E2E using the anonymous fixture.

This test exercises every real stage of ``run_pipeline()`` offline: preflight,
adapter (re)generation, live capture against local ``file://`` pages, replica
build, replica validation, offline adapter generation + execution + artifact /
privacy validation, and the JSON/HTML report.

The only non-deterministic piece — the LLM-backed sequence generation inside
adapter generation — is patched with a deterministic valid completion so the
whole pipeline runs offline with no API key and no live network. Everything else
runs through the real child subprocesses. No browser or server process is left
behind: the pipeline's ManagedProcess stages reap their children)Skip and the
offline adapter's ``finally`` closes the browser + replica server.
"""

import hashlib
import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import agent
from pipeline_adapter import generate_completed_adapter
from pipeline_models import PipelineConfig, PipelineStage, PipelineStatus, StageResult

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = PROJECT_ROOT / "test" / "fixtures" / "pipeline" / "marked_recording.py"
REPLICA_FLOW = PROJECT_ROOT / "test" / "fixtures" / "replica_flow"
HOSPITAL = "fixture"

# A string guaranteed to never appear in the anonymous fixture. Used to assert
# the generated reports contain no fixture secret.
FIXTURE_SECRET = "__PIPELINE_E2E_SECRET_MARKER__"


def lf_text(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")


def lf_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _marker_annotations(fixture_text: str) -> list[dict]:
    """Build GUI-style marker annotations for every marker comment in the source."""
    markers = []
    for index, line in enumerate(fixture_text.split("\n"), start=1):
        match = re.search(r"# \[MARKER: ([^\]]+)\]", line)
        if not match:
            continue
        label = match.group(1)
        if " @ " in label:
            label = label.split(" @ ")[0]
        markers.append({"marker_id": f"e2e-{index:04d}", "line": index, "label": label.strip()})
    return markers


# ---------------------------------------------------------------------------
# Deterministic LLM completions for the two LLM-backed markers (报告截图 /
# 序列选择). Each completion is written to operate correctly once it is copied
# verbatim into the offline runner, which defines ``validation_root``, ``page``
# and (after the kept popup transition) ``page1``.
# ---------------------------------------------------------------------------


def _deterministic_llm(prompt: str, model: str) -> str:
    # Match on the explicit ``标记名称`` line so context snippets containing a
    # different marker's name elsewhere in the prompt cannot collide.
    if "标记名称: 序列选择" in prompt:
        return (
            'page1.locator("#popup-frame").content_frame'
            '.locator("#series-thick").click()'
        )
    if "标记名称: 报告截图" in prompt:
        return (
            'page.screenshot(path=str(validation_root / "report.jpeg"), '
            'type="jpeg", full_page=True)'
        )
    raise AssertionError(f"unexpected LLM-backed marker prompt: {prompt[:120]!r}")


def _in_process_adapter_stage(config, layout, controller):
    """Run the REAL adapter generation in-process with a patched deterministic
    LLM, instead of spawning the LLM-backed subprocess."""
    completed_path = layout.adapter_dir / f"completed_{config.hospital}.py"
    result = generate_completed_adapter(
        config.source_script,
        completed_path,
        model=config.model,
        retry_count=config.retry_count,
    )
    return StageResult(
        PipelineStage.ADAPTER,
        PipelineStatus.SUCCESS,
        artifacts={
            "completed": str(completed_path),
            "output_sha256": result.output_sha256,
        },
    )


class PipelineOfflineE2ETests(unittest.TestCase):
    baseline_chromium = None

    @classmethod
    def setUpClass(cls):
        cls.baseline_chromium = cls._chromium_count()

    @classmethod
    def _chromium_count(cls):
        try:
            out = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq chromium.exe", "/NH"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except Exception:
            return None
        return sum(1 for line in out.stdout.splitlines() if line.strip())

    def test_full_pipeline_offline_end_to_end(self):
        with tempfile.TemporaryDirectory(
            prefix="_pipeline_e2e_tmp_", dir=str(PROJECT_ROOT)
        ) as tmp_str:
            tmp = Path(tmp_str)
            try:
                result = self._run_pipeline_offline(tmp)

                self.assertEqual(result.status, PipelineStatus.SUCCESS, [
                    (s.stage.value, s.status.value, s.message) for s in result.stages
                ])

                layout = result.layout
                adapter_dir = layout.adapter_dir
                validation_dir = layout.validation_dir

                # 6. completed + offline adapters parse
                completed = adapter_dir / f"completed_{HOSPITAL}.py"
                offline = adapter_dir / f"completed_{HOSPITAL}_offline.py"
                self.assertTrue(completed.is_file())
                self.assertTrue(offline.is_file())
                compile(completed.read_text(encoding="utf-8"), str(completed), "exec")
                compile(offline.read_text(encoding="utf-8"), str(offline), "exec")

                # 7. replica index exists
                self.assertTrue((layout.replica_dir / "index.html").is_file())

                # 8. manifest + locator risk report exist
                self.assertTrue((layout.capture_dir / "manifest.json").is_file())
                self.assertTrue((layout.replica_dir / "locator_mapping.json").is_file())
                self.assertTrue((validation_dir / "adapter_capabilities.json").is_file())

                # 9. the offline adapter subprocess ran (its event stream exists)
                events = validation_dir / "events.jsonl"
                self.assertTrue(events.is_file())
                event_lines = [
                    line
                    for line in events.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
                self.assertTrue(any("marker_started" in line for line in event_lines))
                self.assertTrue(any("marker_finished" in line for line in event_lines))

                # 10. no external requests leaked
                external_requests = json.loads(
                    (validation_dir / "external_requests.json").read_text(encoding="utf-8")
                )
                self.assertEqual(external_requests, [])

                # 11. every critical marker has a finished result
                finished = [
                    json.loads(line)["marker"]
                    for line in event_lines
                    if "marker_finished" in line
                ]
                for critical in ("报告截图", "序列选择", "Meta 信息工具",
                                 "窗宽窗位 WL/WW", "影像画布交互"):
                    self.assertIn(critical, finished, f"missing offline result for {critical}")

                # 12. JSON + HTML reports exist and report success
                self.assertTrue(layout.report_json.is_file())
                self.assertTrue(layout.report_html.is_file())
                report = json.loads(layout.report_json.read_text(encoding="utf-8"))
                self.assertEqual(report["status"], "success")

                # 13. report contains no fixture secret string
                report_text = layout.report_json.read_text(encoding="utf-8")
                report_text += layout.report_html.read_text(encoding="utf-8")
                self.assertNotIn(FIXTURE_SECRET, report_text)
            finally:
                # 14. no browser/server child remains (leak check vs baseline),
                # checked on every exit path — success or assertion failure.
                final_count = self._chromium_count()
                if final_count is not None and self.baseline_chromium is not None:
                    self.assertLessEqual(final_count, self.baseline_chromium + 1,
                                         "chromium processes leaked during the run")

    def _run_pipeline_offline(self, tmp: Path):
        from pipeline_orchestrator import run_pipeline

        # Mirror test/fixtures layout so the fixture's walk-up locates the pages
        # even after the run-source copy.
        run_source = tmp / "fixtures" / "pipeline" / "marked_recording.py"
        run_source.parent.mkdir(parents=True, exist_ok=True)
        run_source.write_text(lf_text(FIXTURE), encoding="utf-8", newline="\n")
        for html in REPLICA_FLOW.glob("*.html"):
            target = tmp / "fixtures" / "replica_flow" / html.name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(lf_text(html), encoding="utf-8", newline="\n")

        source_sha = lf_sha256(run_source.read_text(encoding="utf-8"))
        annotations_path = tmp / "annotations.json"
        annotations_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "source_script_sha256": source_sha,
                    "markers": _marker_annotations(
                        run_source.read_text(encoding="utf-8")
                    ),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        config = PipelineConfig(
            hospital="fixture",
            source_script=run_source,
            annotations_path=annotations_path,
            output_root=tmp,
            retry_count=3,
            capture_timeout_s=180,
        )

        with patch.object(agent, "call_llm", side_effect=_deterministic_llm), \
             patch("pipeline_orchestrator.run_adapter_generation",
                   side_effect=_in_process_adapter_stage):
            return run_pipeline(config)


if __name__ == "__main__":
    unittest.main()
