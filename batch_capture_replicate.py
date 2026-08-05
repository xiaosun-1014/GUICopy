"""Isolated command-line entrypoint for replica build stages."""

from __future__ import annotations

import argparse
import ast
import json
import os
import shutil
import subprocess
import sys
import time
import queue
import threading
from datetime import datetime, timezone
from dataclasses import asdict, replace
from collections.abc import Callable
from pathlib import Path

from build_replica import build_replica
from capture_snapshot import capture_interaction_region, capture_locator_snapshot, capture_marker_interaction_region, capture_page_topology, capture_selector_closure, compute_image_diff, decide_state, marker_region_type
from process_runner import ManagedProcess
from replay_helpers import read_manifest, sha256_file, write_manifest
from rewrite_script import ActionPlan, parse_action_plan
from replica_models import CaptureTimingProfile, DomNodeSnapshot, Rect, ReplicaDocument, ReplicaFlow, ReplicaPage, ReplicaState, ReplicaTransition, StateDiffProfile, StateEvidence
from runtime_python import codegen_python_executable


def wait_for_pre_action_state(page: object, marker_label: str) -> None:
    """Wait for asynchronous report data that must settle before selecting a series."""
    if marker_label != "序列选择":
        return
    try:
        report = page.locator("#reportContainer")
        report.wait_for(state="visible", timeout=30000)
        report.locator(".report-footer").wait_for(state="visible", timeout=30000)
    except Exception:
        pass


def wait_for_post_action_state(
    page: object,
    marker_label: str,
    timeout_s: float = 10.0,
    stable_s: float = 1.0,
) -> bool:
    """Wait for known asynchronous UI transitions before capturing the next state."""
    if marker_label != "序列选择":
        return True
    report = page.locator("#reportContainer")
    more_tool = page.locator("a.tool.tool-more")
    try:
        has_report = report.count() > 0
        has_more_tool = more_tool.count() > 0
    except Exception:
        return False
    if not has_report and not has_more_tool:
        return True
    deadline = time.monotonic() + timeout_s
    stable_since: float | None = None
    while time.monotonic() < deadline:
        try:
            report_hidden = not has_report or not report.is_visible()
            more_ready = (
                not has_more_tool
                or (
                    more_tool.is_visible()
                    and "disabled" not in (more_tool.get_attribute("class") or "").split()
                )
            )
        except Exception:
            stable_since = None
            page.wait_for_timeout(100)
            continue
        if report_hidden and more_ready:
            stable_since = stable_since or time.monotonic()
            if time.monotonic() - stable_since >= stable_s:
                return True
        else:
            stable_since = None
        page.wait_for_timeout(100)
    return False


def ensure_post_action_state(
    page: object,
    marker_label: str,
    locator_factory: object | None = None,
    timeout_s: float = 10.0,
    stable_s: float = 1.0,
) -> None:
    """Retry a series transition once when the recorded dblclick did not change UI state."""
    if wait_for_post_action_state(page, marker_label, timeout_s, stable_s):
        return
    if marker_label == "序列选择" and callable(locator_factory):
        locator_factory().dblclick()
        if wait_for_post_action_state(page, marker_label, timeout_s, stable_s):
            return
    raise TimeoutError(f"post-action state did not stabilize for marker: {marker_label}")


class LiveCaptureSession:
    """Persist only marked-action page/frame snapshots during a live replay."""

    def __init__(self, output_root: str | Path) -> None:
        self.output_root = Path(output_root)

    def _capture(
        self,
        action_id: str,
        phase: str,
        page: object,
        locator_factory: object | None = None,
        marker_label: str = "",
    ) -> None:
        capture_root = self.output_root / "snapshots" / action_id / phase
        if phase == "after":
            page.wait_for_timeout(150)
        context_pages = list(getattr(getattr(page, "context", None), "pages", []) or [page])
        named_pages = [("page" if index == 0 else f"page{index}", candidate) for index, candidate in enumerate(context_pages)]
        pages, documents = capture_page_topology(named_pages, capture_root)
        target_locator = locator_factory() if callable(locator_factory) else None
        if target_locator is not None:
            try:
                if target_locator.count() == 0:
                    target_locator = None
            except Exception:
                target_locator = None
        if documents and marker_label:
            target_document = documents[0]
            active_page_id = next((entry.page_id for entry, (_, candidate) in zip(pages, named_pages) if candidate is page), documents[0].page_id)
            if target_locator is not None:
                frame_owner = target_locator.evaluate("""element => {
                    const owner = window.frameElement;
                    return owner ? {id: owner.id || null, name: owner.name || null} : null;
                }""")
                if frame_owner:
                    target_document = next(
                        (document for document in documents if document.frame_id == frame_owner.get("id") or document.frame_name == frame_owner.get("name")),
                        target_document,
                    )
                if target_document is documents[0]:
                    frame_documents = [document for document in documents if document.page_id == active_page_id and document.parent_document_id is not None]
                    if len(frame_documents) == 1:
                        target_document = frame_documents[0]
            if target_document is documents[0]:
                region = capture_marker_interaction_region(page, marker_label, target_document.document_id, target_locator)
            else:
                region = capture_interaction_region(target_locator.locator("xpath=.."), marker_region_type(marker_label), target_document.document_id)
            target_document.regions.append(region)
        (capture_root / "topology.json").write_text(
            json.dumps({"pages": [asdict(item) for item in pages], "documents": [asdict(item) for item in documents]}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        if target_locator is not None:
            try:
                target = capture_locator_snapshot(target_locator)
                (capture_root / "target.json").write_text(json.dumps(asdict(target), ensure_ascii=False, indent=2), encoding="utf-8")
                closure = capture_selector_closure(target_locator, action_id)
                (capture_root / "selector_closure.json").write_text(json.dumps(asdict(closure), ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                pass

    def before(self, action_id: str, page: object, locator_factory: object | None = None, marker_label: str = "") -> None:
        wait_for_pre_action_state(page, marker_label)
        self._capture(action_id, "before", page, locator_factory, marker_label)

    def after(self, action_id: str, page: object, locator_factory: object | None = None, marker_label: str = "") -> None:
        ensure_post_action_state(page, marker_label, locator_factory)
        self._capture(action_id, "after", page, locator_factory, marker_label)


_LIVE_SESSION: LiveCaptureSession | None = None
_INTERACTIVE_AUTH_DONE = False


def configure_live_capture(session: LiveCaptureSession | None) -> None:
    global _LIVE_SESSION
    _LIVE_SESSION = session


def capture_hook_before(action_id: str, page: object, locator_factory: object | None = None, marker_label: str = "") -> None:
    """Runtime hook imported by instrumented scripts; capture wiring is injected by live mode."""
    global _INTERACTIVE_AUTH_DONE
    if os.environ.get("REPLICA_INTERACTIVE_AUTH") == "1" and not _INTERACTIVE_AUTH_DONE:
        await_interactive_auth(sys.stdin, lambda event: print(json.dumps(event, ensure_ascii=False), flush=True))
        _INTERACTIVE_AUTH_DONE = True
    session = _LIVE_SESSION or _session_from_environment()
    if session:
        try:
            session.before(action_id, page, locator_factory, marker_label)
        except Exception as error:
            capture_hook_failed(action_id, error)


def capture_hook_after(action_id: str, page: object, locator_factory: object | None = None, marker_label: str = "") -> None:
    """Runtime hook imported by instrumented scripts; capture wiring is injected by live mode."""
    session = _LIVE_SESSION or _session_from_environment()
    if session:
        try:
            session.after(action_id, page, locator_factory, marker_label)
        except Exception as error:
            capture_hook_failed(action_id, error)


def capture_hook_failed(action_id: str, error: BaseException) -> None:
    """Record an action-level failure without aborting later independent markers."""
    print(json.dumps({"event": "action_failed", "action_id": action_id, "error": type(error).__name__}), flush=True)


def _session_from_environment() -> LiveCaptureSession | None:
    output = os.environ.get("REPLICA_CAPTURE_OUTPUT")
    if not output:
        return None
    global _LIVE_SESSION
    _LIVE_SESSION = LiveCaptureSession(output)
    return _LIVE_SESSION


def instrument_marked_actions(source: str, use_storage_state: bool = False, interactive_auth: bool = False) -> str:
    """Insert capture hooks around marked Playwright action statements without executing source."""
    plan = parse_action_plan(source)
    actions = {}
    for group in plan.marker_groups:
        for action in group.actions:
            line = action.action_args.get("_source_line")
            if isinstance(line, int):
                actions.setdefault(line, []).append((action.action_id, action.locator.page_var if action.locator else "page", action.locator.source_expression if action.locator else None, group.marker_label))
    if use_storage_state:
        source = source.replace("browser.new_context()", "browser.new_context(storage_state=os.environ['REPLICA_STORAGE_STATE'])")
    if interactive_auth:
        source = source.replace("chromium.launch()", "chromium.launch(headless=False)")
        source = source.replace("chromium.launch(headless=True)", "chromium.launch(headless=False)")
    source_lines = source.splitlines()
    output = ["import os" if use_storage_state else "", "from batch_capture_replicate import capture_hook_after, capture_hook_before, capture_hook_failed"]
    for index, line in enumerate(source_lines, start=1):
        indent = line[: len(line) - len(line.lstrip())]
        line_actions = actions.get(index, [])
        for action_id, page_var, locator_source, marker_label in line_actions:
            factory = f", lambda: {locator_source}" if locator_source else ""
            output.append(f'{indent}capture_hook_before("{action_id}", {page_var}{factory}, {marker_label!r})')
        if not line_actions:
            output.append(line)
            continue
        output.append(f"{indent}try:")
        output.append(f"{indent}    {line.lstrip()}")
        output.append(f"{indent}except Exception as error:")
        for action_id, _, _, _ in line_actions:
            output.append(f'{indent}    capture_hook_failed("{action_id}", error)')
        output.append(f"{indent}else:")
        for action_id, page_var, locator_source, marker_label in line_actions:
            factory = f", lambda: {locator_source}" if locator_source else ""
            output.append(f'{indent}    capture_hook_after("{action_id}", {page_var}{factory}, {marker_label!r})')
    instrumented = "\n".join(line for line in output if line) + "\n"
    ast.parse(instrumented)
    return instrumented


def run_live_capture(
    script_path: str | Path,
    output_root: str | Path,
    timeout_s: int = 900,
    storage_state: str | Path | None = None,
    interactive_auth: bool = False,
    emit: Callable[[dict[str, str]], None] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Execute one instrumented recording script in a fresh process with capture hooks enabled."""
    script_path = Path(script_path)
    output_root = Path(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    snapshot_root = output_root / "snapshots"
    if snapshot_root.exists():
        shutil.rmtree(snapshot_root)
    instrumented_path = output_root / "instrumented_replay.py"
    instrumented_path.write_text(
        instrument_marked_actions(script_path.read_text(encoding="utf-8"), storage_state is not None, interactive_auth),
        encoding="utf-8",
    )
    environment = os.environ.copy()
    project_root = str(Path(__file__).resolve().parent)
    environment["PYTHONPATH"] = project_root + os.pathsep + environment.get("PYTHONPATH", "")
    environment["REPLICA_CAPTURE_OUTPUT"] = str(output_root)
    if storage_state is not None:
        environment["REPLICA_STORAGE_STATE"] = str(Path(storage_state).resolve())
    if interactive_auth:
        environment["REPLICA_INTERACTIVE_AUTH"] = "1"
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


def _load_snapshot_state(capture_root: Path, action_id: str, phase: str) -> tuple[list[ReplicaPage], list[ReplicaDocument]]:
    phase_root = capture_root / "snapshots" / action_id / phase
    payload = json.loads((phase_root / "topology.json").read_text(encoding="utf-8"))
    pages = [ReplicaPage(**item) for item in payload["pages"]]
    documents = []
    for item in payload["documents"]:
        item["screenshot_asset_relpath"] = str((phase_root / item["screenshot_asset_relpath"]).relative_to(capture_root)).replace("\\", "/")
        documents.append(ReplicaDocument.from_dict(item))
    return pages, documents


def _load_target_snapshot(capture_root: Path, action_id: str) -> DomNodeSnapshot | None:
    paths = [
        capture_root / "snapshots" / action_id / phase / "target.json"
        for phase in ("before", "after")
    ]
    path = next((candidate for candidate in paths if candidate.exists()), None)
    if path is None:
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["rect"] = Rect(**payload["rect"])
    return DomNodeSnapshot(**payload)


def _load_selector_closure(capture_root: Path, action_id: str):
    paths = [
        capture_root / "snapshots" / action_id / phase / "selector_closure.json"
        for phase in ("before", "after")
    ]
    path = next((candidate for candidate in paths if candidate.exists()), None)
    if path is None:
        return None
    from replica_models import SelectorClosure
    return SelectorClosure(**json.loads(path.read_text(encoding="utf-8")))


def _topology_changed(before_pages: list[ReplicaPage], before_documents: list[ReplicaDocument], after_pages: list[ReplicaPage], after_documents: list[ReplicaDocument]) -> bool:
    before = {(item.page_id, item.parent_document_id, item.frame_id, item.frame_name) for item in before_documents}
    after = {(item.page_id, item.parent_document_id, item.frame_id, item.frame_name) for item in after_documents}
    return before != after or len(before_pages) != len(after_pages)


def _region_dom_changed(before_documents: list[ReplicaDocument], after_documents: list[ReplicaDocument]) -> bool:
    """Compare only captured marker regions, not volatile DOM outside the interaction area."""
    def fingerprint(documents: list[ReplicaDocument]) -> dict[tuple[str, str], tuple[object, ...]]:
        result: dict[tuple[str, str], tuple[object, ...]] = {}
        for document in documents:
            for region in document.regions:
                members = tuple(
                    (member.member_id, member.semantic_type, member.dom.outer_html, tuple(sorted(member.dom.attributes.items())), member.dom.text)
                    for member in region.members
                )
                result[(document.document_id, region.region_type)] = (
                    region.root.outer_html,
                    tuple(sorted(region.root.attributes.items())),
                    region.root.text,
                    members,
                )
        return result
    return fingerprint(before_documents) != fingerprint(after_documents)


def _carry_forward_interactive_nodes(before_documents: list[ReplicaDocument], after_documents: list[ReplicaDocument]) -> None:
    """Carry previously captured overlays into the next visual state for stable locators."""
    previous = {document.document_id: document for document in before_documents}
    for document in after_documents:
        prior = previous.get(document.document_id)
        if prior is None:
            continue
        target_ids = {target.action_id for target in document.targets}
        document.targets = [*(target for target in prior.targets if target.action_id not in target_ids), *document.targets]
        region_ids = {region.region_id for region in document.regions}
        document.regions = [*(region for region in prior.regions if region.region_id not in region_ids), *document.regions]


def _diff_evidence(before_documents: list[ReplicaDocument], after_documents: list[ReplicaDocument], capture_root: Path) -> StateEvidence:
    if not before_documents or not after_documents:
        return StateEvidence(False, False, False, False, 0, 0, 0, 0, "no_documents")
    before = before_documents[0]
    after = after_documents[0]
    try:
        metrics = compute_image_diff(
            (capture_root / before.screenshot_asset_relpath).read_bytes(),
            (capture_root / after.screenshot_asset_relpath).read_bytes(),
            StateDiffProfile(),
        )
        return StateEvidence(False, False, False, False, metrics.changed_pixel_ratio, metrics.mean_abs_diff, metrics.changed_pixel_ratio, 0, "visual_diff")
    except Exception:
        return StateEvidence(False, False, False, False, 0, 0, 0, 0, "visual_diff_unavailable")


def _always_after(marker_label: str, action_type: str) -> bool:
    if action_type == "fill":
        return False
    return marker_label in {"序列布局切换", "序列选择", "Meta 信息工具"} or (marker_label == "窗宽窗位 WL/WW" and action_type in {"click", "press"})


def _has_snapshot_pair(capture_root: Path, action_id: str) -> bool:
    snapshots = capture_root / "snapshots" / action_id
    return all((snapshots / phase / "topology.json").is_file() for phase in ("before", "after"))


def build_flow_from_snapshots(script_path: str | Path, capture_root: str | Path, annotations_payload: dict[str, object] | None = None) -> ReplicaFlow:
    """Build a sequential ReplicaFlow from marked-action snapshot pairs.

    When ``annotations_payload`` (the validated GUI annotations dict) is supplied,
    the GUI UUIDs are matched onto marker groups/actions by source line + normalized
    label before building, so ``ActionTarget.marker_id`` in the manifest carries the
    stable GUI UUID instead of the regenerated ``m_{index}`` id. Without a payload
    (the hash-only/no-annotations path) behavior is unchanged.
    """
    script_path = Path(script_path)
    capture_root = Path(capture_root)
    plan = parse_action_plan(script_path.read_text(encoding="utf-8"))
    if annotations_payload is not None:
        merge_annotation_uuids(plan, annotations_payload)
    all_actions = [action for group in plan.marker_groups for action in group.actions]
    marker_labels = {group.marker_id: group.marker_label for group in plan.marker_groups}
    popup_targets = {action_id: expectation.result_page_var for expectation in plan.popup_expectations for action_id in expectation.body_action_ids}
    if not all_actions:
        raise ValueError("recording contains no marked actions to capture")
    skipped_actions = [action.action_id for action in all_actions if not _has_snapshot_pair(capture_root, action.action_id)]
    actions = [action for action in all_actions if action.action_id not in skipped_actions]
    if not actions:
        raise ValueError("recording contains no successfully captured marked actions")
    first_pages, first_documents = _load_snapshot_state(capture_root, actions[0].action_id, "before")
    states = [ReplicaState("s_000", 0, "", "page", first_pages, first_documents, [], StateEvidence(False, False, False, False, 0, 0, 0, 0, "entry_snapshot"))]
    for index, action in enumerate(actions, start=1):
        target = _load_target_snapshot(capture_root, action.action_id)
        closure = _load_selector_closure(capture_root, action.action_id)
        if target and states[-1].documents:
            document = states[-1].documents[-1] if action.locator and action.locator.frame_chain else states[-1].documents[0]
            document.targets.append(replace(action, dom=target, selector_closure=closure, document_id=document.document_id, transition_id=f"t_{action.action_id}"))
        pages, documents = _load_snapshot_state(capture_root, action.action_id, "after")
        evidence = _diff_evidence(states[-1].documents, documents, capture_root)
        evidence.topology_changed = _topology_changed(states[-1].pages, states[-1].documents, pages, documents)
        evidence.popup_changed = len(states[-1].pages) != len(pages)
        evidence.region_dom_changed = _region_dom_changed(states[-1].documents, documents)
        should_create, reason = decide_state(evidence, StateDiffProfile(), _always_after(marker_labels[action.marker_id], action.action_type))
        evidence.decision_reason = reason
        state_id = f"s_{len(states):03d}" if should_create else None
        source_page = action.locator.page_var if action.locator else "page"
        target_page = popup_targets.get(action.action_id, source_page)
        mode = "popup" if action.action_id in popup_targets and should_create else ("same_page" if should_create else "none")
        states[-1].transitions.append(ReplicaTransition(f"t_{action.action_id}", action.action_id, states[-1].state_id, state_id, source_page, target_page, mode))
        if should_create:
            _carry_forward_interactive_nodes(states[-1].documents, documents)
            states.append(ReplicaState(state_id, len(states), "", "page", pages, documents, [], evidence))
    viewport = first_pages[0].entry_document_id and first_documents[0].viewport if first_documents else {"width": 0, "height": 0}
    warnings = [f"action_capture_failed:{action_id}" for action_id in skipped_actions]
    return ReplicaFlow(1, script_path.stem, script_path.name, sha256_file(script_path), datetime.now(timezone.utc).isoformat(), viewport, plan.bootstrap, plan.popup_expectations, CaptureTimingProfile(), "s_000", states, warnings)


def capture_and_build(
    script_path: str | Path,
    output_root: str | Path,
    emit: Callable[[dict[str, str]], None] | None = None,
    storage_state: str | Path | None = None,
    interactive_auth: bool = False,
    annotations_payload: dict[str, object] | None = None,
) -> Path:
    """Run one live capture replay, persist its manifest, and build the offline entrypoint."""
    notify = emit or (lambda event: None)
    output_root = Path(output_root)
    capture_root = output_root / "capture"
    notify({"event": "capture_started"})
    result = run_live_capture(script_path, capture_root, storage_state=storage_state, interactive_auth=interactive_auth, emit=notify)
    if result.returncode:
        notify({"event": "failed", "stage": "capture"})
        raise RuntimeError(f"instrumented replay failed with exit {result.returncode}: {result.stderr[-1000:]}")
    notify({"event": "capture_finished"})
    flow = build_flow_from_snapshots(script_path, capture_root, annotations_payload)
    write_manifest(capture_root / "manifest.json", flow)
    notify({"event": "build_started"})
    entrypoint = build_replica(flow, capture_root, output_root / "replica")
    notify({"event": "build_finished", "entrypoint": str(entrypoint)})
    return entrypoint


def validate_annotations(script_path: str | Path, annotations_path: str | Path) -> dict[str, object]:
    """Reject stale GUI marker metadata before a live capture starts."""
    payload = json.loads(Path(annotations_path).read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise ValueError("unsupported replica annotations schema version")
    if payload.get("source_script_sha256") != sha256_file(script_path):
        raise ValueError("replica annotations do not match the processed script")
    return payload


def normalize_label(label: str) -> str:
    """Normalize a marker label for matching: collapse all whitespace and casefold.

    This is the single normalization rule applied to BOTH annotation labels and
    script marker_labels so the two sides compare equal. Whitespace runs collapse
    to a single space (leading/trailing stripped) and ASCII letters are lowercased;
    CJK characters are unaffected by casefold.
    """
    return " ".join(str(label).split()).casefold()


def merge_annotation_uuids(plan: ActionPlan, annotations_payload: dict[str, object]) -> dict[str, str]:
    """Match GUI annotation UUIDs onto marker groups/actions by source line + normalized label.

    This is the preflight merge step the brief requires after ``validate_annotations``.
    It builds ``{"line": {"normalized_label": uuid}}`` from the annotations payload and
    aligns each ``MarkerGroup`` by ``source_line`` + ``normalize_label(marker_label)``.

    Failure modes (all raise ``ValueError`` — preflight failure):
      - duplicate:      same source line + normalized label appears more than once
                        in the annotations payload
      - label mismatch: a marker group's source line has annotations, but the
                        normalized label differs from the script's marker_label
      - missing:        an annotation UUID has no matching marker group, OR a
                        marker group has no matching annotation

    On success it rewrites every group's ``marker_id`` and its action targets'
    ``marker_id`` to the GUI UUID, and returns ``{original_regenerated_id: uuid}``.
    The no-annotations path is untouched: callers simply omit this step.
    """
    by_line: dict[int, dict[str, str]] = {}
    for marker in annotations_payload.get("markers", []):
        line = marker["line"]
        norm = normalize_label(marker["label"])
        uuid = marker["marker_id"]
        if norm in by_line.setdefault(line, {}):
            raise ValueError(f"duplicate annotation for line {line}, label {marker['label']!r}")
        by_line[line][norm] = uuid

    result: dict[str, str] = {}
    for group in plan.marker_groups:
        original_id = group.marker_id
        line_labels = by_line.get(group.source_line)
        if line_labels is None:
            raise ValueError(f"missing annotation for marker at source line {group.source_line}")
        norm = normalize_label(group.marker_label)
        if norm not in line_labels:
            raise ValueError(
                f"label mismatch for marker at source line {group.source_line}: "
                f"script label {group.marker_label!r} does not match annotation labels {list(line_labels)}"
            )
        uuid = line_labels.pop(norm)
        result[original_id] = uuid
        group.marker_id = uuid
        for action in group.actions:
            action.marker_id = uuid

    remaining = [line for line, labels in by_line.items() for _label in labels]
    if remaining:
        raise ValueError(f"missing marker group for annotation line(s): {remaining}")
    return result



def await_interactive_auth(stream: object, emit: Callable[[dict[str, str]], None], timeout_s: float = 300) -> None:
    """Wait for the explicit JSONL confirmation before replaying marked actions.

    The command stream is drained by a daemon reader thread into a queue so the
    deadline loop never blocks on a single ``readline()``; line parsing and the
    ``queue.get`` poll interval are bounded (``min(0.25, remaining)`` seconds).
    """
    emit({"event": "auth_required", "message": "请完成登录后继续"})
    lines: queue.Queue[str | None] = queue.Queue()

    def reader() -> None:
        for line in stream:
            lines.put(line)
        lines.put(None)

    threading.Thread(target=reader, name="interactive-auth-reader", daemon=True).start()

    deadline = time.monotonic() + timeout_s
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("authentication_timeout")
        try:
            line = lines.get(timeout=min(0.25, remaining))
        except queue.Empty:
            continue
        if line is None:
            raise RuntimeError("authentication_cancelled")
        try:
            command = json.loads(line)
        except json.JSONDecodeError:
            continue
        if command.get("command") == "continue_after_auth":
            emit({"event": "auth_completed"})
            return
        if command.get("command") == "cancel":
            raise RuntimeError("authentication_cancelled")


def classify_capture_error(error: BaseException) -> str:
    """Return a stable, non-sensitive category for CLI/GUI progress reporting."""
    message = str(error).lower()
    if "auth" in message or "login" in message or "storage-state" in message:
        return "authentication"
    if "timeout" in message or "net::" in message or "connection" in message:
        return "network"
    if "locator" in message or "selector" in message or "strict mode" in message:
        return "selector_failure"
    if "404" in message or "503" in message or "site" in message:
        return "site_unavailable"
    return "capture_failure"


def build_from_manifest(
    manifest_path: str | Path,
    flow_root: str | Path,
    output_root: str | Path,
    emit: Callable[[dict[str, str]], None] | None = None,
) -> Path:
    """Build local assets from an already-captured flow without live network access."""
    notify = emit or (lambda event: print(json.dumps(event, ensure_ascii=False), flush=True))
    notify({"event": "build_started"})
    flow = read_manifest(manifest_path, flow_root)
    entrypoint = build_replica(flow, Path(flow_root), Path(output_root))
    notify({"event": "completed", "entrypoint": str(entrypoint)})
    return entrypoint


def main() -> None:
    parser = argparse.ArgumentParser(description="Capture/build a local interactive replica")
    parser.add_argument("--mode", choices=["offline-build", "live"], required=True)
    parser.add_argument("--manifest")
    parser.add_argument("--flow-root")
    parser.add_argument("--script")
    parser.add_argument("--annotations")
    parser.add_argument("--auth-mode", choices=["scripted", "interactive", "storage-state"], default="scripted")
    parser.add_argument("--storage-state")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if args.mode == "offline-build":
        if not args.manifest or not args.flow_root:
            parser.error("offline-build requires --manifest and --flow-root")
        build_from_manifest(args.manifest, args.flow_root, args.output)
    else:
        if not args.script:
            parser.error("live requires --script")
        if args.auth_mode == "storage-state" and not args.storage_state:
            parser.error("storage-state auth mode requires --storage-state")
        if args.auth_mode == "storage-state" and not Path(args.storage_state).is_file():
            parser.error("storage-state path does not exist")
        annotations_payload = None
        if args.annotations:
            annotations_payload = validate_annotations(args.script, args.annotations)
        emit = lambda event: print(json.dumps(event, ensure_ascii=False), flush=True)
        try:
            entrypoint = capture_and_build(
                args.script,
                args.output,
                emit,
                args.storage_state if args.auth_mode == "storage-state" else None,
                args.auth_mode == "interactive",
                annotations_payload,
            )
        except Exception as error:
            emit({"event": "failed", "category": classify_capture_error(error)})
            raise
        print(json.dumps({"event": "completed", "entrypoint": str(entrypoint)}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
