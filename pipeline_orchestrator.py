"""Pipeline orchestrator: bind adapter generation, live capture, replica build,
and offline validation into one deterministic stage loop with a JSON/HTML report.

Pure orchestration only: every external stage runs in a child subprocess through
:class:`ManagedProcess` (pinned interpreter, never ``sys.executable``); the
orchestrator never executes a recorded script in-process.

Resume operations reuse an existing immutable run directory and never attempt to
restore a browser session or copy storage state.
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
import sys
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

from batch_capture_replicate import build_from_manifest, capture_to_manifest
from orchestrator_events import (
    TerminalGuard,
    normalize_child_event,
    ready_event,
)
from pipeline_io import PipelineStore, RunLayout, create_run_layout
from pipeline_models import (
    PipelineConfig,
    PipelineStage,
    PipelineStatus,
    StageResult,
)
from pipeline_preflight import PreflightResult, run_preflight
from pipeline_report import aggregate_status, write_pipeline_report
from pipeline_validation import (
    ValidationResult,
    evaluate_adapter_capabilities,
    validate_artifacts,
    validate_locator_risk,
    validate_manifest,
    validate_privacy,
    validate_replica,
)
from process_runner import ManagedProcess, ManagedProcessResult
from replay_helpers import read_manifest
from rewrite_script import generate_offline_adapter_script
from runtime_python import codegen_python_executable

PROJECT_ROOT = Path(__file__).resolve().parent

# Realistic timeout for the Chromium-backed manifest replay (Task 7's 5s is too
# short for a real browser boot).
REPLICA_REPLAY_TIMEOUT_MS = 45000

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


class _Cancelled(BaseException):
    """Internal control-flow signal for a mid-stage cancellation."""


@dataclass(frozen=True)
class PipelineRunResult:
    run_id: str
    status: PipelineStatus
    layout: RunLayout
    stages: tuple[StageResult, ...]


def new_run_id() -> str:
    return (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + "-"
        + secrets.token_hex(3)
    )


# ---------------------------------------------------------------------------
# Run layout / resume helpers
# ---------------------------------------------------------------------------


def load_existing_run_layout(
    output_root: Path, hospital: str, run_id: str
) -> RunLayout:
    """Reconstruct a run layout without minting a new run id.

    Rejects a missing run root so a resume never silently creates an empty run.
    """
    root = Path(output_root) / hospital / "runs" / run_id
    if not root.is_dir():
        raise ValueError(f"run not found: {hospital}/{run_id}")
    return create_run_layout(output_root, hospital, run_id)


def _copy_lf(src: Path, dst: Path) -> None:
    """Copy a text file normalizing line endings to LF for stable hashing."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    text = Path(src).read_text(encoding="utf-8")
    dst.write_text(text, encoding="utf-8", newline="\n")


def _prepare_full_run(config: PipelineConfig, layout: RunLayout) -> PipelineConfig:
    """Copy the processed source + annotations into the immutable run source dir.

    Returns an effective config pointing at the run copies. Storage state is
    never copied into the run tree.
    """
    source_copy = layout.source_dir / Path(config.source_script).name
    annotations_copy = layout.source_dir / Path(config.annotations_path).name
    _copy_lf(config.source_script, source_copy)
    _copy_lf(config.annotations_path, annotations_copy)
    return replace(
        config,
        source_script=source_copy,
        annotations_path=annotations_copy,
    )


def validate_resume_prerequisites(layout: RunLayout, operation: str) -> None:
    """Enforce the exact per-operation resume gates (see task brief)."""
    if operation == "adapter-only":
        if not layout.source_dir.is_dir() or not any(layout.source_dir.iterdir()):
            raise ValueError("resume adapter-only requires run source scripts")
    elif operation == "replica-build":
        if not (layout.capture_dir / "manifest.json").is_file():
            raise ValueError("resume replica-build requires capture/manifest.json")
    elif operation == "offline-validation":
        missing = [
            path
            for path in (
                layout.adapter_dir / "completed_offline.py",
                layout.capture_dir / "manifest.json",
                layout.replica_dir / "index.html",
            )
            if not path.is_file()
        ]
        if missing:
            raise ValueError(
                "resume offline-validation requires completed adapter, capture "
                f"manifest, and replica/index.html (missing: {missing})"
            )
    else:
        raise ValueError("unsupported pipeline operation")


# ---------------------------------------------------------------------------
# Child subprocess plumbing
# ---------------------------------------------------------------------------


def _run_managed(
    controller: "PipelineController",
    args: list,
    stage: str,
    timeout_s: float,
    cwd: Path | None = None,
    env: dict | None = None,
) -> ManagedProcessResult:
    """Spawn a child through ManagedProcess and forward its JSON events."""

    def on_event(child: dict) -> None:
        controller._issue(normalize_child_event(child, stage, controller.run_id))

    process = ManagedProcess(
        args,
        cwd=cwd,
        env=env,
        timeout_s=timeout_s,
        on_event=on_event,
    )
    controller.active_process = process
    result = process.run()
    if result.cancelled:
        raise _Cancelled()
    return result


def _required_flag(args: list, name: str, value) -> None:
    if value is not None:
        args += [name, str(value)]


def _last_json_event(text: str, name: str) -> Optional[dict]:
    """Return the last parsed JSON object in ``text`` whose ``event`` is ``name``."""
    found = None
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict) and obj.get("event") == name:
            found = obj
    return found


# ---------------------------------------------------------------------------
# Stage implementations (module-level so tests can patch them)
# ---------------------------------------------------------------------------


def run_preflight_stage(
    config: PipelineConfig, layout: RunLayout, controller: "PipelineController"
) -> StageResult:
    pre = run_preflight(config)
    if not pre.ok:
        return StageResult(
            PipelineStage.PREFLIGHT,
            PipelineStatus.FAILED,
            "preflight",
            "preflight failed: " + ";".join(pre.errors),
        )
    return StageResult(
        PipelineStage.PREFLIGHT,
        PipelineStatus.SUCCESS,
        metrics={
            "markers": list(pre.marker_names),
            "warnings": list(pre.warnings),
        },
    )


def run_adapter_generation(
    config: PipelineConfig, layout: RunLayout, controller: "PipelineController"
) -> StageResult:
    """Regenerate the completed adapter from the run's processed source."""
    completed_path = layout.adapter_dir / f"completed_{config.hospital}.py"
    args = [
        codegen_python_executable(),  # pinned interpreter, never sys.executable
        str(PROJECT_ROOT / "pipeline_adapter.py"),
        "--source", str(config.source_script),
        "--output", str(completed_path),
        "--retry", str(config.retry_count),
    ]
    _required_flag(args, "--model", config.model)
    try:
        result = _run_managed(
            controller, args, "generating_adapter",
            timeout_s=max(config.capture_timeout_s, 600),
        )
    except _Cancelled:
        return StageResult(PipelineStage.ADAPTER, PipelineStatus.CANCELLED, "cancelled")
    event = _last_json_event(result.stdout, "adapter_generated")
    if result.returncode != 0 or event is None or event.get("status") != "success":
        category = "llm_configuration" if config.model else "adapter_generation"
        return StageResult(
            PipelineStage.ADAPTER,
            PipelineStatus.FAILED,
            category,
            (result.stderr or result.stdout or "adapter generation failed").strip()[-2000:],
        )
    return StageResult(
        PipelineStage.ADAPTER,
        PipelineStatus.SUCCESS,
        artifacts={
            "completed": str(completed_path),
            "output_sha256": str(event.get("output_sha256", "")),
        },
    )


def run_capture(
    config: PipelineConfig, layout: RunLayout, controller: "PipelineController"
) -> StageResult:
    """Run live capture (capture-only) through the batch capture subprocess."""
    args = [
        codegen_python_executable(),
        str(PROJECT_ROOT / "batch_capture_replicate.py"),
        "--mode", "capture-only",
        "--script", str(config.source_script),
        "--annotations", str(config.annotations_path),
        "--output", str(layout.capture_dir),
        "--auth-mode", config.auth_mode,
        "--capture-timeout", str(config.capture_timeout_s),
    ]
    if config.auth_mode == "storage-state" and config.storage_state is not None:
        args += ["--storage-state", str(config.storage_state)]
    try:
        result = _run_managed(
            controller, args, "capturing_live",
            timeout_s=config.capture_timeout_s + config.process_exit_grace_s,
        )
    except _Cancelled:
        return StageResult(
            PipelineStage.LIVE_CAPTURE, PipelineStatus.CANCELLED, "cancelled"
        )
    event = _last_json_event(result.stdout, "completed")
    manifest_path: Path | None = None
    if event is not None and event.get("entrypoint"):
        candidate = Path(str(event["entrypoint"]))
        if candidate.is_absolute():
            manifest_path = candidate
        else:
            manifest_path = Path(layout.capture_dir) / candidate
    if result.returncode != 0 or manifest_path is None or not manifest_path.is_file():
        category = "network"
        hint = (result.stderr or result.stdout or "capture failed").strip()[-2000:]
        if "auth" in hint.lower() or "login" in hint.lower():
            category = "authentication"
        return StageResult(
            PipelineStage.LIVE_CAPTURE, PipelineStatus.FAILED, category, hint
        )
    return StageResult(
        PipelineStage.LIVE_CAPTURE,
        PipelineStatus.SUCCESS,
        artifacts={"manifest_path": str(manifest_path)},
    )


def run_replica_build(
    config: PipelineConfig,
    layout: RunLayout,
    controller: "PipelineController",
    manifest_path: Path | None = None,
) -> StageResult:
    """Build the local replica from an already-captured manifest (in-process)."""
    manifest_path = manifest_path or (layout.capture_dir / "manifest.json")

    def child_emit(event: dict) -> None:
        controller._issue(
            normalize_child_event(event, "building_replica", controller.run_id)
        )

    try:
        entrypoint = build_from_manifest(
            manifest_path,
            layout.capture_dir,
            layout.replica_dir,
            emit=child_emit,
            source_path=config.source_script,
        )
    except Exception as exc:  # noqa: BLE001 - surfaced as a build failure
        return StageResult(
            PipelineStage.REPLICA_BUILD,
            PipelineStatus.FAILED,
            "replica_build",
            str(exc),
        )
    return StageResult(
        PipelineStage.REPLICA_BUILD,
        PipelineStatus.SUCCESS,
        artifacts={"entrypoint": str(entrypoint)},
    )


def _combine_validation(
    stage: PipelineStage,
    *results: ValidationResult,
) -> tuple[PipelineStatus, list[str], list[str], dict]:
    errors: list[str] = []
    warnings: list[str] = []
    metrics: dict[str, object] = {}
    for res in results:
        errors.extend(res.errors)
        warnings.extend(res.warnings)
        for key, value in (res.metrics or {}).items():
            metrics.setdefault(key, value)
    if errors:
        return PipelineStatus.FAILED, errors, warnings, metrics
    if warnings:
        return PipelineStatus.PARTIAL, errors, warnings, metrics
    return PipelineStatus.SUCCESS, errors, warnings, metrics


def run_replica_validation(
    config: PipelineConfig, layout: RunLayout, controller: "PipelineController"
) -> StageResult:
    """Stage 5: manifest / locator / replica / privacy validation."""
    manifest = layout.capture_dir / "manifest.json"
    if not manifest.is_file():
        return StageResult(
            PipelineStage.REPLICA_VALIDATION,
            PipelineStatus.FAILED,
            "replica_build",
            "capture manifest missing",
        )
    try:
        flow = read_manifest(manifest, layout.capture_dir)
    except Exception as exc:  # noqa: BLE001
        return StageResult(
            PipelineStage.REPLICA_VALIDATION,
            PipelineStatus.FAILED,
            "replica_build",
            f"manifest_read_failed:{type(exc).__name__}",
        )
    res_manifest = validate_manifest(flow, layout.capture_dir)
    res_locator = validate_locator_risk(flow)
    res_replica = validate_replica(
        layout.root, manifest, timeout_ms=REPLICA_REPLAY_TIMEOUT_MS
    )
    res_privacy = validate_privacy(layout.root)
    combined, errors, warnings, metrics = _combine_validation(
        PipelineStage.REPLICA_VALIDATION,
        res_manifest,
        res_locator,
        res_replica,
        res_privacy,
    )
    if res_replica.metrics.get("driver"):
        metrics["driver"] = res_replica.metrics["driver"]
    return StageResult(
        PipelineStage.REPLICA_VALIDATION,
        combined,
        ("privacy_violation" if res_privacy.errors else None)
        if combined == PipelineStatus.FAILED
        else None,
        ";".join(errors[:20]),
        metrics=metrics,
    )


def run_adapter_validation(
    config: PipelineConfig,
    layout: RunLayout,
    controller: "PipelineController",
    marker_names: Iterable[str] = (),
) -> StageResult:
    """Stage 6: generate the offline adapter and execute it, then validate."""
    completed_path = layout.adapter_dir / f"completed_{config.hospital}.py"
    offline_path = layout.adapter_dir / f"completed_{config.hospital}_offline.py"
    driver = f"adapter/{offline_path.name}"

    if not completed_path.is_file():
        return StageResult(
            PipelineStage.ADAPTER_VALIDATION,
            PipelineStatus.FAILED,
            "adapter_generation",
            "completed adapter missing for offline generation",
        )
    try:
        completed_source = completed_path.read_text(encoding="utf-8")
        offline_source = generate_offline_adapter_script(
            completed_source,
            str(layout.replica_dir),
            str(layout.validation_dir),
        )
        offline_path.parent.mkdir(parents=True, exist_ok=True)
        offline_path.write_text(offline_source, encoding="utf-8", newline="\n")
    except Exception as exc:  # noqa: BLE001
        return StageResult(
            PipelineStage.ADAPTER_VALIDATION,
            PipelineStatus.FAILED,
            "offline_external_request",
            str(exc),
        )

    # Execute the freshly generated offline runner as a subprocess.
    try:
        result = _run_managed(
            controller,
            [codegen_python_executable(), str(offline_path)],
            "validating_adapter",
            timeout_s=config.capture_timeout_s + config.process_exit_grace_s,
            cwd=offline_path.parent,
        )
    except _Cancelled:
        return StageResult(
            PipelineStage.ADAPTER_VALIDATION, PipelineStatus.CANCELLED, "cancelled"
        )
    if result.returncode != 0:
        category = (
            "offline_external_request"
            if "offline_external_request" in (result.stderr or "")
            else "offline_external_request"
        )
        return StageResult(
            PipelineStage.ADAPTER_VALIDATION,
            PipelineStatus.FAILED,
            category,
            (result.stderr or result.stdout).strip()[-2000:],
        )

    capabilities_result = evaluate_adapter_capabilities(
        tuple(marker_names), ()
    )
    artifacts_result = validate_artifacts(
        layout.validation_dir,
        expected_markers=tuple(marker_names),
        capabilities=(capabilities_result.metrics.get("capabilities") or {}),
    )
    privacy_result = validate_privacy(layout.root)
    combined, errors, warnings, metrics = _combine_validation(
        PipelineStage.ADAPTER_VALIDATION,
        capabilities_result,
        artifacts_result,
        privacy_result,
    )
    metrics["driver"] = driver
    if capabilities_result.metrics.get("capabilities"):
        metrics["capabilities"] = capabilities_result.metrics["capabilities"]
    return StageResult(
        PipelineStage.ADAPTER_VALIDATION,
        combined,
        None,
        ";".join(errors[:20]),
        artifacts={"offline_adapter": str(offline_path)},
        metrics=metrics,
    )


# ---------------------------------------------------------------------------
# PipelineController
# ---------------------------------------------------------------------------


class PipelineController:
    def __init__(
        self,
        config: PipelineConfig,
        emit: Callable[[dict[str, object]], None] | None = None,
        run_id: str | None = None,
        operation: str = "full",
    ) -> None:
        if operation not in {"full", "adapter-only", "replica-build", "offline-validation"}:
            raise ValueError("unsupported pipeline operation")
        self.config = config
        self.emit = emit or (lambda event: None)
        self.operation = operation
        self.cancelled = threading.Event()
        self.active_process: ManagedProcess | None = None
        self.results: list[StageResult] = []
        self.last_error_category: str | None = None
        self._guard = TerminalGuard()

        if run_id is None:
            self.run_id = new_run_id()
            self.layout = create_run_layout(
                config.output_root, config.hospital, self.run_id
            )
            if operation == "full":
                self.config = _prepare_full_run(config, self.layout)
        else:
            self.run_id = run_id
            self.layout = load_existing_run_layout(
                config.output_root, config.hospital, run_id
            )
            validate_resume_prerequisites(self.layout, operation)

        self.store = PipelineStore(self.layout)

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

    def _issue(self, event: dict) -> None:
        event.setdefault("run_id", self.run_id)
        kind = event.get("event")
        if kind in ("fatal", "completed", "summary"):
            self._guard.note(str(kind))
        self.emit(event)
        self.store.emit(event)

    def run(self) -> PipelineRunResult:
        return execute_pipeline_stages(self)


def _stage_plan(c: PipelineController):
    """Yield ``(stage, error_category, fn)`` tuples for the current operation."""
    config = c.config
    layout = c.layout
    state: dict = {}

    def replica_build_fn():
        manifest = state.get("manifest") or (layout.capture_dir / "manifest.json")
        return run_replica_build(config, layout, c, manifest)

    def marker_names():
        marker_result = next(
            (r for r in c.results if r.stage == PipelineStage.PREFLIGHT), None
        )
        if marker_result is not None and marker_result.metrics:
            return marker_result.metrics.get("markers", ())
        return ()

    plan: list[tuple[PipelineStage, str, Callable]] = []
    if c.operation == "full":
        plan += [
            (PipelineStage.PREFLIGHT, "preflight",
             lambda: run_preflight_stage(config, layout, c)),
            (PipelineStage.ADAPTER, "adapter_generation",
             lambda: run_adapter_generation(config, layout, c)),
        ]

        def _capture():
            res = run_capture(config, layout, c)
            if res.status == PipelineStatus.SUCCESS and res.artifacts.get("manifest_path"):
                state["manifest"] = Path(res.artifacts["manifest_path"])
            return res

        plan += [
            (PipelineStage.LIVE_CAPTURE, "capture_failure", _capture),
            (PipelineStage.REPLICA_BUILD, "replica_build", replica_build_fn),
            (PipelineStage.REPLICA_VALIDATION, "replica_build",
             lambda: run_replica_validation(config, layout, c)),
            (PipelineStage.ADAPTER_VALIDATION, "offline_external_request",
             lambda: run_adapter_validation(config, layout, c, marker_names())),
        ]
    elif c.operation == "adapter-only":
        plan += [
            (PipelineStage.ADAPTER, "adapter_generation",
             lambda: run_adapter_generation(config, layout, c)),
        ]
    elif c.operation == "replica-build":
        plan += [
            (PipelineStage.REPLICA_BUILD, "replica_build", replica_build_fn),
            (PipelineStage.REPLICA_VALIDATION, "replica_build",
             lambda: run_replica_validation(config, layout, c)),
        ]
    elif c.operation == "offline-validation":
        plan += [
            (PipelineStage.ADAPTER_VALIDATION, "offline_external_request",
             lambda: run_adapter_validation(config, layout, c, marker_names())),
        ]
    else:  # pragma: no cover - guarded in __init__
        raise ValueError("unsupported pipeline operation")
    return plan


def _run_stage(c: PipelineController, stage, error_category, fn):
    """Run one stage; return (continue: bool, result: StageResult)."""
    c._issue({"event": "stage_started", "stage": stage.value})
    try:
        result = fn()
    except _Cancelled:
        result = StageResult(stage, PipelineStatus.CANCELLED, "cancelled")
    except Exception as exc:  # noqa: BLE001 - any unexpected failure fails the stage
        result = StageResult(stage, PipelineStatus.FAILED, error_category, str(exc))
    c.results.append(result)
    if result.status in (PipelineStatus.FAILED, PipelineStatus.CANCELLED):
        c.last_error_category = result.error_category
        c._issue(
            {"event": "stage_finished", "stage": stage.value, "status": result.status.value}
        )
        return False, result
    c._issue({"event": "stage_finished", "stage": stage.value, "status": "success"})
    return True, result


def _write_latest_json(layout: RunLayout, config: PipelineConfig, run_id: str) -> None:
    """Write ``out/{hospital}/latest.json`` atomically (pointer only)."""
    payload = {
        "schema_version": 1,
        "run_id": run_id,
        "run_relpath": str(layout.root.relative_to(Path(config.output_root))).replace("\\", "/"),
        "report_relpath": str(
            layout.report_json.relative_to(Path(config.output_root))
        ).replace("\\", "/"),
    }
    latest = Path(config.output_root) / config.hospital / "latest.json"
    latest.parent.mkdir(parents=True, exist_ok=True)
    tmp = latest.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n"
    )
    tmp.replace(latest)


def execute_pipeline_stages(c: PipelineController) -> PipelineRunResult:
    """Single stage loop: run stages, always report, then terminate per D4.

    The report stage is a virtual stage: it emits stage events and writes report
    files but never touches ``pipeline_state.json``. ``latest.json`` is written
    atomically only for a successful run.
    """
    # protocol handshake
    c._issue(ready_event(c.run_id))

    plan = _stage_plan(c)
    for stage, error_category, fn in plan:
        if c.cancelled.is_set():
            c._issue({"event": "stage_finished", "stage": stage.value, "status": "cancelled"})
            c.last_error_category = "cancelled"
            break
        cont, _ = _run_stage(c, stage, error_category, fn)
        if not cont:
            break

    # virtual report stage (never skipped)
    c._issue({"event": "stage_started", "stage": PipelineStage.REPORT.value})
    try:
        write_pipeline_report(c.layout, c.config, c.results)
        report_result = StageResult(PipelineStage.REPORT, PipelineStatus.SUCCESS)
    except Exception as exc:  # noqa: BLE001
        report_result = StageResult(
            PipelineStage.REPORT, PipelineStatus.FAILED, "artifact_validation", str(exc)
        )
    c.results.append(report_result)
    c._issue(
        {
            "event": "stage_finished",
            "stage": PipelineStage.REPORT.value,
            "status": report_result.status.value,
        }
    )

    # deterministic terminal status
    if c.cancelled.is_set():
        final_status = PipelineStatus.CANCELLED
    elif report_result.status == PipelineStatus.FAILED:
        final_status = PipelineStatus.FAILED
    else:
        final_status = aggregate_status(c.results)

    if final_status == PipelineStatus.FAILED and c.last_error_category:
        c._issue(
            {
                "event": "fatal",
                "error_category": c.last_error_category or "artifact_validation",
                "stage": None,
            }
        )
    c._issue({"event": "summary", "status": final_status.value})
    c._issue(
        {
            "event": "completed",
            "status": final_status.value,
            "error_category": c.last_error_category,
            "report": c.layout.report_json.name,
        }
    )
    c._guard.certify()

    if final_status == PipelineStatus.SUCCESS:
        _write_latest_json(c.layout, c.config, c.run_id)

    return PipelineRunResult(
        run_id=c.run_id,
        status=final_status,
        layout=c.layout,
        stages=tuple(c.results),
    )


# ---------------------------------------------------------------------------
# Entry point helpers
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli_config(args) -> PipelineConfig:
    return PipelineConfig(
        hospital=args.hospital,
        source_script=Path(args.script),
        annotations_path=Path(args.annotations),
        output_root=Path(args.output_root),
        auth_mode=args.auth_mode,
        storage_state=Path(args.storage_state) if args.storage_state else None,
        model=args.model,
        retry_count=args.retry,
        capture_timeout_s=args.capture_timeout,
        auth_timeout_s=args.auth_timeout,
    )


def _stdin_command_reader(controller: PipelineController) -> None:
    """Drain JSONL commands (``continue_after_auth`` / ``cancel``) from stdin."""

    def reader() -> None:
        for raw in sys.stdin:
            line = raw.strip()
            if not line:
                continue
            try:
                command = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(command, dict):
                continue
            try:
                controller.send_command(command)
            except Exception:  # noqa: BLE001 - reader must never die
                pass

    threading.Thread(
        target=reader, name="orchestrator-stdin-commands", daemon=True
    ).start()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run or resume the adapter+replica pipeline"
    )
    parser.add_argument("--script", required=True)
    parser.add_argument("--annotations", required=True)
    parser.add_argument("--hospital", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--auth-mode", choices=["scripted", "interactive", "storage-state"],
                        default="scripted")
    parser.add_argument("--storage-state", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--retry", type=int, default=3)
    parser.add_argument("--capture-timeout", type=int, default=900)
    parser.add_argument("--auth-timeout", type=int, default=300)
    parser.add_argument(
        "--operation",
        choices=["full", "adapter-only", "replica-build", "offline-validation"],
        default="full",
    )
    parser.add_argument("--run-id", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.operation != "full" and not args.run_id:
        parser.error("non-full operation requires --run-id")
    if args.operation == "full" and args.run_id:
        parser.error("full operation does not take --run-id")

    config = _cli_config(args)
    emit = lambda event: print(json.dumps(event, ensure_ascii=False), flush=True)
    try:
        controller = PipelineController(
            config,
            emit=emit,
            run_id=args.run_id,
            operation=args.operation,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    _stdin_command_reader(controller)
    try:
        result = controller.run()
    except Exception as exc:  # noqa: BLE001 - top-level diagnostic
        print(f"pipeline failed: {exc}", file=sys.stderr)
        return 1
    print(f"status: {result.status.value}", file=sys.stderr)
    print(f"run_id: {result.run_id}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
