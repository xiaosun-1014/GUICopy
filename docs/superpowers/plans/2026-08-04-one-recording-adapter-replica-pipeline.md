# One-Recording Adapter and Replica Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build one GUI-triggered pipeline that turns a saved marked recording into a completed online adapter, an interactive offline replica, an offline adapter runner, and a validated, privacy-safe report.

**Architecture:** Keep recording, adapter generation, live capture, replica construction, and offline validation as separate units joined by a new subprocess-based orchestrator. The processed recording remains the source of truth for live capture; the completed adapter is validated separately against the generated replica after only its live bootstrap is replaced.

**Tech Stack:** Python 3.10+, standard-library `ast`, `tokenize`, `dataclasses`, `enum`, `json`, `subprocess`, `threading`, `queue`, `pathlib`, PyQt6 `QProcess`, Playwright Sync API, existing Pillow/lxml/PyYAML utilities, and `unittest`.

---

**Revision note (2026-08-04):** The plan explicitly reuses
`BootstrapPlan`/`generate_replay_script`, preserves GUI UUID marker identities
through the manifest, pins every production child to the codegen-marker Python,
separates manifest replay from completed-adapter validation, and reports
replica capability degradation instead of treating unavailable viewer
JavaScript as a false adapter failure.

## Scope and execution prerequisites

This is one cohesive product pipeline, but it spans six implementation phases. Each phase must leave the repository in a testable state and must not depend on uncommitted work from a later phase.

The current directory is not a Git repository. Before implementation, do one of the following:

1. restore the intended `.git` directory or clone the project into a Git checkout; or
2. explicitly approve execution without commits and record each checkpoint in `docs/superpowers/plans/execution-log.md`.

Do not run `git init` implicitly. The commit commands below assume option 1 has been completed.

Use the documented interpreter for every command:

```powershell
$py = 'D:\Anaconda\envs\codegen-marker\python.exe'
& $py -m unittest <modules> -v
```

Do not use the project `out/` directory as a test fixture. Tests must write to `tempfile.TemporaryDirectory()` or fixed anonymous files under `test/fixtures/`.

## File structure locked by this plan

### New production files

- `pipeline_models.py` — pipeline enums, immutable configuration, stage result, report, and run layout models.
- `runtime_python.py` — the single pinned codegen-marker interpreter contract used by GUI and every child process.
- `pipeline_io.py` — run-directory creation, atomic JSON writes, JSONL event storage, payload redaction, and `latest.json`.
- `pipeline_preflight.py` — static source/annotation/auth/model/output checks; never starts a browser.
- `process_runner.py` — concurrent stdout/stderr consumption, JSONL parsing, stdin commands, timeout, cancellation, and exact process-tree cleanup.
- `pipeline_adapter.py` — adapter generation worker invocation, generation trace, syntax compilation, and publication.
- `pipeline_validation.py` — manifest integrity, replica locator checks, network isolation, artifact checks, and privacy scan.
- `pipeline_report.py` — deterministic JSON and self-contained HTML report generation.
- `pipeline_orchestrator.py` — CLI, state transitions, stage orchestration, retries from stable artifacts, stdin control protocol, and final status.
- `test/fixtures/pipeline/marked_recording.py` — anonymous complete local marked recording.

### Modified production files

- `agent.py` — optional structured generation event sink; preserve existing CLI and `process_script()` behavior.
- `batch_capture_replicate.py` — accept explicit timing values, use safe managed subprocess IO, expose capture-only/build-only boundaries, and preserve existing CLI compatibility.
- `rewrite_script.py` — add completed-adapter offline bootstrap generation and marker instrumentation.
- `build_replica.py` — write non-empty locator risk metadata and fail on missing required screenshot assets.
- `replay_helpers.py` — add generic value redaction helpers while preserving `ReplicaServer`.
- `main_gui.py` — launch the orchestrator, buffer JSONL by stream, display stage status, perform graceful cancel, and open final artifacts.
- `markers.py` and `README.md` — align documented fixed JPEG/JSON artifact names with current skills.
- `.gitignore` — exclude run artifacts and sensitive local inputs.

### New test files

- `test/test_pipeline_models.py`
- `test/test_runtime_python.py`
- `test/test_pipeline_io.py`
- `test/test_pipeline_preflight.py`
- `test/test_process_runner.py`
- `test/test_pipeline_adapter.py`
- `test/test_offline_adapter.py`
- `test/test_pipeline_validation.py`
- `test/test_pipeline_orchestrator.py`
- `test/test_pipeline_gui.py`
- `test/test_pipeline_e2e.py`

Do not create a second replica model hierarchy. `ReplicaFlow`, `BootstrapPlan`, `ActionTarget`, and `CaptureTimingProfile` remain in `replica_models.py`.

---

# Phase 0: Documentation discovery and baseline

## Allowed APIs

The following APIs were confirmed from source and are allowed dependencies:

- `agent.parse_markers(script: str) -> List[Dict]` — `agent.py:114`.
- `agent.validate_syntax(code: str) -> Optional[str]` — `agent.py:255`.
- `agent.process_script(script: str, dry_run: bool = False, max_retries: int = 3, model: str = DEFAULT_MODEL) -> str` — `agent.py:427`.
- `rewrite_script.parse_action_plan(source: str) -> ActionPlan` — `rewrite_script.py:126`.
- `rewrite_script.generate_replay_script(replica_directory: str, entry_page_bindings: dict[str, str], replay_steps: list[dict[str, Any]] | None = None) -> str` — `rewrite_script.py:220`.
- `replica_models.BootstrapPlan(source_start_line, source_end_line, skipped_in_offline_replay, entry_page_bindings)` — `replica_models.py:86`.
- `rewrite_script.locator_risk_report(plan: ActionPlan) -> dict[str, int]` — `rewrite_script.py:308`.
- `batch_capture_replicate.run_live_capture(script_path, output_root, timeout_s=900, storage_state=None, interactive_auth=False, emit=None) -> subprocess.CompletedProcess[str]` — `batch_capture_replicate.py:258`.
- `batch_capture_replicate.build_flow_from_snapshots(script_path, capture_root) -> ReplicaFlow` — `batch_capture_replicate.py:437`.
- `batch_capture_replicate.capture_and_build(script_path, output_root, emit=None, storage_state=None, interactive_auth=False) -> Path` — `batch_capture_replicate.py:479`.
- `batch_capture_replicate.validate_annotations(script_path, annotations_path) -> dict[str, object]` — `batch_capture_replicate.py:504`.
- `batch_capture_replicate.build_from_manifest(manifest_path, flow_root, output_root, emit=None) -> Path` — `batch_capture_replicate.py:548`.
- `build_replica.build_replica(flow: ReplicaFlow, source_root: Path, output_root: Path) -> Path` — `build_replica.py:143`.
- `replay_helpers.sha256_file(path)`, `redact_url(url)`, `read_manifest(path, flow_root, verify_source_hash=False)`, `write_manifest(path, flow)`, and `ReplicaServer(root)` — `replay_helpers.py:16-73`.
- `capture_snapshot.sanitize_html(source_html)` and existing snapshot/state APIs — `capture_snapshot.py:100-359`.
- `main_gui.write_source_text(path, source)`, `build_replica_annotations(display_items, source_code)`, and the existing `QProcess` lifecycle — `main_gui.py:70`, `187`, `543-625`.

The implementation must create, rather than assume, these missing APIs:

- `PipelineRun`, `PipelineStage`, `PipelineStatus`, and `PipelineReport`;
- `run_preflight()`;
- `ManagedProcess`;
- `generate_completed_adapter()` and its trace;
- `generate_offline_adapter_script()`;
- `validate_replica()`, `validate_artifacts()`, and `validate_privacy()`;
- `run_pipeline()`;
- GUI stream buffers and business-result handling.

## Anti-pattern guards

- Do not execute a recorded script inside the Qt process.
- Do not use `page.evaluate()` plus iframe `contentDocument`; use Playwright `Frame`.
- Do not capture replica state with `completed_*.py`; use the processed recording.
- Do not treat `replay_replica.py` as completed-adapter validation.
- Do not decide success from subprocess exit code alone.
- Do not read only stdout while leaving stderr piped and unread.
- Do not implement an auth timeout around a blocking `readline()`.
- Do not silently rewrite unstable locators.
- Do not replace iframes with divs.
- Do not copy storage state, cookies, tokens, original scripts, or remote JavaScript into the replica.
- Do not use JPEG pixels for state-diff decisions; JPEG remains a delivery asset only.
- Do not add hospital-specific exceptions directly to completed adapters.
- Do not rely on `out/ftimage/*` as a test fixture.

## Task 0: Freeze the baseline

**Files:**

- Read: `docs/superpowers/specs/2026-08-04-one-recording-adapter-replica-pipeline-design.md`
- Read: `docs/REPLICA_DESIGN.md:1401-1427`
- Test: existing `test/` suite
- Create during execution only: `docs/superpowers/plans/execution-log.md` if Git remains unavailable

- [ ] **Step 1: Record interpreter and dependency versions**

Run:

```powershell
$py = 'D:\Anaconda\envs\codegen-marker\python.exe'
& $py --version
& $py -c "import playwright, PyQt6, PIL, yaml, lxml; print('dependencies-ok')"
& $py -m playwright --version
```

Expected: Python is at least 3.10, imports print `dependencies-ok`, and Playwright reports a version.

- [ ] **Step 2: Run the fast baseline**

Run:

```powershell
& $py -m unittest `
  test.test_workflow `
  test.test_qt_workflow `
  test.test_replica_gui `
  test.test_agent_marker_boundaries `
  test.test_replay_script `
  test.test_build_replica `
  test.test_replica_runtime -v
```

Expected: all selected tests pass.

- [ ] **Step 3: Run both existing replica E2E tests independently**

Run:

```powershell
& $py -m unittest `
  test.test_replica_e2e.ReplicaEndToEndTests.test_marked_local_recording_replays_offline_without_external_requests -v
& $py -m unittest `
  test.test_replica_e2e.ReplicaEndToEndTests.test_popup_frame_sequence_transition_replays_offline -v
```

Expected: both pass. Record each duration because the popup/frame test currently takes roughly 50 seconds.

- [ ] **Step 4: Record known baseline risks**

Add the following exact entries to the execution log or first commit message:

```text
- run_live_capture reads stderr only after child exit: deadlock risk
- await_interactive_auth uses blocking readline: timeout risk
- GUI parses JSONL without per-stream partial-line buffers
- GUI maps exit code 0 directly to success
- completed adapter is not validated against replica
- locator_mapping.json is always empty
- manual GUI edits can desynchronize _display_items and annotation line numbers
- GUI UUID marker IDs are validated but discarded when parse_action_plan regenerates m_000 IDs
- child interpreter fallback can silently select an unsupported system Python
```

- [ ] **Step 5: Create the baseline checkpoint**

```powershell
git add docs/superpowers/specs/2026-08-04-one-recording-adapter-replica-pipeline-design.md `
        docs/superpowers/plans/2026-08-04-one-recording-adapter-replica-pipeline.md
git commit -m "docs: define one-recording adapter replica pipeline"
```

Expected: one documentation-only commit.

---

# Phase 1: Pipeline contracts, run storage, and preflight

## Task 1: Add pipeline models and an atomic event store

**Files:**

- Create: `runtime_python.py`
- Create: `pipeline_models.py`
- Create: `pipeline_io.py`
- Create: `test/test_runtime_python.py`
- Create: `test/test_pipeline_models.py`
- Create: `test/test_pipeline_io.py`
- Modify: `.gitignore`

- [ ] **Step 1: Write failing model and transition tests**

Create `test/test_runtime_python.py`:

```python
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
```

Create `test/test_pipeline_models.py`:

```python
import unittest

from pipeline_models import PipelineStage, PipelineStatus, PipelineState


class PipelineModelTests(unittest.TestCase):
    def test_state_accepts_only_declared_forward_transition(self):
        state = PipelineState.new("run-1")
        state = state.transition(PipelineStage.PREFLIGHT, PipelineStatus.RUNNING)
        state = state.transition(PipelineStage.ADAPTER, PipelineStatus.RUNNING)
        self.assertEqual(state.stage, PipelineStage.ADAPTER)

    def test_state_rejects_skipping_from_preflight_to_replica_validation(self):
        state = PipelineState.new("run-1").transition(
            PipelineStage.PREFLIGHT, PipelineStatus.RUNNING
        )
        with self.assertRaisesRegex(ValueError, "invalid pipeline transition"):
            state.transition(PipelineStage.REPLICA_VALIDATION, PipelineStatus.RUNNING)

    def test_terminal_status_cannot_transition(self):
        state = PipelineState.new("run-1").finish(PipelineStatus.FAILED, "preflight")
        with self.assertRaisesRegex(ValueError, "terminal"):
            state.transition(PipelineStage.ADAPTER, PipelineStatus.RUNNING)
```

Create `test/test_pipeline_io.py`:

```python
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
```

- [ ] **Step 2: Run tests and verify the imports fail**

```powershell
& $py -m unittest test.test_runtime_python test.test_pipeline_models `
  test.test_pipeline_io -v
```

Expected: `ModuleNotFoundError` for one of the new modules.

- [ ] **Step 3: Implement the pinned interpreter contract**

Create `runtime_python.py`:

```python
from pathlib import Path


CODEGEN_MARKER_PYTHON = Path(
    "D:/Anaconda/envs/codegen-marker/python.exe"
)


def codegen_python_executable() -> str:
    if not CODEGEN_MARKER_PYTHON.is_file():
        raise RuntimeError(
            "Required codegen-marker interpreter is missing: "
            f"{CODEGEN_MARKER_PYTHON}"
        )
    return str(CODEGEN_MARKER_PYTHON)
```

Every production `ManagedProcess` command, GUI `QProcess`, adapter worker,
live-capture instrumented child, offline adapter validation, and generated
replay subprocess must use `codegen_python_executable()`. `sys.executable` is
allowed only inside tests that are explicitly testing the current test
process; production code must not use it as a fallback.

- [ ] **Step 4: Implement the pipeline contracts**

Create `pipeline_models.py` with these public types:

```python
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class PipelineStage(str, Enum):
    DRAFT = "draft"
    PREFLIGHT = "preflight"
    ADAPTER = "generating_adapter"
    LIVE_CAPTURE = "capturing_live"
    REPLICA_BUILD = "building_replica"
    REPLICA_VALIDATION = "validating_replica"
    ADAPTER_VALIDATION = "validating_adapter"
    REPORT = "report"


class PipelineStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL = {
    PipelineStatus.SUCCESS,
    PipelineStatus.PARTIAL,
    PipelineStatus.FAILED,
    PipelineStatus.CANCELLED,
}

ORDER = [
    PipelineStage.DRAFT,
    PipelineStage.PREFLIGHT,
    PipelineStage.ADAPTER,
    PipelineStage.LIVE_CAPTURE,
    PipelineStage.REPLICA_BUILD,
    PipelineStage.REPLICA_VALIDATION,
    PipelineStage.ADAPTER_VALIDATION,
    PipelineStage.REPORT,
]


@dataclass(frozen=True)
class PipelineConfig:
    hospital: str
    source_script: Path
    annotations_path: Path
    output_root: Path
    auth_mode: str = "scripted"
    storage_state: Path | None = None
    model: str | None = None
    retry_count: int = 3
    capture_timeout_s: int = 900
    auth_timeout_s: int = 300
    process_exit_grace_s: int = 5


@dataclass(frozen=True)
class StageResult:
    stage: PipelineStage
    status: PipelineStatus
    error_category: str | None = None
    message: str = ""
    artifacts: dict[str, str] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PipelineState:
    run_id: str
    stage: PipelineStage
    status: PipelineStatus
    error_category: str | None = None

    @classmethod
    def new(cls, run_id: str) -> "PipelineState":
        return cls(run_id, PipelineStage.DRAFT, PipelineStatus.PENDING)

    def transition(
        self, stage: PipelineStage, status: PipelineStatus
    ) -> "PipelineState":
        if self.status in TERMINAL:
            raise ValueError("terminal pipeline state cannot transition")
        current = ORDER.index(self.stage)
        target = ORDER.index(stage)
        if target not in {current, current + 1}:
            raise ValueError(
                f"invalid pipeline transition: {self.stage.value} -> {stage.value}"
            )
        return PipelineState(self.run_id, stage, status)

    def finish(
        self, status: PipelineStatus, error_category: str | None = None
    ) -> "PipelineState":
        if status not in TERMINAL:
            raise ValueError("finish requires terminal status")
        return PipelineState(self.run_id, self.stage, status, error_category)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
```

Create `pipeline_io.py` with `RunLayout`, `create_run_layout()`, `PipelineStore.emit()`, `PipelineStore.write_state()`, and atomic JSON writes. Use `os.replace()` from a sibling temporary file; never write a half-complete state file.

The required redaction rules are:

```python
SECRET_KEYS = {
    "authorization", "cookie", "password", "secret",
    "storage_state", "access_token", "refresh_token",
}

def redact_payload(value):
    if isinstance(value, dict):
        return {
            key: ("REDACTED" if key.lower() in SECRET_KEYS else redact_payload(item))
            for key, item in value.items()
            if key.lower() != "password"
        }
    if isinstance(value, list):
        return [redact_payload(item) for item in value]
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        return redact_url(value)
    return value
```

Use this concrete run-layout contract:

```python
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RunLayout:
    root: Path
    source_dir: Path
    adapter_dir: Path
    capture_dir: Path
    replica_dir: Path
    validation_dir: Path
    logs_dir: Path
    state_path: Path
    events_path: Path
    report_json: Path
    report_html: Path


def create_run_layout(
    output_root: Path, hospital: str, run_id: str
) -> RunLayout:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", hospital):
        raise ValueError("hospital name contains unsafe path characters")
    if not re.fullmatch(r"[A-Za-z0-9T_-]+", run_id):
        raise ValueError("run id contains unsafe path characters")
    root = output_root / hospital / "runs" / run_id
    directories = {
        "source_dir": root / "source",
        "adapter_dir": root / "adapter",
        "capture_dir": root / "capture",
        "replica_dir": root / "replica",
        "validation_dir": root / "validation",
        "logs_dir": root / "logs",
    }
    for directory in directories.values():
        directory.mkdir(parents=True, exist_ok=True)
    return RunLayout(
        root=root,
        **directories,
        state_path=root / "pipeline_state.json",
        events_path=root / "pipeline_events.jsonl",
        report_json=root / "pipeline_report.json",
        report_html=root / "pipeline_report.html",
    )
```

`PipelineStore.emit()` must serialize `redact_payload(event)` with
`ensure_ascii=False`, append exactly one newline, write under a lock, and flush
before returning. `PipelineStore.write_state()` must write `state.to_dict()` to
`pipeline_state.json.tmp` and replace `pipeline_state.json` with `os.replace()`.

Add to `.gitignore`:

```gitignore
out/*/runs/
**/storage_state*.json
**/pipeline_events.jsonl
**/pipeline_report.html
```

- [ ] **Step 5: Run the new tests**

```powershell
& $py -m unittest test.test_runtime_python test.test_pipeline_models `
  test.test_pipeline_io -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```powershell
git add runtime_python.py pipeline_models.py pipeline_io.py `
        test/test_runtime_python.py test/test_pipeline_models.py `
        test/test_pipeline_io.py .gitignore
git commit -m "feat: add pipeline state and run storage"
```

## Task 2: Implement fail-closed preflight

**Files:**

- Create: `pipeline_preflight.py`
- Create: `test/test_pipeline_preflight.py`
- Modify: `rewrite_script.py:126-174`
- Modify: `test/test_replica_action_parser.py`
- Modify: `main_gui.py:92-98`

- [ ] **Step 1: Write failing preflight tests**

Create `test/test_pipeline_preflight.py`:

```python
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pipeline_models import PipelineConfig
from pipeline_preflight import run_preflight


SOURCE = '''from playwright.sync_api import sync_playwright
# [MARKER: 报告截图]
page.locator("#report").click()
'''


class PipelinePreflightTests(unittest.TestCase):
    def make_config(self, root: Path, source: str = SOURCE) -> PipelineConfig:
        script = root / "processed.py"
        annotations = root / "replica_annotations.json"
        script.write_text(source, encoding="utf-8", newline="\n")
        annotations.write_text(json.dumps({
            "schema_version": 1,
            "source_script_sha256": hashlib.sha256(source.encode()).hexdigest(),
            "markers": [{"marker_id": "m-1", "line": 2, "label": "报告截图"}],
        }), encoding="utf-8")
        return PipelineConfig("fixture", script, annotations, root / "runs")

    def test_valid_recording_passes_without_starting_browser(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run_preflight(self.make_config(Path(tmp)))
        self.assertTrue(result.ok, result.errors)

    def test_stale_annotations_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = self.make_config(root)
            config.annotations_path.write_text(
                '{"schema_version":1,"source_script_sha256":"stale","markers":[]}',
                encoding="utf-8",
            )
            result = run_preflight(config)
        self.assertIn("annotations_hash_mismatch", result.errors)

    def test_missing_marker_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run_preflight(self.make_config(Path(tmp), "print('no markers')\n"))
        self.assertIn("no_supported_markers", result.errors)

    def test_storage_state_mode_requires_an_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self.make_config(Path(tmp))
            config = PipelineConfig(
                **{**config.__dict__, "auth_mode": "storage-state",
                   "storage_state": Path(tmp) / "missing.json"}
            )
            result = run_preflight(config)
        self.assertIn("storage_state_missing", result.errors)

    def test_hospital_name_cannot_escape_output_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self.make_config(Path(tmp))
            config = PipelineConfig(**{**config.__dict__, "hospital": "../escape"})
            result = run_preflight(config)
        self.assertIn("hospital_name_invalid", result.errors)

    def test_missing_pinned_interpreter_fails_preflight(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch(
                "pipeline_preflight.codegen_python_executable",
                side_effect=RuntimeError("missing"),
            ):
                result = run_preflight(self.make_config(Path(tmp)))
        self.assertIn("interpreter_missing", result.errors)

    def test_annotation_line_or_label_mismatch_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = self.make_config(root)
            payload = json.loads(config.annotations_path.read_text(encoding="utf-8"))
            payload["markers"][0]["line"] = 3
            config.annotations_path.write_text(
                json.dumps(payload), encoding="utf-8"
            )
            result = run_preflight(config)
        self.assertIn("marker_identity_mismatch", result.errors)
```

Add to `test/test_replica_action_parser.py`:

```python
def test_gui_marker_uuid_is_preserved_in_groups_and_actions(self):
    source = '# [MARKER: 报告截图]\npage.locator("#report").click()\n'
    annotations = {
        "markers": [{
            "marker_id": "4f0df6de-71e9-4e3e-a186-f64be41d12fd",
            "line": 1,
            "label": "报告截图",
        }]
    }
    plan = parse_action_plan(source, annotations["markers"])
    self.assertEqual(
        plan.marker_groups[0].marker_id,
        "4f0df6de-71e9-4e3e-a186-f64be41d12fd",
    )
    self.assertEqual(
        plan.marker_groups[0].actions[0].marker_id,
        "4f0df6de-71e9-4e3e-a186-f64be41d12fd",
    )
```

- [ ] **Step 2: Run the tests and verify failure**

```powershell
& $py -m unittest test.test_pipeline_preflight -v
```

Expected: import failure for `pipeline_preflight`.

- [ ] **Step 3: Implement `run_preflight()`**

Create:

```python
from __future__ import annotations

import ast
import re
from dataclasses import dataclass

from agent import MARKER_MAP, parse_markers
from batch_capture_replicate import validate_annotations
from pipeline_models import PipelineConfig
from runtime_python import codegen_python_executable
from rewrite_script import parse_action_plan


@dataclass(frozen=True)
class PreflightResult:
    ok: bool
    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    marker_names: tuple[str, ...]


def run_preflight(config: PipelineConfig) -> PreflightResult:
    errors: list[str] = []
    warnings: list[str] = []
    marker_names: list[str] = []
    if not config.source_script.is_file():
        errors.append("source_script_missing")
        return PreflightResult(False, tuple(errors), (), ())
    source = config.source_script.read_text(encoding="utf-8")
    try:
        ast.parse(source)
        parse_action_plan(source)
    except SyntaxError:
        errors.append("source_syntax_error")
    markers = parse_markers(source)
    marker_names = [marker["name"] for marker in markers]
    if not markers:
        errors.append("no_supported_markers")
    unsupported = sorted({
        name for name in marker_names
        if name not in MARKER_MAP
    })
    warnings.extend(f"unsupported_marker:{name}" for name in unsupported)
    try:
        annotation_payload = validate_annotations(
            config.source_script, config.annotations_path
        )
    except FileNotFoundError:
        errors.append("annotations_missing")
    except ValueError:
        errors.append("annotations_hash_mismatch")
    else:
        try:
            parse_action_plan(source, annotation_payload["markers"])
        except ValueError:
            errors.append("marker_identity_mismatch")
    if config.auth_mode not in {"scripted", "interactive", "storage-state"}:
        errors.append("auth_mode_invalid")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", config.hospital):
        errors.append("hospital_name_invalid")
    try:
        codegen_python_executable()
    except RuntimeError:
        errors.append("interpreter_missing")
    if config.auth_mode == "storage-state" and (
        config.storage_state is None or not config.storage_state.is_file()
    ):
        errors.append("storage_state_missing")
    config.output_root.mkdir(parents=True, exist_ok=True)
    return PreflightResult(
        not errors, tuple(errors), tuple(warnings), tuple(marker_names)
    )
```

Extend `parse_action_plan()` compatibly:

```python
def parse_action_plan(
    source: str,
    marker_annotations: Sequence[Mapping[str, object]] | None = None,
) -> ActionPlan:
```

When annotations are supplied:

1. normalize each annotation label with the same `MARKER_RE` whitespace rules;
2. index annotations by `(line, normalized_label)`;
3. reject duplicate keys or duplicate marker IDs;
4. require every parsed marker comment to have exactly one matching annotation;
5. reject unused annotations;
6. assign the annotation UUID to `MarkerGroup.marker_id` before constructing
   `ActionTarget`, so group and action use the same stable ID.

Without annotations, preserve the current `m_{index:03d}` IDs for compatibility.

Keep `main_gui.export_preflight_errors()` as a compatibility wrapper that creates only syntax-oriented messages. Do not call `run_preflight()` from the GUI process; the orchestrator calls it.

- [ ] **Step 4: Run preflight and existing GUI tests**

```powershell
& $py -m unittest test.test_pipeline_preflight test.test_replica_gui -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```powershell
git add pipeline_preflight.py test/test_pipeline_preflight.py main_gui.py
git commit -m "feat: add pipeline preflight validation"
```

---

# Phase 2: Adapter generation with traceability

## Task 3: Add structured generation events and an adapter worker

**Files:**

- Modify: `agent.py:427-545`
- Create: `pipeline_adapter.py`
- Create: `test/test_pipeline_adapter.py`

- [ ] **Step 1: Write failing tests**

Create:

```python
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
```

- [ ] **Step 2: Run and verify failure**

```powershell
& $py -m unittest test.test_pipeline_adapter -v
```

Expected: import failure for `pipeline_adapter`.

- [ ] **Step 3: Add a backward-compatible event sink to `agent.process_script()`**

Change the signature to:

```python
def process_script(
    script: str,
    dry_run: bool = False,
    max_retries: int = 3,
    model: str = DEFAULT_MODEL,
    event_sink: Callable[[dict[str, object]], None] | None = None,
) -> str:
```

Emit only non-sensitive structured data:

```python
notify = event_sink or (lambda event: None)
notify({"event": "generation_started", "marker_count": len(markers), "model": model})
```

For each attempt:

```python
notify({
    "event": "marker_attempt",
    "marker": marker["name"],
    "attempt": attempt,
    "max_retries": max_retries,
    "prompt_sha256": hashlib.sha256(current_prompt.encode("utf-8")).hexdigest(),
})
```

After a marker succeeds:

```python
notify({"event": "marker_generated", "marker": marker["name"]})
```

Do not persist prompts or model responses. Keep all existing callers valid.

- [ ] **Step 4: Implement atomic adapter publication**

Create `pipeline_adapter.py` with:

```python
from __future__ import annotations

import ast
import hashlib
import py_compile
from dataclasses import dataclass
from pathlib import Path

import agent


@dataclass(frozen=True)
class AdapterGenerationResult:
    status: str
    model: str
    marker_names: tuple[str, ...]
    output_path: Path
    output_sha256: str
    events: tuple[dict[str, object], ...]


def generate_completed_adapter(
    source_path: Path,
    output_path: Path,
    model: str | None = None,
    retry_count: int = 3,
) -> AdapterGenerationResult:
    source = source_path.read_text(encoding="utf-8")
    events: list[dict[str, object]] = []
    selected_model = model or agent.DEFAULT_MODEL
    completed = agent.process_script(
        source,
        max_retries=retry_count,
        model=selected_model,
        event_sink=events.append,
    )
    ast.parse(completed)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary.write_text(completed, encoding="utf-8", newline="\n")
    py_compile.compile(str(temporary), doraise=True)
    temporary.replace(output_path)
    digest = hashlib.sha256(completed.encode("utf-8")).hexdigest()
    return AdapterGenerationResult(
        "success",
        selected_model,
        tuple(marker["name"] for marker in agent.parse_markers(source)),
        output_path,
        digest,
        tuple(events),
    )
```

Add a CLI `main()` to `pipeline_adapter.py` with required `--source`,
`--output`, optional `--model`, and `--retry` arguments. It must print each
structured generation event and one terminal event as JSONL on stdout:

```json
{"event":"adapter_generated","status":"success","output":"completed_fixture.py","output_sha256":"<64 lowercase hex characters>"}
```

Diagnostics go to stderr. The orchestrator must invoke this CLI through
`ManagedProcess`; it must not call `generate_completed_adapter()` in the Qt
process.

- [ ] **Step 5: Run adapter tests and marker regressions**

```powershell
& $py -m unittest `
  test.test_pipeline_adapter `
  test.test_agent_marker_boundaries `
  test.test_marker_apply `
  test.test_meta_extract `
  test.test_canvas_capture -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```powershell
git add agent.py pipeline_adapter.py test/test_pipeline_adapter.py
git commit -m "feat: generate traced completed adapters"
```

---

# Phase 3: Safe process control and live capture hardening

## Task 4: Add a reusable managed subprocess

**Files:**

- Create: `process_runner.py`
- Create: `test/test_process_runner.py`
- Modify: `batch_capture_replicate.py:258-332`

- [ ] **Step 1: Write failing tests for both streams, timeout, and commands**

Create:

```python
import tempfile
import unittest
from pathlib import Path

from process_runner import ManagedProcess
from runtime_python import codegen_python_executable


class ManagedProcessTests(unittest.TestCase):
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
```

- [ ] **Step 2: Run and verify failure**

```powershell
& $py -m unittest test.test_process_runner -v
```

Expected: import failure for `process_runner`.

- [ ] **Step 3: Implement `ManagedProcess`**

The implementation must:

- call `Popen(args, cwd=cwd, env=env, stdin=PIPE, stdout=PIPE, stderr=PIPE, text=True, bufsize=1)`;
- create one reader thread per output stream;
- push `(stream_name, line)` tuples into one queue;
- parse JSON objects only from complete stdout lines;
- retain ordinary stdout and stderr separately;
- expose `start()`, `send_command(payload)`, `cancel()`, `wait()`, and `run()`;
- terminate only the exact spawned process tree;
- return a result containing `pid`, `returncode`, `stdout`, `stderr`, `timed_out`, and `cancelled`.

Use these exact platform rules:

```python
creationflags = (
    subprocess.CREATE_NEW_PROCESS_GROUP
    if os.name == "nt"
    else 0
)
start_new_session = os.name != "nt"
```

For cleanup:

```python
if os.name == "nt":
    subprocess.run(
        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
        check=False,
        capture_output=True,
        text=True,
    )
else:
    os.killpg(process.pid, signal.SIGTERM)
```

After the configured grace period, use the matching forceful termination. Never use a name-based process kill.

- [ ] **Step 4: Refactor `run_live_capture()` to use `ManagedProcess`**

Keep its public signature and `subprocess.CompletedProcess` return type for compatibility. Internally:

```python
managed = ManagedProcess(
    [codegen_python_executable(), str(instrumented_path)],
    cwd=Path(project_root),
    env=environment,
    timeout_s=timeout_s,
    on_event=emit,
)
result = managed.run()
if result.timed_out:
    raise subprocess.TimeoutExpired(result.args, timeout_s)
return subprocess.CompletedProcess(
    result.args, result.returncode, result.stdout, result.stderr
)
```

Do not retain the old single stdout reader plus end-of-process stderr read.

- [ ] **Step 5: Make interactive auth deadline non-blocking**

Replace the blocking `readline()` loop with a daemon reader thread and queue. The deadline loop must call `queue.get(timeout=min(0.25, remaining))`. Preserve the existing JSONL commands and events.

- [ ] **Step 6: Run focused capture tests**

```powershell
& $py -m unittest `
  test.test_process_runner `
  test.test_batch_capture_replicate.BatchCaptureReplicateTests.test_subprocess_runner_executes_instrumented_local_script_once `
  test.test_batch_capture_replicate.BatchCaptureReplicateTests.test_live_capture_removes_stale_snapshot_directories `
  test.test_batch_capture_replicate.BatchCaptureReplicateTests.test_interactive_auth_requires_jsonl_continue_command -v
```

Expected: all tests pass and no test exceeds its explicit timeout.

- [ ] **Step 7: Commit**

```powershell
git add process_runner.py test/test_process_runner.py batch_capture_replicate.py
git commit -m "fix: harden capture subprocess lifecycle"
```

## Task 5: Make capture quality and timeout outcomes explicit

**Files:**

- Modify: `batch_capture_replicate.py`
- Modify: `replica_models.py:103-115`
- Modify: `build_replica.py:143-214`
- Modify: `test/test_batch_capture_replicate.py`
- Modify: `test/test_build_replica.py`

- [ ] **Step 1: Add failing tests**

Add tests that assert:

```python
def test_capture_to_manifest_does_not_build_replica(self):
    manifest = capture_to_manifest(
        script, annotations, output, capture_timeout_s=7
    )
    self.assertTrue(manifest.exists())
    self.assertFalse((output / "replica" / "index.html").exists())

def test_capture_manifest_preserves_gui_marker_uuid(self):
    manifest = capture_to_manifest(script, annotations, output)
    flow = read_manifest(manifest, output)
    self.assertEqual(
        flow.states[0].documents[0].targets[0].marker_id,
        gui_marker_uuid,
    )

def test_capture_and_build_forwards_timeout_to_live_capture(self):
    with patch(
        "batch_capture_replicate.run_live_capture",
        side_effect=subprocess.TimeoutExpired(["fixture"], 7),
    ) as runner:
        with self.assertRaises(subprocess.TimeoutExpired):
            capture_and_build("recorded.py", "out", capture_timeout_s=7)
    self.assertEqual(runner.call_args.kwargs["timeout_s"], 7)

def test_build_from_manifest_verifies_explicit_source_hash(self):
    # Write a valid manifest, mutate its source file, and expect ValueError.
    with self.assertRaisesRegex(ValueError, "hash"):
        build_from_manifest(manifest, flow_root, output, source_path=source)

def test_builder_fails_when_required_screenshot_is_missing(self):
    with self.assertRaisesRegex(FileNotFoundError, "screenshot"):
        build_replica(flow_with_missing_asset, source_root, output_root)
```

- [ ] **Step 2: Run the focused tests and verify failure**

```powershell
& $py -m unittest test.test_batch_capture_replicate test.test_build_replica -v
```

Expected: new tests fail for missing parameter/hash/asset gates.

- [ ] **Step 3: Extend `capture_and_build()`**

First create the public capture-only boundary:

```python
def capture_to_manifest(
    script_path: str | Path,
    annotations_path: str | Path,
    capture_root: str | Path,
    emit: Callable[[dict[str, str]], None] | None = None,
    storage_state: str | Path | None = None,
    interactive_auth: bool = False,
    capture_timeout_s: int = 900,
) -> Path:
    annotation_payload = validate_annotations(script_path, annotations_path)
    marker_annotations = annotation_payload["markers"]
    result = run_live_capture(
        script_path,
        capture_root,
        timeout_s=capture_timeout_s,
        storage_state=storage_state,
        interactive_auth=interactive_auth,
        emit=emit,
        marker_annotations=marker_annotations,
    )
    if result.returncode:
        raise RuntimeError(
            f"instrumented replay failed with exit {result.returncode}: "
            f"{result.stderr[-1000:]}"
        )
    flow = build_flow_from_snapshots(
        script_path, capture_root, marker_annotations=marker_annotations
    )
    manifest_path = Path(capture_root) / "manifest.json"
    write_manifest(manifest_path, flow)
    return manifest_path
```

Then keep `capture_and_build()` as a compatible wrapper with this signature:

```python
def capture_and_build(
    script_path: str | Path,
    output_root: str | Path,
    emit: Callable[[dict[str, str]], None] | None = None,
    storage_state: str | Path | None = None,
    interactive_auth: bool = False,
    capture_timeout_s: int = 900,
    annotations_path: str | Path | None = None,
) -> Path:
```

For backward compatibility, `annotations_path=None` uses temporary
`m_{index:03d}` marker IDs. The product orchestrator must always provide the
annotations path. It must call
`capture_to_manifest(script_path, annotations_path, capture_root, ...)`, then
`build_from_manifest(manifest_path, capture_root, output_root / "replica",
source_path=script_path)`.
Do not duplicate the capture implementation in both functions.

Extend the existing CLI mode choices to
`["capture-only", "offline-build", "live"]`:

- `capture-only` calls `capture_to_manifest()` and emits a terminal event with
  the manifest path;
- `offline-build` preserves the existing manifest build behavior;
- `live` remains the backward-compatible capture-and-build wrapper.

The orchestrator must use `capture-only` through `ManagedProcess` so that
`continue_after_auth` and `cancel` commands can be forwarded to the active
capture child.

Extend these existing functions compatibly:

```text
instrument_marked_actions(
    source,
    use_storage_state=False,
    interactive_auth=False,
    marker_annotations=None,
)
run_live_capture(
    script_path,
    output_root,
    timeout_s=900,
    storage_state=None,
    interactive_auth=False,
    emit=None,
    marker_annotations=None,
)
build_flow_from_snapshots(
    script_path,
    capture_root,
    marker_annotations=None,
)
```

All three must call annotation-aware `parse_action_plan()` with the same marker
list. This is the identity contract from GUI UUID through instrumented action,
snapshot, `ActionTarget.marker_id`, manifest, and report.

Emit:

```python
{"event": "capture_started", "stage": "live_capture"}
{"event": "capture_finished", "stage": "live_capture"}
{"event": "build_started", "stage": "replica_build"}
{"event": "build_finished", "stage": "replica_build", "entrypoint": "replica/index.html"}
```

- [ ] **Step 4: Tighten rebuild and builder integrity**

Extend `build_from_manifest()` compatibly with
`source_path: str | Path | None = None`. When supplied, verify it directly:

```python
flow = read_manifest(manifest_path, flow_root)
if source_path is not None and sha256_file(source_path) != flow.source_script_sha256:
    raise ValueError("source script hash does not match manifest")
```

The orchestrator must always supply the run's source script. Do not copy a
credential-bearing processed script into the capture asset directory merely to
make `verify_source_hash=True` work.

In `build_replica()`, fail before rendering if any `screenshot_asset_relpath` does not resolve to an existing file. Write locator risk metadata derived from the flow rather than `[]`.

Do not mutate `flow.warnings` in place for asset size. Compute a local `build_warnings` list and write it to `replica_build_report.json`.

- [ ] **Step 5: Run capture and builder tests**

```powershell
& $py -m unittest test.test_batch_capture_replicate test.test_build_replica -v
```

Expected: all tests pass. Record duration; no silent 15-minute wait is acceptable in unit tests.

- [ ] **Step 6: Commit**

```powershell
git add batch_capture_replicate.py replica_models.py build_replica.py `
        test/test_batch_capture_replicate.py test/test_build_replica.py
git commit -m "feat: enforce capture and replica integrity gates"
```

---

# Phase 4: Offline adapter generation and replica validation

## Task 6: Generate an offline runner from the completed adapter

**Files:**

- Modify: `rewrite_script.py`
- Create: `test/test_offline_adapter.py`

- [ ] **Step 1: Write failing AST rewrite tests**

Create:

```python
import ast
import unittest

from rewrite_script import generate_offline_adapter_script


COMPLETED = '''from playwright.sync_api import sync_playwright

def run(playwright):
    browser = playwright.chromium.launch(headless=False)
    context = browser.new_context()
    page = context.new_page()
    page.goto("https://real.example/login?token=secret")
    page.locator("#password").fill("secret")
    # [MARKER: 报告截图]
    page.locator("#open-viewer").click()
    # [MARKER: Meta 信息工具]
    page.locator("#metadata").click()
    browser.close()

with sync_playwright() as playwright:
    run(playwright)
'''

COMPLETED_WITH_CANVAS = COMPLETED.replace(
    "# [MARKER: Meta 信息工具]",
    "# [MARKER: 影像画布交互]",
).replace(
    'page.locator("#metadata").click()',
    'page.locator("canvas").click()',
)


class OfflineAdapterTests(unittest.TestCase):
    def test_rewrite_removes_live_bootstrap_but_keeps_post_marker_business_logic(self):
        generated = generate_offline_adapter_script(COMPLETED, ".", "validation")
        ast.parse(generated)
        self.assertNotIn("https://real.example", generated)
        self.assertNotIn('#password").fill', generated)
        self.assertIn('#open-viewer").click', generated)
        self.assertIn('#metadata").click', generated)
        self.assertIn("ReplicaServer", generated)

    def test_rewrite_blocks_non_loopback_requests(self):
        generated = generate_offline_adapter_script(COMPLETED, ".", "validation")
        self.assertIn("context.route", generated)
        self.assertIn("external_requests", generated)
        self.assertIn("offline_external_request", generated)

    def test_rewrite_emits_marker_start_and_finish_events(self):
        generated = generate_offline_adapter_script(COMPLETED, ".", "validation")
        self.assertIn("marker_started", generated)
        self.assertIn("marker_finished", generated)
        self.assertIn("报告截图", generated)
        self.assertIn("Meta 信息工具", generated)

    def test_rewrite_uses_bootstrap_plan_entry_bindings(self):
        generated = generate_offline_adapter_script(COMPLETED, ".", "validation")
        self.assertIn("pages = {\"page\": page}", generated)
        self.assertIn("restored from local entry binding", generated)

    def test_static_only_canvas_policy_emits_degraded_instead_of_false_failure(self):
        generated = generate_offline_adapter_script(
            COMPLETED_WITH_CANVAS,
            ".",
            "validation",
            capability_policy={"影像画布交互": "static-only"},
        )
        self.assertIn("marker_degraded", generated)
        self.assertIn("canvas_dynamic_pixels", generated)
```

- [ ] **Step 2: Run and verify failure**

```powershell
& $py -m unittest test.test_offline_adapter -v
```

Expected: missing `generate_offline_adapter_script`.

- [ ] **Step 3: Implement marker-preserving bootstrap rewrite**

Add:

```python
def generate_offline_adapter_script(
    completed_source: str,
    replica_directory: str,
    validation_directory: str,
    capability_policy: Mapping[str, str] | None = None,
) -> str:
```

Required algorithm:

1. `ast.parse(completed_source)`.
2. Call `parse_action_plan(completed_source)` and use its existing
   `BootstrapPlan`; reject `skipped_in_offline_replay=False`.
3. Use `BootstrapPlan.source_end_line` rather than independently guessing the
   bootstrap boundary.
4. Use `BootstrapPlan.entry_page_bindings` as the page-variable contract.
5. Find the single top-level `run` function; reject zero or multiple matches.
6. Keep imports that do not import local recorded modules.
7. Select only `run.body` statements whose `lineno` is greater than
   `BootstrapPlan.source_end_line`.
8. Remove the final top-level `with sync_playwright(): run(playwright)` invocation.
9. Preserve marker comments by slicing the original source between AST statement boundaries, not by relying only on `ast.unparse()`.
10. Insert `emit({"event":"marker_started","marker":marker_name})` before each marker block and a matching `marker_finished` after its last statement.
11. Provide `browser`, `context`, `page`, and `pages={"page": page}` from the local replica bootstrap.
12. Add a context route that allows only:
    - the active `http://127.0.0.1:<port>` origin;
    - `data:`, `blob:`, and `about:blank`.
13. Abort and record every other request.
14. At completion, write `validation/external_requests.json`; raise `RuntimeError("offline_external_request")` if it is non-empty.
15. Always close the browser and replica server in `finally`.
16. Run `ast.parse()` on the generated source before returning.

Marker execution policy:

- `supported`: execute the completed marker block and fail on any exception;
- `degraded`: execute the block, record a warning, and still fail on unexpected
  locator/logic exceptions;
- `static-only`: do not execute viewer-JS/dynamic-pixel code. Require the
  manifest/replica locator validation for the marker to have passed, emit
  `marker_degraded` with capability `canvas_dynamic_pixels`, and make the
  offline stage `partial`;
- no policy entry defaults to `supported`.

Only `影像画布交互` may use `static-only` in the first release. Do not catch and
silence the entire canvas block after execution; skipping is decided before the
block from the explicit capability matrix, so genuine locator failures remain
visible in Stage 5.

Use `ReplicaServer` from the generated replica's `serve_replica.py`; do not import project-only modules into the portable output except Playwright.

Refactor the local browser/context/page bootstrap rendering currently embedded
in `generate_replay_script()` into one private helper used by both
`generate_replay_script()` and `generate_offline_adapter_script()`. Preserve
the existing public signatures and `test/test_replay_script.py` behavior.
Do not create a second `ReplicaServer`, page-binding model, popup binding model,
or locator-expression implementation.

- [ ] **Step 4: Add a real local execution test**

Extend `test/test_offline_adapter.py` to:

- build a minimal replica with `<button id="open-viewer">`;
- write generated `completed_fixture_offline.py`;
- run it in a subprocess with a 30-second timeout;
- assert exit code 0;
- assert JSONL includes `marker_started` and `marker_finished`;
- assert `external_requests.json` is `[]`.

- [ ] **Step 5: Run rewrite and replay tests**

```powershell
& $py -m unittest test.test_offline_adapter test.test_replay_script -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```powershell
git add rewrite_script.py test/test_offline_adapter.py
git commit -m "feat: generate offline completed adapter runner"
```

## Task 7: Validate manifest, locators, artifacts, network, and privacy

**Files:**

- Create: `pipeline_validation.py`
- Create: `test/test_pipeline_validation.py`
- Modify: `replay_helpers.py`

- [ ] **Step 1: Write failing validator tests**

Create tests for these exact outcomes:

```python
def test_manifest_rejects_dangling_transition():
    result = validate_manifest(flow_with_transition_to_missing_state, root)
    assert "dangling_transition" in result.errors

def test_critical_locator_must_be_unique_and_visible():
    result = validate_replica(replica_root, manifest_path)
    assert result.status == "failed"
    assert "critical_locator_not_unique" in result.errors

def test_coordinate_only_critical_action_is_partial():
    result = validate_locator_risk(flow_with_mouse_only_critical_action)
    assert result.status == "partial"

def test_required_json_must_parse_and_canvas_count_must_be_positive():
    result = validate_artifacts(validation_root, required={"dicom_meta.json"})
    assert "artifact_json_invalid" in result.errors

def test_privacy_scan_rejects_storage_state_and_token_text():
    result = validate_privacy(run_root)
    assert "storage_state_artifact" in result.errors
    assert "secret_pattern" in result.errors

def test_replica_validation_runs_manifest_replay_not_completed_adapter():
    result = validate_replica(replica_root, manifest_path)
    assert result.metrics["driver"] == "replica/replay_replica.py"
    assert result.metrics["manifest_replay_exit_code"] == 0

def test_canvas_marker_declares_viewer_js_and_dynamic_pixels_unsupported():
    result = evaluate_adapter_capabilities(
        expected_markers=("影像画布交互",),
        offline_events=(),
    )
    assert result.status == "partial"
    assert result.metrics["capabilities"]["viewer_js_api"] == "unsupported"
    assert result.metrics["capabilities"]["canvas_dynamic_pixels"] == "unsupported"

def test_always_after_critical_ordinal_action_remains_partial():
    result = validate_locator_risk(
        flow_with_always_after_transition_and_ordinal_locator
    )
    assert result.status == "partial"
```

- [ ] **Step 2: Run and verify failure**

```powershell
& $py -m unittest test.test_pipeline_validation -v
```

Expected: missing `pipeline_validation`.

- [ ] **Step 3: Implement manifest and locator validation**

Public API:

```python
@dataclass(frozen=True)
class ValidationResult:
    status: str
    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    metrics: dict[str, object]
```

Create these exact functions:

```text
validate_manifest(flow: ReplicaFlow, capture_root: Path) -> ValidationResult
validate_locator_risk(flow: ReplicaFlow) -> ValidationResult
validate_replica(
    replica_root: Path,
    manifest_path: Path,
    timeout_ms: int = 5000,
) -> ValidationResult
```

`validate_manifest()` must build `errors`, `warnings`, and `metrics`, then return
`ValidationResult("failed" if errors else "success", tuple(errors),
tuple(warnings), metrics)`. It must check:

- unique state/page/document/action IDs;
- existing entry state;
- no dangling transitions;
- relative asset paths contained inside capture root;
- every referenced screenshot exists;
- every iframe parent document exists;
- source script hash if the source file is present.

`validate_replica()` must use `ReplicaServer` and Playwright to reconstruct every captured locator recipe and assert critical locators have `count()==1` and `is_visible()`. Keyboard and mouse actions are not locators and must remain classified as such.

Before locator inspection, Stage 5 must execute the generated
`replica/replay_replica.py` with `codegen_python_executable()` through
`ManagedProcess`. Record its exit code and driver path. It must never execute
`completed_{hospital}_offline.py`.

Stage 6 is the only stage that executes
`completed_{hospital}_offline.py`. Record the driver name in both stage results
so the report cannot conflate manifest replay with completed-adapter
validation.

- [ ] **Step 4: Implement artifact and privacy validation**

Public API:

```text
validate_artifacts(
    validation_root: Path,
    expected_markers: tuple[str, ...],
    capabilities: Mapping[str, str],
) -> ValidationResult
validate_privacy(run_root: Path) -> ValidationResult
evaluate_adapter_capabilities(
    expected_markers: tuple[str, ...],
    offline_events: tuple[dict[str, object], ...],
) -> ValidationResult
```

Rules:

- `报告截图` requires non-empty `report.jpeg`;
- pre-viewer Meta requires parseable, non-empty `patient_info.json`;
- viewer Meta requires parseable, non-empty `dicom_meta.json`;
- canvas marker requires at least one non-empty `.jpeg` under `canvas_frames/`
  only when `canvas_dynamic_pixels=="supported"`; when it is `unsupported`,
  missing frames produce the declared partial warning
  `artifact_not_verifiable:canvas_frames`, not a false failure;
- files named `storage_state*.json` are always errors;
- scan text files only, capped at 5 MB each;
- reject Authorization/Bearer headers, cookie dumps, password values, and known source query values;
- never print the matched secret; report file path and rule name only.

Automatic privacy validation guarantees only known input secrets, URL query
values, and high-confidence credential patterns. Reports must not embed DOM
text, Metadata values, or screenshots. Raw screenshots/Metadata remain marked
as sensitive local artifacts. Do not claim arbitrary patient names or
accession text can always be detected automatically.

`evaluate_adapter_capabilities()` must write
`validation/adapter_capabilities.json` and return the same matrix in
`metrics["capabilities"]`:

```json
{
  "locator_click_fill": "supported",
  "popup_iframe_transition": "supported",
  "series_dom_selection": "degraded",
  "metadata_dom_read": "degraded",
  "canvas_locate_focus_click": "supported",
  "viewer_js_api": "unsupported",
  "keyboard_wheel_slider_routing": "degraded",
  "canvas_dynamic_pixels": "unsupported"
}
```

Series and Metadata may be promoted to `supported` only with complete region
evidence. A canvas marker makes offline adapter validation at most `partial`
until dynamic pixel transitions are explicitly implemented and verified.
This is a replica capability boundary, not proof that the online adapter is
broken.

`validate_locator_risk()` must apply ordinal/structural/absolute-coordinate
risk after `_always_after` state creation. A forced new state never upgrades
the locator that enters it.

- [ ] **Step 5: Run validation and existing privacy tests**

```powershell
& $py -m unittest `
  test.test_pipeline_validation `
  test.test_capture_snapshot `
  test.test_replica_manifest `
  test.test_replica_runtime -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```powershell
git add pipeline_validation.py replay_helpers.py test/test_pipeline_validation.py
git commit -m "feat: validate replica artifacts and privacy"
```

---

# Phase 5: Orchestration, report, and GUI integration

## Task 8: Build the orchestrator and deterministic reports

**Files:**

- Create: `pipeline_report.py`
- Create: `pipeline_orchestrator.py`
- Create: `test/test_pipeline_orchestrator.py`
- Create: `test/test_pipeline_report.py`

- [ ] **Step 1: Write failing orchestration tests**

Use mocks for all external stages:

```python
def test_pipeline_runs_stages_in_declared_order(self):
    events = []
    result = run_pipeline(config, emit=events.append)
    self.assertEqual(result.status, PipelineStatus.SUCCESS)
    self.assertEqual(
        [event["stage"] for event in events if event["event"] == "stage_started"],
        [
            "preflight",
            "generating_adapter",
            "capturing_live",
            "building_replica",
            "validating_replica",
            "validating_adapter",
            "report",
        ],
    )

def test_critical_validation_failure_produces_failed_not_success(self):
    with patch(
        "pipeline_orchestrator.validate_replica",
        return_value=ValidationResult("failed", ("critical_locator_not_unique",), (), {}),
    ):
        result = run_pipeline(config)
    self.assertEqual(result.status, PipelineStatus.FAILED)

def test_noncritical_locator_risk_produces_partial(self):
    with patch(
        "pipeline_orchestrator.validate_locator_risk",
        return_value=ValidationResult("partial", (), ("coordinate_only",), {}),
    ):
        result = run_pipeline(config)
    self.assertEqual(result.status, PipelineStatus.PARTIAL)

def test_cancel_command_sets_cancelled_and_writes_report(self):
    controller = PipelineController(config)
    controller.cancel()
    result = controller.run()
    self.assertEqual(result.status, PipelineStatus.CANCELLED)
    self.assertTrue(result.layout.report_json.exists())

def test_replica_build_resume_requires_existing_manifest(self):
    with self.assertRaisesRegex(ValueError, "manifest"):
        resume_pipeline(config, run_id="run-without-manifest",
                        operation="replica-build")

def test_offline_validation_resume_does_not_repeat_live_capture(self):
    with patch("pipeline_orchestrator.capture_to_manifest") as capture:
        result = resume_pipeline(config, existing_run_id,
                                 operation="offline-validation")
    capture.assert_not_called()
    self.assertIn(result.status, {PipelineStatus.SUCCESS, PipelineStatus.PARTIAL})

def test_report_distinguishes_manifest_replay_and_completed_adapter_drivers(self):
    result = run_pipeline(config)
    report = json.loads(result.layout.report_json.read_text(encoding="utf-8"))
    self.assertEqual(
        report["drivers"]["replica_validation"],
        "replica/replay_replica.py",
    )
    self.assertEqual(
        report["drivers"]["adapter_validation"],
        "adapter/completed_fixture_offline.py",
    )
    self.assertIn("capabilities", report)
```

- [ ] **Step 2: Run and verify failure**

```powershell
& $py -m unittest test.test_pipeline_orchestrator test.test_pipeline_report -v
```

Expected: missing modules.

- [ ] **Step 3: Implement deterministic report aggregation**

`pipeline_report.py` must expose:

```python
def aggregate_status(results: list[StageResult]) -> PipelineStatus:
    if any(result.status == PipelineStatus.FAILED for result in results):
        return PipelineStatus.FAILED
    if any(result.status == PipelineStatus.CANCELLED for result in results):
        return PipelineStatus.CANCELLED
    if any(result.status == PipelineStatus.PARTIAL for result in results):
        return PipelineStatus.PARTIAL
    return PipelineStatus.SUCCESS


def write_pipeline_report(
    layout: RunLayout,
    config: PipelineConfig,
    results: list[StageResult],
) -> tuple[Path, Path]:
    status = aggregate_status(results)
    payload = redact_payload({
        "schema_version": 1,
        "hospital": config.hospital,
        "source_script": config.source_script.name,
        "status": status.value,
        "stages": [
            {
                **asdict(result),
                "stage": result.stage.value,
                "status": result.status.value,
            }
            for result in results
        ],
    })
    json_text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    json_tmp = layout.report_json.with_suffix(".json.tmp")
    json_tmp.write_text(json_text, encoding="utf-8", newline="\n")
    json_tmp.replace(layout.report_json)
    html_text = (
        "<!doctype html><meta charset=\"utf-8\">"
        "<title>Pipeline report</title><h1>Pipeline report</h1><pre>"
        + html.escape(json_text)
        + "</pre>"
    )
    html_tmp = layout.report_html.with_suffix(".html.tmp")
    html_tmp.write_text(html_text, encoding="utf-8", newline="\n")
    html_tmp.replace(layout.report_html)
    return layout.report_json, layout.report_html
```

The JSON report is the source of truth. The HTML report must render only escaped, redacted JSON fields and local relative artifact links. It must not embed patient JSON or images as base64.

- [ ] **Step 4: Implement `PipelineController` and `run_pipeline()`**

Required public API:

```python
@dataclass(frozen=True)
class PipelineRunResult:
    run_id: str
    status: PipelineStatus
    layout: RunLayout
    stages: tuple[StageResult, ...]


class PipelineController:
    def __init__(
        self,
        config: PipelineConfig,
        emit: Callable[[dict[str, object]], None] | None = None,
        run_id: str | None = None,
        operation: str = "full",
    ) -> None:
        self.config = config
        self.emit = emit or (lambda event: None)
        self.cancelled = threading.Event()
        self.active_process: ManagedProcess | None = None
        self.results: list[StageResult] = []
        self.operation = operation
        if run_id is None:
            self.run_id = new_run_id()
            self.layout = create_run_layout(
                config.output_root, config.hospital, self.run_id
            )
        else:
            self.run_id = run_id
            self.layout = load_existing_run_layout(
                config.output_root, config.hospital, run_id
            )
            validate_resume_prerequisites(self.layout, operation)

    @classmethod
    def resume(
        cls,
        config: PipelineConfig,
        run_id: str,
        operation: str,
        emit: Callable[[dict[str, object]], None] | None = None,
    ) -> "PipelineController":
        return cls(config, emit, run_id=run_id, operation=operation)

    def send_command(self, command: dict[str, object]) -> None:
        if command.get("command") == "cancel":
            self.cancel()
        elif self.active_process is not None:
            self.active_process.send_command(command)

    def cancel(self) -> None:
        self.cancelled.set()
        if self.active_process is not None:
            self.active_process.cancel()

    def run(self) -> PipelineRunResult:
        return self._run_declared_stages()

    def _run_declared_stages(self) -> PipelineRunResult:
        # Implement the numbered stage table immediately below this block.
        # Each row appends one StageResult and emits start/finish events.
        # On the first failed/cancelled result, skip execution stages and
        # always execute report generation before returning.
        return execute_pipeline_stages(self)


def run_pipeline(
    config: PipelineConfig,
    emit: Callable[[dict[str, object]], None] | None = None,
) -> PipelineRunResult:
    return PipelineController(config, emit).run()


def resume_pipeline(
    config: PipelineConfig,
    run_id: str,
    operation: str,
    emit: Callable[[dict[str, object]], None] | None = None,
) -> PipelineRunResult:
    return PipelineController.resume(config, run_id, operation, emit).run()
```

Implement `execute_pipeline_stages(controller)` in the same module. It is the
single stage loop and must use the numbered order below; do not create a second
or recursive controller. The helper must return `PipelineRunResult` after
calling `write_pipeline_report()` in a `finally` block. The comment in
`_run_declared_stages()` describes control flow, not deferred functionality.

Implement `load_existing_run_layout()` by calling the same path constructor
without creating a new run ID and rejecting a missing run root. Implement
`validate_resume_prerequisites()` with these exact gates:

- `adapter-only`: source script and annotations exist;
- `replica-build`: source script and `capture/manifest.json` exist;
- `offline-validation`: completed adapter, capture manifest, and
  `replica/index.html` exist;
- any unknown operation raises `ValueError("unsupported pipeline operation")`.

Implement `new_run_id()` with:

```python
def new_run_id() -> str:
    return (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + "-"
        + secrets.token_hex(3)
    )
```

For a new `full` operation, before preflight copy the processed source and annotations into
`layout.source_dir` with LF-preserving writes, then construct an effective
`PipelineConfig` pointing at those immutable run copies. Do not copy storage
state. All later hashes, generation, capture, and reports use the run copies.
Resume operations use the existing immutable run copies.

Stage order:

1. preflight;
2. completed adapter generation by running `pipeline_adapter.py` through
   `ManagedProcess`;
3. live capture by running `batch_capture_replicate.py --mode capture-only`
   through `ManagedProcess`;
4. replica build from the returned manifest, always passing the explicit
   processed source path for hash verification;
5. manifest/locator/replica/privacy validation;
6. offline adapter generation and subprocess execution;
7. artifact/network/privacy validation;
8. JSON and HTML report;
9. update `out/{hospital}/latest.json` only for `success`.

The two child commands begin exactly with the pinned interpreter:

```python
adapter_args = [
    codegen_python_executable(),
    str(PROJECT_ROOT / "pipeline_adapter.py"),
    "--source", str(run_config.source_script),
    "--output", str(completed_path),
    "--retry", str(run_config.retry_count),
]
capture_args = [
    codegen_python_executable(),
    str(PROJECT_ROOT / "batch_capture_replicate.py"),
    "--mode", "capture-only",
    "--script", str(run_config.source_script),
    "--annotations", str(run_config.annotations_path),
    "--output", str(layout.capture_dir),
]
```

Append `--model`, auth, storage-state, and timeout arguments only when their
corresponding config values are present. Never substitute `sys.executable`.

Write `latest.json` atomically as:

```json
{
  "schema_version": 1,
  "run_id": "20260804T120000Z-a1b2c3",
  "run_relpath": "runs/20260804T120000Z-a1b2c3",
  "report_relpath": "runs/20260804T120000Z-a1b2c3/pipeline_report.json"
}
```

Never create a directory junction or symlink for `latest`; an atomic JSON
pointer is portable on Windows and cannot accidentally replace a run tree.
Do not scan, rename, migrate, delete, or select historical
`completed_*_vN.py`, `out/zscloud/`, or `out/ftimage_runner/` artifacts.
Only `out/{hospital}/runs/{run_id}` participates in pipeline history.

Every stage emits:

```json
{"event":"stage_started","stage":"preflight","run_id":"20260804T120000Z-a1b2c3"}
{"event":"stage_finished","stage":"preflight","status":"success","run_id":"20260804T120000Z-a1b2c3"}
```

On failure:

```json
{"event":"pipeline_finished","status":"failed","error_category":"preflight","report":"pipeline_report.json"}
```

The CLI accepts:

```text
--script
--annotations
--hospital
--output-root
--auth-mode
--storage-state
--model
--retry
--capture-timeout
--auth-timeout
--operation
--run-id
```

It reads stdin JSONL commands `continue_after_auth` and `cancel` in a daemon thread. It prints events to stdout only; ordinary diagnostics go to stderr.

`--operation` choices are:

- `full` — create a new run and execute all stages;
- `adapter-only` — reuse an existing run source and regenerate its adapter;
- `replica-build` — require an existing verified capture manifest and rebuild
  the replica without live capture;
- `offline-validation` — require an existing adapter, manifest, and replica,
  then regenerate/run the offline adapter and reports.

Non-full operations require `--run-id`. They append events to the existing run
and rewrite state/report atomically. They never attempt to restore a browser
session.

- [ ] **Step 5: Add stable error classification**

Use exact categories from the design:

```python
ERROR_CATEGORIES = {
    "preflight",
    "llm_configuration",
    "adapter_generation",
    "authentication",
    "network",
    "authorization",
    "site_unavailable",
    "selector_failure",
    "page_state_timeout",
    "popup_timeout",
    "frame_resolution",
    "capture_failure",
    "replica_build",
    "offline_external_request",
    "artifact_validation",
    "privacy_violation",
    "cancelled",
}
```

Do not classify every timeout as network.

- [ ] **Step 6: Run orchestrator tests**

```powershell
& $py -m unittest test.test_pipeline_models test.test_pipeline_io `
  test.test_pipeline_preflight test.test_pipeline_adapter `
  test.test_pipeline_validation test.test_pipeline_report `
  test.test_pipeline_orchestrator -v
```

Expected: all tests pass without launching a real browser.

- [ ] **Step 7: Commit**

```powershell
git add pipeline_report.py pipeline_orchestrator.py `
        test/test_pipeline_report.py test/test_pipeline_orchestrator.py
git commit -m "feat: orchestrate adapter and replica pipeline"
```

## Task 9: Replace GUI replica export with the product pipeline

**Files:**

- Modify: `main_gui.py:308-625,806-812`
- Create: `test/test_pipeline_gui.py`
- Modify: `test/test_replica_gui.py`

- [ ] **Step 1: Write failing GUI tests**

Required assertions:

```python
def test_primary_button_launches_pipeline_orchestrator(self):
    self.window._on_code_ready(MARKED_SOURCE)
    self.window._on_save()
    with patch("main_gui.QProcess.start") as start:
        self.window._on_export_replica()
    self.assertIn("pipeline_orchestrator.py", self.window._export_process.arguments()[0])
    start.assert_called_once()

def test_partial_jsonl_chunks_are_buffered_per_stream(self):
    self.window._consume_pipeline_chunk("stdout", b'{"event":"stage_')
    self.window._consume_pipeline_chunk("stdout", b'finished","status":"success"}\n')
    self.assertEqual(self.window._last_pipeline_event["status"], "success")

def test_exit_zero_without_success_report_is_failure(self):
    self.window._on_export_finished(0, object())
    self.assertIn("未产生最终验证报告", self.window.statusBar().currentMessage())

def test_cancel_sends_command_before_forced_kill(self):
    self.window._on_cancel_export()
    self.assertEqual(
        self.window._export_process.messages[0],
        b'{"command":"cancel"}\n',
    )

def test_close_event_cancels_active_pipeline(self):
    self.window.close()
    self.assertTrue(self.window._pipeline_cancel_requested)
```

- [ ] **Step 2: Run and verify failure**

```powershell
& $py -m unittest test.test_pipeline_gui -v
```

Expected: missing methods or wrong command.

- [ ] **Step 3: Update the controls and QProcess command**

Change the button label to:

```text
生成 Adapter + 离线复刻
```

Replace the GUI-local interpreter fallback with:

```python
from runtime_python import codegen_python_executable


def replica_python_executable() -> str:
    """Backward-compatible name for the required pipeline interpreter."""
    return codegen_python_executable()
```

If the interpreter is missing, show a preflight error and do not create
`QProcess`. Never fall back to GUI `sys.executable`.

Launch:

```python
process.setArguments([
    str(PROJECT_ROOT / "pipeline_orchestrator.py"),
    "--script", str(source_path),
    "--annotations", str(annotations_path),
    "--hospital", hospital,
    "--output-root", str(recording_path.parent.parent),
    "--auth-mode", str(self.replica_auth_mode.currentData()),
])
```

Here `recording_path.parent` is `out/{hospital}` and
`recording_path.parent.parent` is the shared `out` root expected by
`create_run_layout()`. Add a test that prevents `out/{hospital}/{hospital}/runs`
from being created.

Keep storage state out of the GUI until a file picker and explicit warning are implemented. Scripted and interactive modes remain available.

- [ ] **Step 4: Implement per-stream JSONL buffering**

Initialize:

```python
self._pipeline_buffers = {"stdout": "", "stderr": ""}
self._last_pipeline_event: dict[str, object] | None = None
self._final_pipeline_report: Path | None = None
```

`_consume_pipeline_chunk(stream, chunk)` must:

1. decode and prepend the matching stream buffer;
2. split with `split("\n")`;
3. retain the final incomplete fragment;
4. parse complete stdout lines as JSON;
5. treat stderr as redacted diagnostics;
6. update the GUI only from complete events.

- [ ] **Step 5: Implement graceful cancel and terminal result handling**

Cancel sequence:

1. write `{"command":"cancel"}\n`;
2. start a 5-second `QTimer`;
3. call `terminate()` if still running;
4. call `kill()` only after another 2 seconds.

An exit code of zero is not success unless a `pipeline_finished` event refers to an existing `pipeline_report.json` whose status is `success` or `partial`.

Handle `closeEvent()` with the same cancellation path.

- [ ] **Step 6: Fix annotation generation after manual edits**

Before save/export, derive marker annotations from the actual editor source using `agent.parse_markers()` and the existing anchor IDs where line fingerprints still match. If an edited marker cannot retain an ID, create a new UUID. Do not use stale `_display_items` line numbers.

Add a regression test that edits text above a marker after recording and asserts the saved annotation line equals the marker's actual source line.

- [ ] **Step 7: Remove the embedded sensitive FTImage query**

Replace the hard-coded `stm=` URL with:

```python
FTIMAGE_URL = os.environ.get(
    "FTIMAGE_RECORDING_URL",
    "https://yyx.ftimage.cn/dimage/index.html",
)
```

Update the GUI test to assert scheme, host, and path only. Never assert or commit a token query.

- [ ] **Step 8: Run GUI tests**

```powershell
& $py -m unittest `
  test.test_pipeline_gui `
  test.test_replica_gui `
  test.test_qt_workflow `
  test.test_workflow -v
```

Expected: all pass with the Qt offscreen platform if required.

- [ ] **Step 9: Commit**

```powershell
git add main_gui.py test/test_pipeline_gui.py test/test_replica_gui.py
git commit -m "feat: expose one-click adapter replica pipeline"
```

---

# Phase 6: Full offline E2E, documentation, and real-site release gate

## Task 10: Add the complete anonymous pipeline fixture and offline E2E

**Files:**

- Create: `test/fixtures/pipeline/marked_recording.py`
- Create: `test/test_pipeline_e2e.py`
- Modify: `test/fixtures/replica_flow/*.html` only if a missing semantic control is required

- [ ] **Step 1: Create an anonymous marked recording fixture**

The fixture must use only local `file://` fixture pages and include:

```text
bootstrap navigation
报告截图 marker
popup creation
序列选择 marker
nested iframe
Meta 信息工具 marker
窗宽窗位 WL/WW marker
影像画布交互 marker
browser cleanup
```

Use stable IDs from `test/fixtures/replica_flow/`. Include no real URL, patient value, token, or credential.

- [ ] **Step 2: Write the failing full-pipeline E2E**

The test must:

1. copy the fixture into a temporary run source;
2. create matching annotations with the exact LF SHA-256;
3. patch only the LLM-backed sequence generation with a deterministic valid completion;
4. call `run_pipeline()`;
5. assert final status is `success`;
6. assert `completed_fixture.py` and `completed_fixture_offline.py` parse;
7. assert `replica/index.html` exists;
8. assert manifest and locator risk report exist;
9. assert the offline adapter subprocess ran;
10. assert `external_requests.json == []`;
11. assert every critical marker has a result;
12. assert JSON and HTML reports exist;
13. assert the report contains no fixture secret string;
14. assert no browser or server child remains.

- [ ] **Step 3: Run the new E2E and verify failure**

```powershell
& $py -m unittest test.test_pipeline_e2e -v
```

Expected: fail at the first missing pipeline integration.

- [ ] **Step 4: Make the minimum integration changes**

Only adjust production code for concrete failures exposed by this E2E. Do not add special cases keyed to `"fixture"`, a fixture URL, or a fixture DOM string.

- [ ] **Step 5: Run all offline E2E tests**

```powershell
& $py -m unittest `
  test.test_pipeline_e2e `
  test.test_replica_e2e `
  test.test_replica_runtime `
  test.test_replay_script -v
```

Expected: all pass; every non-local request is actively aborted and the recorded list is empty.

- [ ] **Step 6: Commit**

```powershell
git add test/fixtures/pipeline/marked_recording.py `
        test/test_pipeline_e2e.py `
        pipeline_orchestrator.py pipeline_validation.py rewrite_script.py
git commit -m "test: cover complete offline adapter replica pipeline"
```

## Task 11: Align documentation and artifact contracts

**Files:**

- Modify: `README.md`
- Modify: `markers.py`
- Modify: `.env.example`
- Create: `docs/PIPELINE_RUNBOOK.md`
- Create: `docs/REAL_SITE_SMOKE_TEST.md`
- Create: `test/test_pipeline_documentation.py`

- [ ] **Step 1: Add a documentation contract test**

Add a small unittest that asserts:

```python
readme = Path("README.md").read_text(encoding="utf-8")
self.assertIn("生成 Adapter + 离线复刻", readme)
self.assertIn("report.jpeg", readme)
self.assertIn("dicom_meta.json", readme)
self.assertNotIn("report_*.png", readme)
self.assertNotIn("dicom_meta_*.json", readme)
```

- [ ] **Step 2: Update marker templates**

The report marker template must show:

```python
page.screenshot(
    path=str(SCRIPT_DIR / "report.jpeg"),
    type="jpeg",
    quality=95,
    full_page=True,
)
```

Do not put marker timestamps into fixed output names.

- [ ] **Step 3: Document the one-click path**

`README.md` and `docs/PIPELINE_RUNBOOK.md` must document:

- recording and marker insertion;
- save requirement;
- the one-click GUI operation;
- scripted versus interactive auth;
- run-directory structure;
- meaning of success/partial/failed;
- opening `replica/index.html` via `serve_replica.py`;
- opening `pipeline_report.html`;
- rerunning adapter generation, replica build, or offline validation from stable artifacts;
- privacy warnings for screenshots and Metadata;
- `.env` keys without real values.

- [ ] **Step 4: Add the real-site smoke checklist**

`docs/REAL_SITE_SMOKE_TEST.md` must have separate tables for:

- popup viewer, using a uicloud-like target;
- nested iframe viewer, using a cxhospital-like target;
- FTImage.

Each table records:

```text
date
tester
sanitized site identifier
auth mode
run ID
adapter generation
live capture
popup/frame topology
replica build
offline adapter
external request count
artifact validation
privacy validation
final status
blocker category
```

No patient data or token may be copied into this document.

- [ ] **Step 5: Run docs and marker tests**

```powershell
& $py -m unittest test.test_markers test.test_agent_marker_boundaries `
  test.test_pipeline_documentation -v
```

Expected: all pass.

- [ ] **Step 6: Commit**

```powershell
git add README.md markers.py .env.example docs/PIPELINE_RUNBOOK.md `
        docs/REAL_SITE_SMOKE_TEST.md test/test_pipeline_documentation.py
git commit -m "docs: document adapter replica product pipeline"
```

## Task 12: Final verification and release gate

**Files:**

- Verify: all modified production and test files
- Update only with sanitized results: `docs/REAL_SITE_SMOKE_TEST.md`

- [ ] **Step 1: Scan for prohibited implementation patterns**

Run:

```powershell
rg -n "contentDocument|contentWindow.*document" -g "*.py" .
rg -n "report_.*\.png|dicom_meta_.*\.json" -g "*.py" -g "*.md" .
rg -n "locator_mapping\.json.*\\[\\]" build_replica.py
rg -n "page\.goto\\(\"https://" test/fixtures/pipeline
& $py -m unittest test.test_pipeline_validation.PipelineValidationTests.test_latest_run_contains_no_sensitive_values -v
```

Expected:

- no new iframe DOM access via `contentDocument`;
- no stale artifact contract in active docs/templates;
- locator mapping is no longer hard-coded empty;
- anonymous fixture has no external navigation;
- the privacy validator reports no sensitive values without printing matched secrets.

- [ ] **Step 2: Run syntax compilation**

```powershell
& $py -m compileall -q `
  agent.py batch_capture_replicate.py build_replica.py capture_snapshot.py `
  codegen_manager.py main_gui.py markers.py replay_helpers.py replica_models.py `
  rewrite_script.py pipeline_models.py pipeline_io.py pipeline_preflight.py `
  process_runner.py pipeline_adapter.py pipeline_validation.py pipeline_report.py `
  pipeline_orchestrator.py test
```

Expected: exit code 0.

- [ ] **Step 3: Run the full unit and local integration suite**

```powershell
& $py -m unittest discover -s test -v
```

Expected: all tests pass. If the suite exceeds the agreed CI budget, split browser E2E into a separate command; do not hide or skip it.

- [ ] **Step 4: Run the full anonymous pipeline twice**

Run the pipeline E2E twice into distinct run IDs.

Expected:

- both runs succeed;
- neither overwrites the other;
- `latest.json` points to the second successful run;
- the first run remains intact;
- output hashes are stable for deterministic artifacts;
- no process remains.

- [ ] **Step 5: Perform the three real-site smoke tests**

Run each site manually with authorized credentials:

1. popup viewer;
2. nested iframe viewer;
3. FTImage.

Do not mark the product released unless all three succeed or the user explicitly approves a documented blocker and reduced support statement.

- [ ] **Step 6: Inspect each final report**

For every real run verify:

- completed online adapter exists and compiles;
- offline adapter exists and ran;
- replica loads through localhost;
- critical marker outcomes are present;
- external request count is zero during offline validation;
- required artifacts are non-empty and parseable;
- no storage state is inside the run directory;
- HTML and JSON reports contain no patient identifier or secret;
- browsers and servers exited.

- [ ] **Step 7: Final commit**

```powershell
git add docs/REAL_SITE_SMOKE_TEST.md
git commit -m "test: record sanitized real-site pipeline validation"
```

Expected: the commit contains metrics and status only, never screenshots, Metadata, URLs with query values, credentials, or patient data.

---

## Phase completion gates

### Phase 0 complete when

- baseline results and known risks are recorded;
- the implementation runs in a real Git checkout or no-commit execution is explicitly approved.

### Phase 1 complete when

- pipeline state and events survive process crashes without partial JSON;
- preflight catches syntax, marker, annotation, auth, and output problems without launching a browser.

### Phase 2 complete when

- completed adapters publish atomically;
- syntax-invalid generation never appears as a completed artifact;
- model, prompt hashes, marker attempts, skill identity, and output hash are traceable without storing prompts or responses.

### Phase 3 complete when

- stdout and stderr cannot deadlock;
- login timeout is genuinely bounded;
- cancellation and timeout clean the exact child process tree;
- live capture exposes its timeout and quality warnings.

### Phase 4 complete when

- completed adapter bootstrap is replaced without re-generating marker logic;
- offline validation actively blocks non-local traffic;
- critical locators, manifest integrity, artifacts, and privacy are validated.

### Phase 5 complete when

- the orchestrator, not the GUI, owns business state;
- GUI remains responsive, buffers JSONL correctly, and never treats exit code alone as success;
- cancellation is graceful before forceful termination.

### Phase 6 complete when

- the anonymous full pipeline and existing replica E2E tests pass;
- documentation matches actual artifact names and GUI behavior;
- popup, nested iframe, and FTImage real-site smoke tests satisfy the release gate.

## Final anti-pattern review

Before declaring completion, confirm all of the following are false:

- GUI imports and executes a recorded script;
- completed adapter is used for live replica capture;
- replica self-replay is presented as completed-adapter validation;
- any critical action failure is downgraded to a warning without a partial/failed result;
- stderr remains unread while a child runs;
- auth timeout wraps a permanently blocking read;
- network isolation only observes requests instead of aborting them;
- patient data or credentials appear in the report;
- storage state is copied into a run;
- a hospital-specific fix is applied directly to a generated completed script;
- real-site release is claimed from fixture tests alone.

## Spec coverage map

| Approved design requirement | Implemented by |
|---|---|
| Isolated run directories, state, events, and latest pointer | Tasks 1 and 8 |
| Fail-closed preflight | Task 2 |
| Traced, atomic completed adapter generation | Task 3 |
| Bounded stdout/stderr, auth, timeout, cancellation, cleanup | Tasks 4 and 5 |
| Processed script used for capture | Tasks 5 and 8 |
| Replica build and manifest/asset integrity | Tasks 5 and 7 |
| Completed adapter with local-only bootstrap | Task 6 |
| Active external-network blocking | Tasks 6, 7, and 10 |
| Critical locator and artifact validation | Task 7 |
| Privacy scan and secret-free reports | Tasks 1, 7, and 8 |
| Success/partial/failed/cancelled aggregation | Tasks 1 and 8 |
| Stage reruns from stable artifacts | Task 8 |
| One GUI action and responsive progress/cancel UI | Task 9 |
| Anonymous fixture, offline E2E, and no-process-leak gate | Task 10 |
| Fixed artifact names and operator documentation | Task 11 |
| Popup, nested iframe, and FTImage real-site release gate | Task 12 |
