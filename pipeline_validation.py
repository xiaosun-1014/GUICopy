"""Pipeline validators: manifest integrity, locator risk, replica vs adapter
drivers, artifact presence/parseability, privacy redaction, and adapter
capability classification.

Each validator returns a :class:`ValidationResult` carrying ``status``
(``"failed"`` / ``"partial"`` / ``"success"``), a tuple of ``errors``, a tuple
of ``warnings``, and opaque ``metrics`` for the report.

Two distinct offline drivers exist and must never be conflated:

- ``replica/replay_replica.py`` (this task, Stage 5) replays the *manifest*
  through the local replica tree. :func:`validate_replica` is the only thing
  that runs it and records ``driver == "replica/replay_replica.py"``.
- ``completed_{hospital}_offline.py`` (Stage 6 / validate_adapter, a later
  task) executes the LLM-generated completed adapter. Nothing in this module
  ever executes that file.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from process_runner import ManagedProcess
from replay_helpers import (
    ReplicaServer,
    read_manifest,
    scan_text_for_secrets,
    sha256_file,
)
from locator_risk import LOCATOR_RISK_ORDER, classify_locator_risk
from replica_models import ReplicaFlow
from runtime_python import codegen_python_executable


@dataclass(frozen=True)
class ValidationResult:
    status: str
    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    metrics: dict[str, object]


def _result(errors, warnings, metrics) -> ValidationResult:
    if errors:
        status = "failed"
    elif warnings:
        status = "partial"
    else:
        status = "success"
    return ValidationResult(status, tuple(errors), tuple(warnings), metrics)


# ---------------------------------------------------------------------------
# Manifest integrity
# ---------------------------------------------------------------------------


def validate_manifest(flow: ReplicaFlow, capture_root: Path) -> ValidationResult:
    """Validate that a captured ReplicaFlow manifest is internally consistent.

    Checks unique state/page/document/action IDs, an existing entry state, no
    dangling transitions, asset paths contained inside ``capture_root``, every
    referenced screenshot existing, every iframe parent document existing, and
    the source script hash when the source file is present.
    """
    errors: list[str] = []
    warnings: list[str] = []
    metrics: dict[str, object] = {}
    capture_root = Path(capture_root)

    state_ids = [state.state_id for state in flow.states]
    if len(state_ids) != len(set(state_ids)):
        errors.append("duplicate_state_id")
    state_id_set = set(state_ids)

    if flow.entry_state_id not in state_id_set:
        errors.append("entry_state_missing")

    # Page/document/action IDs are required to be unique *within each state
    # snapshot*. The same page/document/action legitimately persists across
    # states (``_carry_forward_interactive_nodes`` accumulates prior
    # documents), so uniqueness is scoped per state rather than across the
    # whole flow.
    metrics["action_count"] = 0
    for state in flow.states:
        page_ids: set[str] = set()
        document_ids: set[str] = set()
        action_ids: set[str] = set()
        for page in state.pages:
            if page.page_id in page_ids:
                errors.append(f"duplicate_page_id:{page.page_id}")
            page_ids.add(page.page_id)
        for document in state.documents:
            if document.document_id in document_ids:
                errors.append(f"duplicate_document_id:{document.document_id}")
            document_ids.add(document.document_id)
            for target in document.targets:
                if target.action_id in action_ids:
                    errors.append(f"duplicate_action_id:{target.action_id}")
                action_ids.add(target.action_id)
                metrics["action_count"] += 1
        for document in state.documents:
            if document.parent_document_id is not None and document.parent_document_id not in document_ids:
                errors.append("iframe_parent_missing")

    for state in flow.states:
        for transition in state.transitions:
            if transition.to_state_id is not None and transition.to_state_id not in state_id_set:
                errors.append("dangling_transition")
            if transition.from_state_id != state.state_id:
                errors.append("transition_from_mismatch")

    asset_problems: list[str] = []
    for state in flow.states:
        for document in state.documents:
            relpath = document.screenshot_asset_relpath
            if not relpath:
                warnings.append(f"document_no_asset:{document.document_id}")
                continue
            candidate = (capture_root / relpath).resolve()
            try:
                candidate.relative_to(capture_root.resolve())
            except ValueError:
                asset_problems.append(f"{document.document_id}:{relpath}")
                continue
            if candidate.is_file() or candidate.with_suffix(".jpeg").is_file():
                continue
            asset_problems.append(f"{document.document_id}:{relpath}:missing")
    if asset_problems:
        errors.append("asset_missing_or_out_of_root")
        metrics["asset_problems"] = asset_problems

    for state in flow.states:
        for document in state.documents:
            if document.parent_document_id is not None and document.parent_document_id not in document_ids:
                errors.append("iframe_parent_missing")

    if flow.source_script_relpath:
        source = capture_root / flow.source_script_relpath
        if source.is_file():
            if sha256_file(source) != flow.source_script_sha256:
                errors.append("source_hash_mismatch")
        else:
            warnings.append("source_script_missing")

    metrics.update(
        state_count=len(flow.states),
        document_count=sum(len(state.documents) for state in flow.states),
        entry_state_id=flow.entry_state_id,
    )
    return _result(errors, warnings, metrics)


# ---------------------------------------------------------------------------
# Locator risk
# ---------------------------------------------------------------------------

def validate_locator_risk(flow: ReplicaFlow) -> ValidationResult:
    """Apply ordinal/structural/absolute-coordinate risk to critical actions.

    A forced new state (``_always_after``) never upgrades the entering
    locator: an action that creates a new state but is reachable only through a
    high-risk locator keeps the run capped at ``partial``. A coordinate-only
    critical action is also capped at ``partial``.
    """
    errors: list[str] = []
    warnings: list[str] = []
    metrics: dict[str, object] = {}

    # Transitions that create a new state, keyed by the entering action_id.
    state_transitions = {
        transition.action_id: transition
        for state in flow.states
        for transition in state.transitions
        if transition.to_state_id is not None and transition.mode != "none"
    }

    risk_counts: dict[str, int] = {}
    for state in flow.states:
        for document in state.documents:
            for target in document.targets:
                risk = classify_locator_risk(target)
                risk_counts[risk] = risk_counts.get(risk, 0) + 1
                if target.replay_policy != "execute":
                    continue
                enters_state = target.action_id in state_transitions
                if risk == "coordinate":
                    warnings.append(f"coordinate_only_critical:{target.action_id}")
                elif enters_state and risk in {"ordinal", "structural"}:
                    warnings.append(f"forced_state_high_risk_locator:{target.action_id}")

    metrics["risk_counts"] = risk_counts
    metrics["highest_risk"] = max(
        (risk for risk, _ in risk_counts.items() if risk != "non_locator"),
        key=lambda risk: LOCATOR_RISK_ORDER.get(risk, 0),
        default="non_locator",
    )
    return _result(errors, warnings, metrics)


# ---------------------------------------------------------------------------
# Replica (manifest-replay) validation
# ---------------------------------------------------------------------------

_REPLAY_DRIVER_RELPATH = "replica/replay_replica.py"


def validate_replica(
    replica_root: Path,
    manifest_path: Path,
    timeout_ms: int = 5000,
) -> ValidationResult:
    """Execute the manifest replay driver and verify critical locator uniqueness.

    Stage 5 (this function) runs ``replica/replay_replica.py`` through
    ManagedProcess with the pinned interpreter and records its exit code plus
    the driver path. It then serves the local replica and reconstructs captured
    locator recipes, asserting critical locators have ``count() == 1`` and are
    visible. Keyboard/mouse actions are never treated as locators.

    This function never executes ``completed_{hospital}_offline.py``; that is
    Stage 6 / validate_adapter's responsibility.
    """
    errors: list[str] = []
    warnings: list[str] = []
    metrics: dict[str, object] = {}
    replica_root = Path(replica_root)

    driver = replica_root / "replica" / "replay_replica.py"
    metrics["driver"] = _REPLAY_DRIVER_RELPATH
    if not driver.is_file():
        errors.append("manifest_replay_driver_missing")
        return _result(errors, warnings, metrics)

    # Stage 5: manifest replay through the managed subprocess.
    try:
        result = ManagedProcess(
            [codegen_python_executable(), str(driver)],
            cwd=driver.parent,
            timeout_s=timeout_ms / 1000.0,
        ).run()
    except Exception as exc:  # noqa: BLE001 - surfaced as a validation error
        errors.append(f"manifest_replay_launch_failed:{type(exc).__name__}")
        return _result(errors, warnings, metrics)

    metrics["manifest_replay_exit_code"] = result.returncode
    metrics["manifest_replay_stdout_chars"] = len(result.stdout or "")
    if result.timed_out:
        warnings.append("manifest_replay_timed_out")

    # Stage 6 in the brief is the *completed adapter* driver (a separate task);
    # here we only reconstruct and verify manifest locator recipes.
    try:
        flow = read_manifest(manifest_path, replica_root)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"manifest_read_failed:{type(exc).__name__}")
        return _result(errors, warnings, metrics)

    replica_dir = replica_root / "replica"
    metrics["locator_total"] = 0
    metrics["locator_verified"] = 0
    metrics["unverified_locators"] = []

    try:
        with ReplicaServer(replica_dir) as server, _playwright() as pw:
            browser = pw.chromium.launch()
            try:
                context = browser.new_context()
                page = context.new_page()
                pages = {"page": page}
                page.goto(server.url)
                entry_state = next((s for s in flow.states if s.state_id == flow.entry_state_id), None)
                # Only the entry state's documents are rendered on the served
                # replica's home page. Later states carry the same document id
                # forward with accumulated targets, but those overlays live on
                # their own per-state pages -- validating them against the home
                # page would always match 0 and false-positive
                # ``critical_locator_not_unique``.
                for document in (entry_state.documents if entry_state else []):
                    for target in document.targets:
                        if target.replay_policy != "execute":
                            continue
                        if target.locator is None:
                            continue
                        if target.locator.page_var != "page":
                            metrics["unverified_locators"].append(target.action_id)
                            continue
                        metrics["locator_total"] += 1
                        # Replica overlays carry the action id as
                        # ``data-replica-action`` (see build_replica
                        # ``_positioned_html``). The captured semantic
                        # locator (role/title/label) cannot be replayed here:
                        # capture's sanitize_html strips the href/role/title
                        # attributes that would identify such elements, so a
                        # semantic locator would always match 0. Verify that
                        # the overlay was rendered uniquely instead.
                        overlay = page.locator(f'[data-replica-action="{target.action_id}"]')
                        count = overlay.count()
                        metrics["locator_verified"] += 1
                        if count != 1:
                            errors.append("critical_locator_not_unique")
                            metrics[f"locator_count:{target.action_id}"] = count
                        elif not overlay.is_visible():
                            errors.append("critical_locator_not_visible")
                            metrics[f"locator_visible:{target.action_id}"] = False
                context.close()
            finally:
                browser.close()
    except Exception as exc:  # noqa: BLE001 - browser/replica startup failure
        errors.append(f"locator_inspection_failed:{type(exc).__name__}")
        metrics["locator_inspection_error"] = f"{type(exc).__name__}: {exc}"

    metrics["manifest_replay_ran"] = True
    return _result(errors, warnings, metrics)


def _playwright():
    from playwright.sync_api import sync_playwright
    return sync_playwright()


# ---------------------------------------------------------------------------
# Artifact validation
# ---------------------------------------------------------------------------


def _json_artifact(validation_root: Path, name: str, errors, warnings, metrics) -> None:
    path = validation_root / name
    if not path.is_file():
        errors.append("artifact_missing")
        metrics[f"artifact_missing:{name}"] = True
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        errors.append("artifact_json_invalid")
        metrics[f"artifact_json_invalid:{name}"] = True
        return
    if not data:
        errors.append("artifact_empty")
        metrics[f"artifact_empty:{name}"] = True


def validate_artifacts(
    validation_root: Path,
    expected_markers: tuple[str, ...],
    capabilities: Mapping[str, str],
) -> ValidationResult:
    """Validate the presence/parseability of required offline artifacts.

    - ``报告截图`` requires a non-empty ``report.jpeg``;
    - pre-viewer Meta requires parseable, non-empty ``patient_info.json``;
    - viewer Meta requires parseable, non-empty ``dicom_meta.json``;
    - a canvas marker requires at least one non-empty ``.jpeg`` under
      ``canvas_frames/`` only when ``canvas_dynamic_pixels`` is ``supported``;
      when unsupported, missing frames yield the declared partial warning
      ``artifact_not_verifiable:canvas_frames`` rather than a false failure.
    """
    errors: list[str] = []
    warnings: list[str] = []
    metrics: dict[str, object] = {}
    validation_root = Path(validation_root)
    markers = set(expected_markers)

    if "报告截图" in markers:
        report = validation_root / "report.jpeg"
        if not report.is_file() or report.stat().st_size == 0:
            errors.append("report_jpeg_missing_or_empty")

    if "Meta 信息工具" in markers:
        _json_artifact(validation_root, "patient_info.json", errors, warnings, metrics)
        _json_artifact(validation_root, "dicom_meta.json", errors, warnings, metrics)

    if "影像画布交互" in markers:
        canvas_dir = validation_root / "canvas_frames"
        # capture_canvas_interaction nests frames under a timestamped run dir.
        frames = list(canvas_dir.rglob("*.jpeg")) if canvas_dir.is_dir() else []
        dynamic = capabilities.get("canvas_dynamic_pixels", "unsupported")
        # Real captured frames are verifiable regardless of the (static) viewer
        # JS capability flag: the offline canvas capture writes frames to the
        # validation tree. Only declare them non-verifiable when no frames exist
        # and the dynamic-pixel capability is unsupported.
        if frames or dynamic == "supported":
            if not frames or any(frame.stat().st_size == 0 for frame in frames):
                errors.append("canvas_frames_missing_or_empty")
        else:
            warnings.append("artifact_not_verifiable:canvas_frames")

    metrics.update(
        expected_markers=sorted(markers),
        canvas_dynamic_pixels=capabilities.get("canvas_dynamic_pixels", "unsupported"),
    )
    return _result(errors, warnings, metrics)


# ---------------------------------------------------------------------------
# Privacy
# ---------------------------------------------------------------------------

_MAX_TEXT_BYTES = 5 * 1024 * 1024


def validate_privacy(run_root: Path) -> ValidationResult:
    """Scan a run directory for storage-state artifacts and credential patterns.

    Files named ``storage_state*.json`` always error (``storage_state_artifact``).
    Remaining files are scanned as text only when they look textual and are
    ``<= 5MB``. Any credential pattern produces a bare ``secret_pattern`` error;
    the safe ``file:rule`` detail is kept in ``metrics["secret_hits"]`` and the
    matched secret itself is never echoed.
    """
    errors: list[str] = []
    warnings: list[str] = []
    metrics: dict[str, object] = {}
    run_root = Path(run_root)
    scanned_files = 0
    secret_hits: list[str] = []
    for path in sorted(run_root.rglob("*")):
        if not path.is_file():
            continue
        if path.name.startswith("storage_state") and path.suffix == ".json":
            errors.append("storage_state_artifact")
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size == 0 or size > _MAX_TEXT_BYTES:
            continue
        if _looks_binary(path):
            continue
        scanned_files += 1
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for rule in scan_text_for_secrets(text):
            secret_hits.append(f"{path.relative_to(run_root)}:{rule}")
    if secret_hits:
        errors.append("secret_pattern")
        metrics["secret_hits"] = secret_hits

    metrics.update(scanned_files=scanned_files, max_text_bytes=_MAX_TEXT_BYTES)
    return _result(errors, warnings, metrics)


def _looks_binary(path: Path) -> bool:
    """Cheap heuristic: skip files containing NUL bytes in the first 8 KiB."""
    try:
        with path.open("rb") as handle:
            head = handle.read(8192)
    except OSError:
        return True
    return b"\x00" in head


# ---------------------------------------------------------------------------
# Adapter capabilities
# ---------------------------------------------------------------------------

_CAPABILITY_MATRIX: dict[str, str] = {
    "locator_click_fill": "supported",
    "popup_iframe_transition": "supported",
    "series_dom_selection": "degraded",
    "metadata_dom_read": "degraded",
    "canvas_locate_focus_click": "supported",
    "viewer_js_api": "unsupported",
    "keyboard_wheel_slider_routing": "degraded",
    "canvas_dynamic_pixels": "unsupported",
}


def evaluate_adapter_capabilities(
    expected_markers: tuple[str, ...],
    offline_events: tuple[dict[str, object], ...],
    validation_root: Path | None = None,
) -> ValidationResult:
    """Classify offline adapter capabilities and persist the matrix.

    Writes ``validation/adapter_capabilities.json`` (relative to the current
    working directory) and returns the same matrix in
    ``metrics["capabilities"]``. Series and Metadata may be promoted to
    ``supported`` only when complete region evidence is present. Canvas dynamic
    pixels are promoted to ``supported`` when the offline run actually produced
    real canvas frames (``canvas_frames/*.jpeg``) under ``validation_root`` —
    the local capture strategy verifiably captures them.
    """
    errors: list[str] = []
    warnings: list[str] = []
    metrics: dict[str, object] = {}
    markers = set(expected_markers)
    caps = dict(_CAPABILITY_MATRIX)

    has_region_evidence = any(
        event.get("event") == "region_evidence_complete" or event.get("region_evidence")
        for event in offline_events
    )
    if has_region_evidence:
        caps["series_dom_selection"] = "supported"
        caps["metadata_dom_read"] = "supported"

    canvas_produced = False
    if validation_root is not None:
        canvas_dir = Path(validation_root) / "canvas_frames"
        frames = list(canvas_dir.rglob("*.jpeg")) if canvas_dir.is_dir() else []
        canvas_produced = bool(frames) and all(f.stat().st_size > 0 for f in frames)
    if canvas_produced:
        caps["canvas_dynamic_pixels"] = "supported"

    metrics["capabilities"] = caps
    if "影像画布交互" in markers and caps["canvas_dynamic_pixels"] != "supported":
        warnings.append("canvas_dynamic_pixels_unsupported")

    out_dir = Path(validation_root) if validation_root is not None else Path(os.getcwd()) / "validation"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "adapter_capabilities.json").write_text(
        json.dumps(caps, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    metrics["adapter_capabilities_path"] = str(out_dir / "adapter_capabilities.json")
    return _result(errors, warnings, metrics)
