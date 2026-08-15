"""Build an offline, interactive replica tree from a captured ReplicaFlow."""

from __future__ import annotations

import json
import os
import shutil
import hashlib
import html
import re
from dataclasses import asdict
from pathlib import Path

from locator_risk import classify_locator_risk
from replica_models import DomNodeSnapshot, InteractionRegion, ReplicaDocument, ReplicaFlow, ReplicaState
from rewrite_script import generate_replay_script, generate_serve_script
from replay_helpers import sha256_file, series_key_slug


_SERIES_IDENTITY_ATTRS = ("data-series-uid", "data-series", "data-uid", "value", "id")
# Raw, patient/series-identity-bearing attributes that must never reach the
# *served* HTML surface. Routing uses the injected ``data-replica-series-key``
# slug, so these are safe to drop from the rendered member markup while the
# complete (sanitized-for-executables) Metadata panel remains intact.
_SERIES_IDENTITY_REDACT_ATTRS = (*_SERIES_IDENTITY_ATTRS, "title")

# Identity values shorter than this are not safe to replace globally: a short
# value ("1", "202") would appear all over markup text / CSS, and replacing
# every occurrence silently corrupts the served page's text and styles. Real
# identity values (UIDs, unique series descriptions) are far longer and remain
# fully redacted; short values are left in place as the safe trade-off.
_REDACT_MIN_IDENTITY_LEN = 8


def _redact_series_identity_attrs(outer_html: str) -> str:
    """Remove raw identity attributes (e.g. ``data-series-uid``) from served HTML.

    Only the public ``data-replica-series-key`` slug is authorative for offline
    series navigation, so these raw attributes are pure privacy leakage and are
    stripped from the served surface (P1#7/closure). The value (a real UID) is
    never echoed back into the served DOM.
    """
    for name in _SERIES_IDENTITY_REDACT_ATTRS:
        outer_html = re.sub(rf'\s{name}=("[^"]*"|\'[^\']*\')', "", outer_html, flags=re.IGNORECASE)
    return outer_html


def _redact_series_snapshot_markup(
    snapshot: DomNodeSnapshot,
    markup: str,
    replacement: str = "redacted-series",
) -> str:
    """Redact identity attributes and any repeated identity value in a subtree."""
    markup = _redact_series_identity_attrs(markup)
    raw_values = {
        snapshot.attributes.get(name, "")
        for name in _SERIES_IDENTITY_REDACT_ATTRS
    }
    for raw_value in sorted((value for value in raw_values if value), key=len, reverse=True):
        if len(raw_value) < _REDACT_MIN_IDENTITY_LEN:
            continue
        markup = markup.replace(html.escape(raw_value, quote=True), replacement)
        markup = markup.replace(raw_value, replacement)
    return markup


def _redact_known_series_identities(
    markup: str,
    series_route_by_identity: dict[str, dict[str, object]] | None,
) -> str:
    """Replace every captured raw series identity with its public route slug."""
    for raw_value, route in sorted(
        (series_route_by_identity or {}).items(), key=lambda item: len(item[0]), reverse=True
    ):
        if not raw_value or len(raw_value) < _REDACT_MIN_IDENTITY_LEN:
            continue
        replacement = str(route.get("slug") or "redacted-series")
        markup = markup.replace(html.escape(raw_value, quote=True), replacement)
        markup = markup.replace(raw_value, replacement)
    return markup


def _member_series_key(dom: DomNodeSnapshot) -> str | None:
    """Derive a series member's stable identity from its own DOM attributes.

    (P0#3) This is the same identity-priority rule ``capture_snapshot`` uses for
    descriptor identity, so a member in *any* captured document (the recorded
    hub, or a branch Viewer document) yields the same raw key as its branch's
    ``series_key``. Only the public slug ever enters served HTML.
    """
    for name in _SERIES_IDENTITY_ATTRS:
        value = dom.attributes.get(name)
        if value:
            return value
    return None


RUNTIME = """if (new URLSearchParams(location.search).has('debug')) {
  document.documentElement.classList.add('replica-debug');
}
function applyReplicaSeriesRoute(seriesEl, event) {
  const routes = window.__REPLICA_SERIES_ROUTE__ || {};
  const key = seriesEl.getAttribute('data-replica-series-key');
  const route = routes[key];
  // Immediate a11y feedback: mark this option selected within its group before
  // any navigation; the final content still comes from the target state.
  const group = seriesEl.closest('[role="listbox"], [data-replica-series]') || seriesEl.parentElement;
  if (group) {
    group.querySelectorAll('[role="option"]').forEach(item => {
      if (item === seriesEl || (item !== seriesEl && item.contains && item.contains(seriesEl))) {
        item.setAttribute('aria-selected', 'true');
        item.setAttribute('aria-disabled', 'false');
      } else {
        item.setAttribute('aria-selected', 'false');
      }
    });
  }
  // Disabled branch (failed / no route): navigate nowhere, stay audibly disabled.
  if (!route || route.disabled || !route.viewerUrl) {
    seriesEl.setAttribute('aria-disabled', 'true');
    if (event) event.preventDefault();
    return;
  }
  if (event) event.preventDefault();
  window.top.location.assign(route.viewerUrl);
}
function fitReplicaToViewport() {
  const replica = document.querySelector('.replica');
  if (!replica) return;
  const width = Number(replica.dataset.viewportWidth);
  const height = Number(replica.dataset.viewportHeight);
  if (!width || !height) return;
  const viewport = window.visualViewport;
  const viewportWidth = viewport ? viewport.width : window.innerWidth;
  const viewportHeight = viewport ? viewport.height : window.innerHeight;
  const scaleX = viewportWidth / width;
  const scaleY = viewportHeight / height;
  replica.style.transform = `scale(${scaleX}, ${scaleY})`;
  replica.style.left = '0px';
  replica.style.top = '0px';
}
window.addEventListener('resize', fitReplicaToViewport);
if (window.visualViewport) window.visualViewport.addEventListener('resize', fitReplicaToViewport);
fitReplicaToViewport();
document.addEventListener('click', event => {
  const seriesEl = event.target.closest('[data-replica-series-key]');
  if (seriesEl) {
    applyReplicaSeriesRoute(seriesEl, event);
    return;
  }
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
  if (transition.mode === 'input') return;
  event.preventDefault();
  if (transition.mode === 'input_activate') sessionStorage.setItem('replica-autofocus', transition.inputId || '');
  if (transition.mode === 'popup') window.open(transition.url, transition.windowName || 'replica-popup');
  else window.top.location.assign(transition.url);
});
function fitReplicaInput(element) {
  if (!element.dataset.replicaRight) {
    element.dataset.replicaRight = String(parseFloat(element.style.left) + parseFloat(element.style.width));
  }
  const width = Math.max(parseFloat(element.dataset.replicaWidth || element.style.width), element.value.length * 7.2 + 2);
  element.style.width = `${width}px`;
  element.style.left = `${parseFloat(element.dataset.replicaRight) - width}px`;
}
document.querySelectorAll('[data-replica-input]').forEach(element => {
  element.dataset.replicaWidth = element.style.width;
  const stored = sessionStorage.getItem(`replica-input:${element.id}`);
  if (stored !== null) element.value = stored;
  fitReplicaInput(element);
  element.addEventListener('input', () => {
    sessionStorage.setItem(`replica-input:${element.id}`, element.value);
    fitReplicaInput(element);
  });
});
const autofocusId = sessionStorage.getItem('replica-autofocus');
if (autofocusId) {
  const autofocusInput = document.getElementById(autofocusId);
  if (autofocusInput) {
    sessionStorage.removeItem('replica-autofocus');
    const focusInput = () => {
      autofocusInput.focus();
      autofocusInput.setSelectionRange(autofocusInput.value.length, autofocusInput.value.length);
    };
    focusInput();
    window.setTimeout(focusInput, 100);
    window.setTimeout(focusInput, 300);
    window.addEventListener('load', focusInput, { once: true });
  }
}
document.addEventListener('keydown', event => {
  if (event.key !== 'Enter') return;
  const element = event.target.closest('[data-replica-action]');
  if (!element) return;
  const transition = window.__REPLICA_TRANSITIONS__[element.dataset.replicaAction];
  if (!transition || transition.mode !== 'input') return;
  event.preventDefault();
  if (element.id) sessionStorage.setItem(`replica-input:${element.id}`, element.value);
  window.top.location.assign(transition.url);
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


def _positioned_html(snapshot: DomNodeSnapshot, action_id: str | None = None, input_mode: bool = False) -> str:
    attributes = ' data-replica-overlay=""'
    if action_id:
        attributes += f' data-replica-action="{action_id}"'
    if input_mode:
        attributes += ' data-replica-input="" autocomplete="off" spellcheck="false" data-lpignore="true"'
    if snapshot.tag_name == "a" and " role=" not in snapshot.outer_html and " href=" not in snapshot.outer_html:
        attributes += ' role="link"'
    style = f"position:absolute;left:{snapshot.rect.x}px;top:{snapshot.rect.y}px;width:{snapshot.rect.width}px;height:{snapshot.rect.height}px;"
    if input_mode:
        style += snapshot.attributes.get("style", "")
    outer_html = snapshot.outer_html
    if input_mode:
        outer_html = re.sub(r'\sstyle=("[^"]*"|\'[^\']*\')', "", outer_html, count=1, flags=re.I)
    if input_mode and "value" in snapshot.attributes:
        value = html.escape(snapshot.attributes["value"], quote=True)
        if re.search(r'\svalue=("[^"]*"|\'[^\']*\')', outer_html, flags=re.I):
            outer_html = re.sub(r'\svalue=("[^"]*"|\'[^\']*\')', f' value="{value}"', outer_html, count=1, flags=re.I)
        else:
            outer_html = outer_html.replace(f"<{snapshot.tag_name}", f'<{snapshot.tag_name} value="{value}"', 1)
    return outer_html.replace(f"<{snapshot.tag_name}", f"<{snapshot.tag_name}{attributes} style=\"{style}\"", 1)


def _action_html(snapshot: DomNodeSnapshot, action_id: str) -> str:
    """Attach an action to a node already positioned inside a captured region root."""
    attributes = f' data-replica-overlay="" data-replica-action="{action_id}"'
    if snapshot.tag_name == "a" and " role=" not in snapshot.outer_html and " href=" not in snapshot.outer_html:
        attributes += ' role="link"'
    return snapshot.outer_html.replace(f"<{snapshot.tag_name}", f"<{snapshot.tag_name}{attributes}", 1)


def _series_member_html(
    snapshot: DomNodeSnapshot,
    series_key: str,
    selected: bool,
    disabled: bool,
) -> str:
    """Attach a series route key with accessible option semantics to a member node."""
    attributes = (
        f' data-replica-overlay="" data-replica-series-key="{html.escape(series_key, quote=True)}"'
    )
    role_attr = ' role="option"'
    if ' role=' not in snapshot.outer_html and ' role=' not in " ".join(snapshot.attributes):
        attributes += role_attr
    attributes += f' aria-selected="{"true" if selected else "false"}"'
    if disabled:
        attributes += ' aria-disabled="true"'
    # Raw identity attributes never reach the served subtree. Some viewers copy
    # the SeriesInstanceUID into every descendant ``id`` and patient identity
    # into ``title``; neither is needed because the public slug routes offline.
    src = _redact_series_snapshot_markup(snapshot, snapshot.outer_html, series_key)
    return src.replace(f"<{snapshot.tag_name}", f"<{snapshot.tag_name}{attributes}", 1)


def _relative_url(source_file: Path, destination_file: Path) -> str:
    return Path(os.path.relpath(destination_file, source_file.parent)).as_posix()


def _is_metadata_panel(region: InteractionRegion) -> bool:
    """Distinguish a real metadata panel from Tags icons and patient summaries."""
    if region.region_type != "metadata" or not region.root.outer_html or not region.root.text.strip():
        return False
    attributes = region.root.attributes
    identity = " ".join((attributes.get("id", ""), attributes.get("class", ""))).lower()
    is_named_panel = any(token in identity for token in ("tagsbox", "box-tags", "dicom", "metadata"))
    return is_named_panel or attributes.get("role", "").lower() == "dialog"


def _target_for_action(state: ReplicaState, action_id: str) -> ActionTarget | None:
    return next(
        (
            target
            for document in state.documents
            for target in document.targets
            if target.action_id == action_id
        ),
        None,
    )


def _input_submit_state(
    states: dict[str, ReplicaState],
    transition: ReplicaTransition,
) -> ReplicaState | None:
    """Collapse recorded fill -> Enter states into one editable input interaction."""
    source = states[transition.from_state_id]
    fill = _target_for_action(source, transition.action_id)
    if fill is None or fill.action_type != "fill" or transition.to_state_id is None:
        return None
    filled = states.get(transition.to_state_id)
    if filled is None:
        return None
    for submit in filled.transitions:
        press = _target_for_action(filled, submit.action_id)
        args = press.action_args.get("args", []) if press is not None else []
        if (
            press is not None
            and press.action_type == "press"
            and args == ["Enter"]
            and press.marker_id == fill.marker_id
            and submit.to_state_id in states
        ):
            return states[submit.to_state_id]
    return None


def _input_activation(
    states: dict[str, ReplicaState],
    transition: ReplicaTransition,
) -> ActionTarget | None:
    """Recognize a recorded canvas click that reveals the editable WL/WW field."""
    source = states[transition.from_state_id]
    click = _target_for_action(source, transition.action_id)
    target = states.get(transition.to_state_id or "")
    if click is None or click.action_type != "click" or target is None:
        return None
    next_action_ids = {item.action_id for item in target.transitions}
    candidates = []
    for document in target.documents:
        for candidate in document.targets:
            if (
                candidate.marker_id == click.marker_id
                and candidate.action_type == "fill"
                and candidate.dom is not None
                and candidate.dom.tag_name in {"input", "textarea"}
            ):
                candidates.append(candidate)
    return next((candidate for candidate in candidates if candidate.action_id in next_action_ids), candidates[-1] if candidates else None)


def _render_document(
    document: ReplicaDocument,
    child_documents: list[ReplicaDocument],
    output_root: Path,
    destination: Path,
    document_paths: dict[str, Path],
    asset_path: Path,
    transitions: dict[str, dict[str, str]],
    back_target: str | None = None,
    series_route: dict[str, dict[str, object]] | None = None,
    series_key_by_member: dict[str, str] | None = None,
    selected_series_key: str | None = None,
    series_route_by_identity: dict[str, dict[str, object]] | None = None,
) -> str:
    asset = _relative_url(destination, output_root / asset_path)
    parts = [
        "<!doctype html><html><head><meta charset=\"utf-8\"><title>Replica</title>",
        "<style>"
        "html,body{margin:0;width:100%;height:100%;overflow:hidden;background:rgb(3,6,9)}"
        "body{position:relative}"
        ".replica{position:absolute;overflow:hidden;transform-origin:top left}"
        ".replica-bg{display:block;width:100%;height:100%;object-fit:fill}"
        ".overlay{position:absolute;inset:0}.overlay>*{box-sizing:border-box}"
        ".overlay>[data-replica-overlay]{opacity:0}.overlay>[data-replica-action]{z-index:1}"
        ".overlay>[data-replica-overlay]:not([data-replica-action]):not([data-replica-input]):not([data-replica-series-key]):not([role]):not(button):not(input):not(select):not(textarea):not(canvas):not(a){pointer-events:none}"
        ".overlay>[data-replica-action],.overlay>[data-replica-input],.overlay>[data-replica-series-key],.overlay>[data-replica-overlay][role],.overlay>[data-replica-overlay][data-testid],.overlay>[data-replica-overlay]button,.overlay>[data-replica-overlay]input,.overlay>[data-replica-overlay]select,.overlay>[data-replica-overlay]textarea,.overlay>[data-replica-overlay]canvas,.overlay>[data-replica-overlay]a{pointer-events:auto}"
        ".overlay>[data-replica-input]{opacity:1;caret-color:rgb(255,255,255)}"
        ".replica-metadata{background:rgb(3,6,9);color:rgb(209,228,255);"
        "font:14px/1.35 'Helvetica Neue',Helvetica,'Microsoft YaHei',Arial,sans-serif;"
        "overscroll-behavior:contain;scrollbar-gutter:stable}"
        ".replica-metadata>div{box-sizing:border-box;min-height:100%;width:100%;color:inherit;font:inherit}"
        ".replica-metadata .content{box-sizing:border-box;min-height:100%;padding:42px 5% 24px}"
        ".replica-metadata .panel{margin:0 0 6px;border:1px solid rgb(29,35,43);"
        "border-radius:4px;overflow:hidden;background:rgb(1,3,5)}"
        ".replica-metadata .hd{display:flex;min-height:29px;align-items:center;justify-content:center;"
        "background:rgb(36,38,40);color:rgb(209,228,255);font-weight:500;text-transform:uppercase}"
        ".replica-metadata .bd{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));"
        "column-gap:6%;padding:7px 6px 9px}"
        ".replica-metadata .item{min-width:0;line-height:19px;overflow-wrap:anywhere}"
        ".replica-metadata .item.single{grid-column:1/-1}"
        ".replica-metadata .close{position:absolute;top:8px;right:8px;z-index:4;display:block;"
        "padding:4px 12px;color:rgb(248,250,252);background:rgb(185,28,28);border-radius:4px;cursor:pointer}"
        ".replica-metadata .close::after{content:'× 关闭'}"
        ".replica-metadata [data-replica-panel-close]{position:absolute;top:8px;right:8px;z-index:5;"
        "display:block;min-width:64px;height:30px;padding:4px 12px;color:#fff;background:#b91c1c;"
        "border:0;border-radius:4px;cursor:pointer}"
        ".replica-metadata [data-replica-panel-close]::after{content:'× 关闭'}"
        ".replica-debug .overlay>[data-replica-overlay]{opacity:.35;outline:2px solid rgb(255,0,170);background:rgba(255,0,170,.08)}"
        "@media(max-width:720px){.replica-metadata .content{padding-inline:24px}.replica-metadata .bd{grid-template-columns:1fr}.replica-metadata .item.single{grid-column:auto}}"
        "</style>",
        "</head>",
        f'<body><main class="replica" data-viewport-width="{document.viewport["width"]}" '
        f'data-viewport-height="{document.viewport["height"]}" '
        f'style="width:{document.viewport["width"]}px;height:{document.viewport["height"]}px">',
        f'<img class="replica-bg" src="{asset}" alt="Captured visual state">',
        '<section class="overlay">',
    ]
    rendered_nodes: set[tuple[str, float, float, float, float]] = set()
    rendered_element_ids: set[str] = set()
    metadata_panel_html = {
        region.region_id: region.root.outer_html
        for region in document.regions
        if _is_metadata_panel(region)
    }
    embedded_target_ids: set[str] = set()
    embedded_close_action_id = None
    for region in document.regions:
        if region.region_id not in metadata_panel_html:
            continue
        panel_html = metadata_panel_html[region.region_id]
        for target in document.targets:
            if not target.dom or target.dom.outer_html not in panel_html:
                continue
            action_html = _action_html(target.dom, target.action_id)
            is_close = "close" in target.dom.attributes.get("class", "").lower()
            if is_close:
                action_html = action_html.replace(
                    "data-replica-overlay=\"\"",
                    "data-replica-overlay=\"\" data-replica-panel-close",
                    1,
                )
            panel_html = panel_html.replace(target.dom.outer_html, action_html, 1)
            embedded_target_ids.add(target.action_id)
            if is_close:
                embedded_close_action_id = target.action_id
        metadata_panel_html[region.region_id] = panel_html
    region_by_id = {
        member.dom.attributes["id"]: member.dom
        for region in document.regions
        for member in region.members
        if member.dom.attributes.get("id")
    }
    last_target_by_element_id = {
        target.dom.attributes["id"]: index
        for index, target in enumerate(document.targets)
        if target.dom and target.dom.attributes.get("id")
    }
    duplicate_action_ids = {
        target.action_id
        for target in document.targets
        if sum(candidate.action_id == target.action_id for candidate in document.targets) > 1
    }
    for target_index, target in enumerate(document.targets):
        if target.action_id in embedded_target_ids:
            continue
        if target.dom:
            element_id = target.dom.attributes.get("id", "")
            is_duplicate_action = target.action_id in duplicate_action_ids
            # Collapse multiple distinct actions on one element to the last one
            # (e.g. fill then press on the same input). A genuine duplicate action
            # id must still render every copy so ``critical_locator_not_unique``
            # validation can flag it.
            if (
                element_id
                and last_target_by_element_id[element_id] != target_index
                and not is_duplicate_action
            ):
                continue
            if (
                element_id
                and element_id in rendered_element_ids
                and not is_duplicate_action
            ):
                continue
            input_mode = target.dom.tag_name in {"input", "textarea"}
            snapshot = target.dom if input_mode else region_by_id.get(target.dom.attributes.get("id", ""), target.dom)
            target_markup = _positioned_html(
                snapshot,
                target.action_id,
                input_mode,
            )
            parts.append(_redact_known_series_identities(
                target_markup, series_route_by_identity
            ))
            rendered_nodes.add((snapshot.outer_html, snapshot.rect.x, snapshot.rect.y, snapshot.rect.width, snapshot.rect.height))
            if snapshot.attributes.get("id"):
                rendered_element_ids.add(snapshot.attributes["id"])
    for region in document.regions:
        metadata_panel_html_for_region = (
            metadata_panel_html.get(region.region_id)
            if _is_metadata_panel(region)
            else None
        )
        for member in region.members:
            if member.dom.attributes.get("id") in rendered_element_ids:
                continue
            if any(f'id="{element_id}"' in member.dom.outer_html for element_id in rendered_element_ids):
                continue
            if metadata_panel_html_for_region is not None and member.dom.outer_html:
                # Sibling controls around a metadata panel are captured as
                # members (WL/WW inputs, confirm button, canvas); anything the
                # panel root already contains verbatim must not be duplicated.
                if (
                    member.dom.outer_html in metadata_panel_html_for_region
                    or metadata_panel_html_for_region in member.dom.outer_html
                ):
                    continue
            member_key = (member.dom.outer_html, member.dom.rect.x, member.dom.rect.y, member.dom.rect.width, member.dom.rect.height)
            if member_key in rendered_nodes:
                continue
            series_key = None
            disabled_route = False
            if region.region_type == "series":
                # P0#3: bind by the branch's own viewer member id first (the
                # review-sanctioned path), then by the member's stable semantic
                # identity so the recorded hub and *every* branch Viewer document
                # route the same series regardless of which document captured the
                # list. Only public slugs are written into served HTML.
                if series_key_by_member:
                    series_key = series_key_by_member.get(member.member_id)
                if series_key is None and series_route_by_identity:
                    identity = _member_series_key(member.dom)
                    identity_route = series_route_by_identity.get(identity) if identity else None
                    if identity_route is not None:
                        series_key = str(identity_route.get("slug"))
                        disabled_route = bool(identity_route.get("disabled"))
            if series_key is not None:
                route = (series_route or {}).get(series_key, {})
                member_markup = _series_member_html(
                    member.dom,
                    series_key,
                    selected=bool(series_key == selected_series_key),
                    disabled=disabled_route or bool(route.get("disabled")),
                )
                parts.append(_redact_known_series_identities(
                    member_markup, series_route_by_identity
                ))
            else:
                positioned = _positioned_html(member.dom)
                if region.region_type == "series":
                    positioned = _redact_series_snapshot_markup(member.dom, positioned)
                parts.append(_redact_known_series_identities(
                    positioned, series_route_by_identity
                ))
            rendered_nodes.add(member_key)
            if member.dom.attributes.get("id"):
                rendered_element_ids.add(member.dom.attributes["id"])
    rendered_metadata_regions: set[str] = set()
    for region in document.regions:
        if (
            not _is_metadata_panel(region)
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
            f'<div class="replica-metadata" data-replica-panel-region="{region.region_id}" style="{panel_style}">'
            f"{metadata_panel_html[region.region_id]}</div>"
        )
    if back_target is not None and embedded_close_action_id is None:
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
    runtime_version = hashlib.sha256(RUNTIME.encode("utf-8")).hexdigest()[:12]
    runtime_url = f"{runtime_url}?v={runtime_version}"
    parts.extend(["</section></main>", f"<script>window.__REPLICA_TRANSITIONS__={json.dumps(transitions, ensure_ascii=False)};</script>"])
    if series_route:
        parts.append(f"<script>window.__REPLICA_SERIES_ROUTE__={json.dumps(series_route, ensure_ascii=False)};</script>")
    parts.append(f'<script src="{runtime_url}"></script>')
    parts.append("</body></html>")
    return "".join(parts)


def _state_root(flow: ReplicaFlow, state: ReplicaState, output_root: Path) -> Path:
    return output_root if state.state_id == flow.entry_state_id else output_root / "states" / state.state_id


def _state_entry_path(flow: ReplicaFlow, state: ReplicaState, output_root: Path) -> Path:
    """Resolve the absolute path to a state's active entry document.

    Used to compute branch route viewer/metadata/return URLs relative to any
    rendered document.
    """
    main_page = next(
        (page for page in state.pages if page.page_var == "page"),
        state.pages[0] if state.pages else None,
    )
    active_page = next(
        (page for page in state.pages if page.page_var == state.active_page_var),
        main_page,
    )
    main_entry_id = main_page.entry_document_id if main_page else ""
    entry_id = active_page.entry_document_id if active_page else ""
    try:
        doc = next(
            (d for d in state.documents if d.document_id == entry_id),
            state.documents[0],
        )
    except IndexError:
        return Path()
    return _state_root(flow, state, output_root) / _document_path(doc, main_entry_id)


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
                _is_metadata_panel(region)
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
            submit_state = _input_submit_state(states, transition)
            activation = _input_activation(states, transition)
            target_state = submit_state or states[transition.to_state_id]
            target_page = next((page for page in target_state.pages if page.page_var == transition.target_page_var), None)
            if target_page is None:
                continue
            target_root = _state_root(flow, target_state, output_root)
            target_main_page = next((page for page in target_state.pages if page.page_var == "page"), target_state.pages[0])
            target_path = target_root / _document_path(
                next(document for document in target_state.documents if document.document_id == target_page.entry_document_id),
                target_main_page.entry_document_id,
            )
            mode = "input" if submit_state else ("input_activate" if activation else transition.mode)
            transitions[transition.action_id] = {
                "mode": mode,
                "target_path": target_path,
                "windowName": target_page.window_name or "replica-popup",
                "inputId": activation.dom.attributes.get("id", "") if activation and activation.dom else "",
            }
        # Map each branch's Viewer state to its series_key so rendering can flag
        # the active option in that state's series region(s). Only the public
        # slug (not the raw series_key, which may be a real SeriesInstanceUID)
        # is ever written into served HTML / route maps.
        series_key_by_viewer_state = {
            branch.viewer_state_id: series_key_slug(branch.series_key)
            for branch in flow.series_branches
            if branch.viewer_state_id
        }
        selected_series_key = series_key_by_viewer_state.get(state.state_id)
        branches_by_metadata_state = {
            branch.metadata_state_id: branch
            for branch in flow.series_branches
            if branch.metadata_state_id
        }
        # Compute a per-branch route map with URLs relative to a given destination.
        def _series_route_for(destination: Path) -> dict[str, dict[str, object]]:
            route: dict[str, dict[str, object]] = {}
            for branch in flow.series_branches:
                viewer_abs = (
                    _state_entry_path(flow, states[branch.viewer_state_id], output_root)
                    if branch.viewer_state_id and branch.viewer_state_id in states
                    else None
                )
                disabled = (
                    branch.capture_status not in {"captured", "partial"}
                    or branch.viewer_state_id is None
                    or branch.viewer_state_id not in states
                )
                entry: dict[str, object] = {"disabled": disabled}
                if viewer_abs:
                    entry["viewerUrl"] = _relative_url(destination, viewer_abs)
                if branch.metadata_state_id and branch.metadata_state_id in states:
                    metadata_abs = _state_entry_path(flow, states[branch.metadata_state_id], output_root)
                    entry["metadataUrl"] = _relative_url(destination, metadata_abs)
                route[series_key_slug(branch.series_key)] = entry
            return route

        # Resolve the Metadata close target. For a series-branch Metadata state,
        # return *explicitly* to the branch's recorded return state (never inferred
        # from ordinal ordering). Plain recorded (non-branch) Metadata keeps the
        # legacy ordinal predecessor fallback.
        branch_metadata_return = None
        branch = branches_by_metadata_state.get(state.state_id)
        if branch is not None and branch.return_state_id and branch.return_state_id in states:
            branch_metadata_return = _state_entry_path(flow, states[branch.return_state_id], output_root)
        for document in state.documents:
            children = [candidate for candidate in state.documents if candidate.parent_document_id == document.document_id]
            destination = document_paths[document.document_id]
            destination.parent.mkdir(parents=True, exist_ok=True)
            document_transitions = {
                target.action_id: {
                    "mode": str(transitions[target.action_id]["mode"]),
                    "url": _relative_url(destination, transitions[target.action_id]["target_path"]),
                    "windowName": str(transitions[target.action_id]["windowName"]),
                    "inputId": str(transitions[target.action_id].get("inputId", "")),
                }
                for target in document.targets
                if target.action_id in transitions
            }
            back_abs = None
            if state.state_id != flow.entry_state_id and branch_metadata_return is not None:
                back_abs = branch_metadata_return
            elif state.state_id != flow.entry_state_id:
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
            series_route = _series_route_for(destination)
            # A branch's source_member_id is a stable member identifier that
            # recurs across every document's series region (hub and all viewer
            # states). Route-key any member whose id matches a branch's source
            # member, in whichever document it appears.
            series_key_by_member = {
                branch.source_member_id: series_key_slug(branch.series_key)
                for branch in flow.series_branches
                if branch.source_member_id
            }
            # (P0#3) Additionally route by the member's own stable semantic
            # identity so any document's series list (recorded hub or branch
            # Viewer) binds to a branch without relying on cross-snapshot member
            # id equality. Only the public slug is emitted into served HTML.
            below_threshold_status = {"captured", "partial"}
            series_route_by_identity = {
                branch.series_key: {
                    "slug": series_key_slug(branch.series_key),
                    "disabled": branch.capture_status not in below_threshold_status
                    or branch.viewer_state_id is None
                    or branch.viewer_state_id not in states,
                }
                for branch in flow.series_branches
                if branch.series_key
            }
            destination.write_text(_render_document(
                document,
                children,
                output_root,
                destination,
                document_paths,
                asset_paths[(state.state_id, document.document_id)],
                document_transitions,
                back_target if show_metadata_back else None,
                series_route=series_route if series_route else None,
                series_key_by_member=series_key_by_member or None,
                selected_series_key=selected_series_key,
                series_route_by_identity=series_route_by_identity or None,
            ), encoding="utf-8")
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
