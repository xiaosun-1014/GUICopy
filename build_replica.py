"""Build an offline, interactive replica tree from a captured ReplicaFlow."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import asdict
from pathlib import Path

from locator_risk import classify_locator_risk
from replica_models import DomNodeSnapshot, ReplicaDocument, ReplicaFlow, ReplicaState
from rewrite_script import generate_replay_script, generate_serve_script
from replay_helpers import sha256_file


RUNTIME = """if (new URLSearchParams(location.search).has('debug')) {
  document.documentElement.classList.add('replica-debug');
}
document.addEventListener('click', event => {
  const option = event.target.closest('[role="option"]');
  if (option) {
    const group = option.closest('[role="listbox"], [data-replica-series]') || option.parentElement;
    group.querySelectorAll('[role="option"]').forEach(item => item.setAttribute('aria-selected', item === option ? 'true' : 'false'));
  }
  const backEl = event.target.closest('[data-replica-back]');
  if (backEl) {
    event.preventDefault();
    const target = backEl.getAttribute('data-replica-back');
    if (target) window.top.location.assign(target);
    return;
  }
  const element = event.target.closest('[data-replica-action]');
  if (!element) return;
  const transition = window.__REPLICA_TRANSITIONS__[element.dataset.replicaAction];
  if (!transition) return;
  event.preventDefault();
  if (transition.mode === 'popup') window.open(transition.url, transition.windowName || 'replica-popup');
  else window.top.location.assign(transition.url);
});
"""

ASSET_WARNING_BYTES = 50 * 1024 * 1024
ASSET_CONFIRM_BYTES = 200 * 1024 * 1024


def _document_path(document: ReplicaDocument, main_entry_document_id: str) -> Path:
    """Use one root index for main page and distinct roots for each popup page."""
    if document.document_id == main_entry_document_id:
        return Path("index.html")
    if document.parent_document_id is None:
        return Path("pages") / document.page_id / "index.html"
    return Path("documents") / document.document_id / "index.html"


def _positioned_html(snapshot: DomNodeSnapshot, action_id: str | None = None) -> str:
    attributes = ' data-replica-overlay=""'
    if action_id:
        attributes += f' data-replica-action="{action_id}"'
    style = f"position:absolute;left:{snapshot.rect.x}px;top:{snapshot.rect.y}px;width:{snapshot.rect.width}px;height:{snapshot.rect.height}px;"
    return snapshot.outer_html.replace(f"<{snapshot.tag_name}", f"<{snapshot.tag_name}{attributes} style=\"{style}\"", 1)


def _relative_url(source_file: Path, destination_file: Path) -> str:
    return Path(os.path.relpath(destination_file, source_file.parent)).as_posix()


def _render_document(
    document: ReplicaDocument,
    child_documents: list[ReplicaDocument],
    output_root: Path,
    destination: Path,
    document_paths: dict[str, Path],
    asset_path: Path,
    transitions: dict[str, dict[str, str]],
    back_target: str | None = None,
) -> str:
    asset = _relative_url(destination, output_root / asset_path)
    parts = [
        "<!doctype html><html><head><meta charset=\"utf-8\"><title>Replica</title>",
        "<style>html,body{margin:0;overflow:hidden}.replica{position:relative;overflow:hidden}.replica-bg{display:block;width:100%;height:100%;object-fit:fill}.overlay{position:absolute;inset:0}.overlay>*{box-sizing:border-box}.overlay>[data-replica-overlay]{opacity:0}.overlay>[data-replica-action]{z-index:1}.replica-debug .overlay>[data-replica-overlay]{opacity:.35;outline:2px solid #ff00aa;background:rgba(255,0,170,.08)}</style>",
        "</head>",
        f'<body><main class="replica" style="width:{document.viewport["width"]}px;height:{document.viewport["height"]}px">',
        f'<img class="replica-bg" src="{asset}" alt="Captured visual state">',
        '<section class="overlay">',
    ]
    rendered_nodes: set[tuple[str, float, float, float, float]] = set()
    rendered_element_ids: set[str] = set()
    region_by_id = {
        member.dom.attributes["id"]: member.dom
        for region in document.regions
        for member in region.members
        if member.dom.attributes.get("id")
    }
    for target in document.targets:
        if target.dom:
            snapshot = region_by_id.get(target.dom.attributes.get("id", ""), target.dom)
            parts.append(_positioned_html(snapshot, target.action_id))
            rendered_nodes.add((snapshot.outer_html, snapshot.rect.x, snapshot.rect.y, snapshot.rect.width, snapshot.rect.height))
            if snapshot.attributes.get("id"):
                rendered_element_ids.add(snapshot.attributes["id"])
    for region in document.regions:
        for member in region.members:
            if member.dom.attributes.get("id") in rendered_element_ids:
                continue
            if any(f'id="{element_id}"' in member.dom.outer_html for element_id in rendered_element_ids):
                continue
            member_key = (member.dom.outer_html, member.dom.rect.x, member.dom.rect.y, member.dom.rect.width, member.dom.rect.height)
            if member_key in rendered_nodes:
                continue
            parts.append(_positioned_html(member.dom))
            rendered_nodes.add(member_key)
            if member.dom.attributes.get("id"):
                rendered_element_ids.add(member.dom.attributes["id"])
    rendered_metadata_regions: set[str] = set()
    for region in document.regions:
        if (
            region.region_type != "metadata"
            or not region.root.outer_html
            or region.region_id in rendered_metadata_regions
        ):
            continue
        rendered_metadata_regions.add(region.region_id)
        rect = region.root.rect
        panel_style = (
            f"position:absolute;left:{rect.x}px;top:{rect.y}px;"
            f"width:{rect.width}px;height:{rect.height}px;overflow-y:auto;z-index:2;"
        )
        parts.append(
            f'<div data-replica-panel-region="{region.region_id}" style="{panel_style}">'
            f"{region.root.outer_html}</div>"
        )
    if back_target is not None:
        parts.append(
            f'<button data-replica-back="{back_target}" style="position:fixed;top:8px;right:8px;z-index:5;'
            'padding:4px 12px;font:14px/1.4 sans-serif;color:#fff;background:#b91c1c;border:none;'
            'border-radius:4px;cursor:pointer;">× 关闭</button>'
        )
    for child in child_documents:
        source = _relative_url(destination, document_paths[child.document_id])
        frame_id = f' id="{child.frame_id}"' if child.frame_id else ""
        frame_name = f' name="{child.frame_name}"' if child.frame_name else ""
        parts.append(f'<iframe{frame_id}{frame_name} src="{source}" style="position:absolute;left:0;top:0;width:{child.viewport["width"]}px;height:{child.viewport["height"]}px"></iframe>')
    runtime_url = _relative_url(destination, output_root / "replica_runtime.js")
    parts.extend(["</section></main>", f"<script>window.__REPLICA_TRANSITIONS__={json.dumps(transitions, ensure_ascii=False)};</script>", f'<script src="{runtime_url}"></script>', "</body></html>"])
    return "".join(parts)


def _state_root(flow: ReplicaFlow, state: ReplicaState, output_root: Path) -> Path:
    return output_root if state.state_id == flow.entry_state_id else output_root / "states" / state.state_id


def _replay_steps(flow: ReplicaFlow) -> list[dict[str, object]]:
    """Flatten marked targets in capture order and retain their transition semantics."""
    transitions = {transition.action_id: transition for state in flow.states for transition in state.transitions}
    steps: list[dict[str, object]] = []
    seen: set[str] = set()
    for state in sorted(flow.states, key=lambda candidate: candidate.ordinal):
        for document in state.documents:
            for target in document.targets:
                if target.action_id in seen or target.replay_policy != "execute":
                    continue
                seen.add(target.action_id)
                step = asdict(target)
                step["page_var"] = target.locator.page_var if target.locator else "page"
                transition = transitions.get(target.action_id)
                if transition:
                    step["transition"] = {
                        "mode": transition.mode,
                        "target_page_var": transition.target_page_var,
                    }
                steps.append(step)
    return steps


def _locator_risk_metadata(flow: ReplicaFlow) -> dict[str, dict[str, object]]:
    """Derive locator risk metadata from the flow's ActionTargets (never empty [])."""
    metadata: dict[str, dict[str, object]] = {}
    for state in flow.states:
        for document in state.documents:
            for target in document.targets:
                metadata[target.action_id] = {
                    "marker_id": target.marker_id,
                    "locator_risk": classify_locator_risk(target),
                    "locator_kind": target.locator.locator_kind if target.locator else None,
                    "required_ancestor_count": target.selector_closure.required_ancestor_count if target.selector_closure else 0,
                    "required_sibling_count": target.selector_closure.required_sibling_count if target.selector_closure else 0,
                }
    return metadata


def build_replica(flow: ReplicaFlow, source_root: Path, output_root: Path) -> Path:
    """Write screenshots, DOM overlays, iframe trees, and declared state transitions."""
    source_root = Path(source_root)
    output_root = Path(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    # Provenance gate: every declared screenshot asset must resolve to a file.
    for state in flow.states:
        for document in state.documents:
            if not document.screenshot_asset_relpath:
                continue
            source_asset = source_root / document.screenshot_asset_relpath
            candidate = None
            if source_asset.exists():
                candidate = source_asset
            else:
                jpeg = source_asset.with_suffix(".jpeg")
                if jpeg.exists():
                    candidate = jpeg
            if candidate is None:
                raise FileNotFoundError(
                    f"required screenshot asset missing: {document.screenshot_asset_relpath}"
                )
    (output_root / "replica_runtime.js").write_text(RUNTIME, encoding="utf-8")
    (output_root / "serve_replica.py").write_text(generate_serve_script(), encoding="utf-8")
    (output_root / "replay_replica.py").write_text(
        generate_replay_script(".", flow.bootstrap.entry_page_bindings, _replay_steps(flow)), encoding="utf-8"
    )
    locator_risk_metadata = _locator_risk_metadata(flow)
    (output_root / "locator_mapping.json").write_text(json.dumps(locator_risk_metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    states = {state.state_id: state for state in flow.states}
    asset_paths: dict[tuple[str, str], Path] = {}
    copied_hashes: set[Path] = set()
    total_asset_bytes = 0
    for state in flow.states:
        for document in state.documents:
            source_asset = source_root / document.screenshot_asset_relpath
            visual_source = source_asset.with_suffix(".jpeg") if source_asset.with_suffix(".jpeg").exists() else source_asset
            suffix = visual_source.suffix or ".png"
            visual_hash = sha256_file(visual_source) if visual_source.exists() else document.screenshot_sha256
            asset_paths[(state.state_id, document.document_id)] = Path("assets") / "by-hash" / f"{visual_hash}{suffix}"
            destination_asset = output_root / asset_paths[(state.state_id, document.document_id)]
            if destination_asset in copied_hashes:
                continue
            destination_asset.parent.mkdir(parents=True, exist_ok=True)
            if visual_source.exists():
                shutil.copy2(visual_source, destination_asset)
                total_asset_bytes += destination_asset.stat().st_size
            copied_hashes.add(destination_asset)
    build_warnings: list[str] = []
    if total_asset_bytes >= ASSET_CONFIRM_BYTES:
        build_warnings.append(f"asset_size_confirmation_required:{total_asset_bytes}")
    elif total_asset_bytes >= ASSET_WARNING_BYTES:
        build_warnings.append(f"asset_size_warning:{total_asset_bytes}")
    build_warnings.extend(flow.warnings)
    for state in flow.states:
        state_root = _state_root(flow, state, output_root)
        main_page = next((page for page in state.pages if page.page_var == "page"), state.pages[0] if state.pages else None)
        main_entry_document_id = main_page.entry_document_id if main_page else ""
        active_page = next(
            (page for page in state.pages if page.page_var == state.active_page_var),
            main_page,
        )
        active_entry_document_id = active_page.entry_document_id if active_page else ""
        active_page_has_metadata = bool(
            active_page
            and any(
                region.region_type == "metadata"
                for candidate in state.documents
                if candidate.page_id == active_page.page_id
                for region in candidate.regions
            )
        )
        document_paths = {document.document_id: state_root / _document_path(document, main_entry_document_id) for document in state.documents}
        transitions: dict[str, dict[str, object]] = {}
        for transition in state.transitions:
            if transition.to_state_id is None or transition.to_state_id not in states:
                continue
            target_state = states[transition.to_state_id]
            target_page = next((page for page in target_state.pages if page.page_var == transition.target_page_var), None)
            if target_page is None:
                continue
            target_root = _state_root(flow, target_state, output_root)
            target_main_page = next((page for page in target_state.pages if page.page_var == "page"), target_state.pages[0])
            target_path = target_root / _document_path(
                next(document for document in target_state.documents if document.document_id == target_page.entry_document_id),
                target_main_page.entry_document_id,
            )
            transitions[transition.action_id] = {"mode": transition.mode, "target_path": target_path, "windowName": target_page.window_name or "replica-popup"}
        for document in state.documents:
            children = [candidate for candidate in state.documents if candidate.parent_document_id == document.document_id]
            destination = document_paths[document.document_id]
            destination.parent.mkdir(parents=True, exist_ok=True)
            document_transitions = {
                target.action_id: {
                    "mode": str(transitions[target.action_id]["mode"]),
                    "url": _relative_url(destination, transitions[target.action_id]["target_path"]),
                    "windowName": str(transitions[target.action_id]["windowName"]),
                }
                for target in document.targets
                if target.action_id in transitions
            }
            back_abs = None
            if state.state_id != flow.entry_state_id:
                ordered = sorted(flow.states, key=lambda s: s.ordinal)
                position = [i for i, s in enumerate(ordered) if s.state_id == state.state_id]
                if position and position[0] > 0:
                    prev_state = ordered[position[0] - 1]
                    prev_main_page = next(
                        (page for page in prev_state.pages if page.page_var == "page"),
                        prev_state.pages[0] if prev_state.pages else None,
                    )
                    prev_page = next(
                        (page for page in prev_state.pages if page.page_var == prev_state.active_page_var),
                        prev_main_page,
                    )
                    if prev_page is not None:
                        prev_doc = next(
                            (d for d in prev_state.documents if d.document_id == prev_page.entry_document_id),
                            prev_state.documents[0] if prev_state.documents else None,
                        )
                        if prev_doc is not None:
                            prev_main_entry_id = prev_main_page.entry_document_id if prev_main_page else ""
                            back_abs = _state_root(flow, prev_state, output_root) / _document_path(prev_doc, prev_main_entry_id)
            back_target = _relative_url(destination, back_abs) if back_abs is not None else None
            show_metadata_back = (
                document.document_id == active_entry_document_id
                and active_page_has_metadata
            )
            destination.write_text(_render_document(document, children, output_root, destination, document_paths, asset_paths[(state.state_id, document.document_id)], document_transitions, back_target if show_metadata_back else None), encoding="utf-8")
    entrypoint = output_root / "index.html"
    if not entrypoint.exists():
        entrypoint.write_text("<!doctype html><meta charset=\"utf-8\"><title>Empty replica</title>", encoding="utf-8")
    (output_root / "replica_build_report.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "flow_id": flow.flow_id,
                "entrypoint": str(entrypoint.relative_to(output_root)).replace("\\", "/"),
                "locator_risks": locator_risk_metadata,
                "build_warnings": build_warnings,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return entrypoint
