"""Build an offline, interactive replica tree from a captured ReplicaFlow."""

from __future__ import annotations

import copy
import json
import os
import shutil
import hashlib
import html
import re
from dataclasses import asdict, replace
from pathlib import Path

from locator_risk import classify_locator_risk
from replica_models import (
    ActionTarget,
    DomNodeSnapshot,
    InteractionRegion,
    Rect,
    ReplicaDocument,
    ReplicaFlow,
    ReplicaState,
    ReplicaTransition,
)
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
  // 布局按钮是「粘性按钮」（点了还要能再点），所以必须先于 series /
  // action 命中：只换同一状态内的背景 img.replica-bg，不导航、不消失。
  const layoutEl = event.target.closest('[data-replica-layout]');
  if (layoutEl) {
    event.preventDefault();
    const layoutId = layoutEl.getAttribute('data-replica-layout');
    const layoutUrls = window.__REPLICA_LAYOUTS__ || {};
    const url = layoutUrls[layoutId];
    if (url) {
      const bg = document.querySelector('img.replica-bg');
      if (bg) bg.src = url;      // 只换背景，保持当前 series 热区与布局系列解耦
    }
    return;
  }
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
    below_fold: bool = False,
    dy: float = 0.0,
    clip_rect: "Rect | None" = None,
) -> str:
    """Attach a series route key with accessible option semantics to a member node.

    ``below_fold`` marks rows whose absolute rect starts below the captured
    viewport fold: the static screenshot only covers the fold, so those rows have
    no pixel background and must render their own DOM content (icon + label)
    instead of staying a transparent hit-target (see the CSS rule
    ``[data-replica-below-fold]{opacity:1}`` in ``_render_document``).

    ``dy`` rebases an absolute page rect into a scroll container's local
    coordinates (tall-list mode moves the rows inside the series panel's own
    scrolling region, so each row's ``top`` becomes ``rect.y - dy``).

    ``clip_rect`` (步骤5 去重叠) trims the row's hit area so an overlapping
    interactive layout button wins the overlap band. ``clip_rect`` must already
    be in the same coordinate space as the row's rendered rect (i.e. it should
    carry the same ``dy`` rebase the caller applies via ``top``).
    """
    top = snapshot.rect.y if dy == 0 else snapshot.rect.y - dy
    box_left = snapshot.rect.x
    box_top = top
    box_width = snapshot.rect.width
    box_height = snapshot.rect.height
    if clip_rect is not None:
        # 勤俭裁剪：只剔除重叠带，序列项其余命中区保留。
        x0 = float(clip_rect.x)
        y0 = float(clip_rect.y)
        x1 = x0 + float(clip_rect.width)
        y1 = y0 + float(clip_rect.height)
        hit_x0, hit_y0 = float(box_left), float(box_top)
        hit_x1, hit_y1 = hit_x0 + float(box_width), hit_y0 + float(box_height)
        ix0, iy0 = max(hit_x0, x0), max(hit_y0, y0)
        ix1, iy1 = min(hit_x1, x1), min(hit_y1, y1)
        if ix1 > ix0 and iy1 > iy0:
            # 交集在垂直方向的位置决定剪顶部还是底部：顶部重叠带把 top 下移，
            # 底部重叠带把 height 缩短。水平方向不剪（布局按钮通常只占一小段）。
            if iy0 - hit_y0 <= hit_y1 - iy1:
                hit_y0 = iy1
            else:
                hit_y1 = iy0
        box_top = hit_y0
        box_height = max(0.0, hit_y1 - hit_y0)
    style = (
        f"position:absolute;left:{box_left}px;top:{box_top}px;"
        f"width:{box_width}px;height:{box_height}px;"
    )
    attributes = (
        f' data-replica-overlay="" data-replica-series-key="{html.escape(series_key, quote=True)}"'
        f' style="{style}"'
    )
    if below_fold:
        attributes += ' data-replica-below-fold=""'
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


def _normalize_layout_id(text: str) -> str | None:
    """Normalize a layout spec to the canonical ``a*b`` form.

    ``1x1`` / ``1 X 1`` / ``1*1`` all collapse to ``1*1`` so the captured member
    text can be matched against ``layout_variants`` keys regardless of the
    separator the viewer's CSS class names / text happen to use.
    """
    match = re.match(r"\s*(\d+)\s*[*xX]\s*(\d+)\s*$", text)
    if not match:
        return None
    return f"{match.group(1)}*{match.group(2)}"


def _infer_layout_id(dom: DomNodeSnapshot) -> str | None:
    """Infer a layout variant id from a layout region member's DOM.

    Accepts several real-world spellings and collapses them to ``a*b``:

    - ``2*2`` / ``2x2`` / ``2 X 2`` (text or element id)
    - ``layout_1_1`` / ``layout-1-1`` (underscore/hyphen separated digits in id)
    - ``*1 Shift+1`` (zscloud records the 1x1 shortcut with a leading star as
      the member's visible text -- still layout 1x1)

    Falls back to ``None`` when the member is a plain icon / irregularly named
    and should stay purely decorative.
    """
    sources = [
        dom.text or "",
        dom.attributes.get("id") or "",
    ]
    norm_sources = [source.replace("-", " ").replace("_", " ") for source in sources]
    for source in sources + norm_sources:
        # ``*1 Shift+1`` 风格：星号前导的单个数字 → 正方形布局 N*N。
        match = re.search(r"^\s*[*xX]\s*(\d+)", source)
        if match:
            return f"{match.group(1)}*{match.group(1)}"
        # ``2*2`` / ``2x2`` / ``layout 1 1`` → a*b。
        match = re.search(r"(\d+)\s*[*xX]\s*(\d+)", source)
        if match:
            return f"{match.group(1)}*{match.group(2)}"
        # 下划线/连字符分隔（``layout_1_1`` 已在 norm_sources 里变空格）：
        # 两个相邻数字视为行列（1 1 → 1*1）。
        match = re.search(r"(\d+)\s+(\d+)\b", source)
        if match and source.startswith("layout"):
            return f"{match.group(1)}*{match.group(2)}"
    return None


def _layout_member_html(
    snapshot: DomNodeSnapshot,
    variant_id: str,
    disabled: bool = False,
) -> str:
    """Render a layout region member as a clickable background-variant switcher.

    Clicking it swaps only ``img.replica-bg`` (via the RUNTIME
    ``data-replica-layout`` branch — no navigation), keeping layout selection
    decoupled from series routing. ``disabled`` members (variant inferred but no
    captured background) render as inert ``aria-disabled`` options.
    """
    attributes = ' data-replica-overlay=""'
    attributes += f' data-replica-layout="{variant_id}"'
    if disabled:
        attributes += ' aria-disabled="true"'
    role_attr = ' role="option"'
    if ' role=' not in snapshot.outer_html and ' role=' not in " ".join(snapshot.attributes):
        attributes += role_attr
    style = (
        f"position:absolute;left:{snapshot.rect.x}px;top:{snapshot.rect.y}px;"
        f"width:{snapshot.rect.width}px;height:{snapshot.rect.height}px;"
    )
    return snapshot.outer_html.replace(
        f"<{snapshot.tag_name}", f"<{snapshot.tag_name}{attributes} style=\"{style}\"", 1
    )


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
    series_list_asset: str | None = None,
    menu_strip: dict[str, object] | None = None,
    layout_variants: dict[str, str] | None = None,
) -> str:
    asset = _relative_url(destination, output_root / asset_path)
    viewport_h = float(document.viewport["height"])
    # A series list that scrolls in the real viewer is captured in scrolling
    # content coordinates, so its rows below the captured fold sit at content
    # y beyond the screenshot height.
    series_extent = 0.0
    series_region = None
    for region in document.regions:
        if region.region_type != "series":
            continue
        if series_region is None:
            series_region = region
        for member in region.members:
            member_rect = member.dom.rect
            series_extent = max(series_extent, float(member_rect.y) + float(member_rect.height))
    # Real-viewer scroll scoping: only the series panel scrolls; the report /
    # viewer background stays pinned. With a full-content list capture we build a
    # dedicated scroll region at the recorded panel rect (origin + the panel's
    # on-screen height), inside which the tall panel background and the rows move
    # together. Without one we fall back to a scrollable overlay whose below-fold
    # rows render their own DOM content over a fixed screenshot.
    has_tall_list = bool(series_list_asset) and series_region is not None
    tall_scroll = has_tall_list and series_extent > viewport_h + 1.0
    series_overflow = (not has_tall_list) and series_extent > viewport_h + 1.0
    series_origin_x = 0.0
    series_origin_y = 0.0
    series_pane_w = 0.0
    series_pane_h = 0.0
    series_view_h = 0.0
    if tall_scroll:
        root_rect = series_region.root.rect
        series_origin_x = float(root_rect.x)
        series_origin_y = float(root_rect.y)
        series_pane_w = float(root_rect.width)
        # The scroll-stitched panel image (content_height) is the container's full
        # scrollHeight and can carry header/whitespace above and below the rows;
        # the *rows'* extent is what should drive how far the panel can scroll so
        # the last row reaches the bottom of the on-screen window when the user
        # scrolls to the end (real FT behavior).
        series_pane_h = max(0.0, series_extent - series_origin_y)
        series_view_h = viewport_h - series_origin_y
        if series_view_h <= 1.0 or series_pane_h <= series_view_h + 1.0:
            # Nothing actually scrolls; degrade to the fixed-page rendering.
            tall_scroll = False
            series_origin_x = series_origin_y = series_pane_w = series_pane_h = series_view_h = 0.0
    parts = [
        "<!doctype html><html><head><meta charset=\"utf-8\"><title>Replica</title>",
        "<style>"
        "html,body{margin:0;width:100%;height:100%;overflow:hidden;background:rgb(3,6,9)}"
        "body{position:relative}"
        ".replica{position:absolute;overflow:hidden;transform-origin:top left}"
        ".replica-bg{display:block;width:100%;height:100%;object-fit:fill}"
        ".overlay{position:absolute;inset:0}.overlay>*{box-sizing:border-box}"
        ".overlay>[data-replica-overlay]{opacity:0}.overlay>[data-replica-action]{z-index:1}.overlay>[data-replica-series-key]{z-index:2}.overlay>[data-replica-series-key][data-replica-below-fold]{opacity:1}"
        "/* 布局按钮 z-index:3 高于 series-key(2)：重叠区命中优先布局（方案 A）"
        "布局与序列解耦：同状态内换背景，不导航。data-replica-visible 同为 3，"
        "两者上一状态不同时出现，不冲突。 */"
        ".overlay>[data-replica-layout]{z-index:3;cursor:pointer;border-radius:3px}"
        ".overlay>[data-replica-layout]:hover{outline:2px solid rgba(120,170,255,.5);outline-offset:-2px}"
        ".overlay>[data-replica-layout][aria-disabled=\"true\"]{cursor:not-allowed;opacity:.45;filter:grayscale(.6);pointer-events:none}"
        "/* Tall-list mode: only the series panel scrolls; its rows scroll as"
        "rendered DOM content (no moving background image), matching the real FT"
        "panel where scrolling moves the list entries, not the panel chrome. */"
        ".overlay .series-scroll [data-replica-series-key]{pointer-events:auto}"
        ".overlay>[data-replica-visible]{opacity:1;z-index:3;background:rgb(20,25,33);border:1px solid rgb(44,52,63);"
        "border-radius:4px;color:rgb(209,228,255);display:flex;align-items:center;justify-content:center;"
        "font:13px/1.4 'Helvetica Neue',Helvetica,sans-serif;cursor:pointer;box-shadow:0 8px 20px rgba(0,0,0,.55)}"
        ".overlay>[data-replica-overlay]:not([data-replica-layout]):not([data-replica-action]):not([data-replica-input]):not([data-replica-series-key]):not([role]):not(button):not(input):not(select):not(textarea):not(canvas):not(a){pointer-events:none}"
        ".overlay>[data-replica-layout],.overlay>[data-replica-action],.overlay>[data-replica-input],.overlay>[data-replica-series-key],.overlay>[data-replica-overlay][role],.overlay>[data-replica-overlay][data-testid],.overlay>[data-replica-overlay]button,.overlay>[data-replica-overlay]input,.overlay>[data-replica-overlay]select,.overlay>[data-replica-overlay]textarea,.overlay>[data-replica-overlay]canvas,.overlay>[data-replica-overlay]a{pointer-events:auto}"
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
        (
            # The page itself never scrolls in the replica: the report/viewer
            # background stays pinned, exactly like the real FT viewer. Only the
            # series panel (its own .series-scroll region) or, in fallback mode,
            # the whole overlay scrolls.
            f'<body><main class="replica" data-viewport-width="{document.viewport["width"]}" '
            f'data-viewport-height="{document.viewport["height"]}" '
            f'style="width:{document.viewport["width"]}px;height:{viewport_h:.0f}px">'
        ),
        (
            f'<img class="replica-bg" src="{asset}" alt="Captured visual state">'
        ),
        (
            '<section class="overlay" style="overflow-y:auto;max-height:'
            f'{viewport_h:.0f}px;height:{series_extent:.0f}px;'
            'overscroll-behavior:contain;scrollbar-gutter:stable">'
            if series_overflow else '<section class="overlay">'
        ),
        (
            f'<img class="series-pane-bg" src="{series_list_asset}" '
            f'style="position:absolute;left:{series_region.root.rect.x}px;'
            f'top:{series_region.root.rect.y}px;width:{series_region.root.rect.width}px;'
            f'height:{document.series_list_content_height}px;pointer-events:none;">'
            if (has_tall_list and not tall_scroll) else ""
        ),
        (
            # Real 更多 menu row (tool-button strip) captured from the recorded
            # menu-open snapshot, painted over the fixed viewer background for the
            # synthetic btags intermediate state. Purely visual; the clickable
            # Tags button is a separate transparent hit layer on top.
            f'<img class="replica-menu-strip" src="{_relative_url(destination, output_root / menu_strip["relpath"])}" '
            f'style="position:absolute;left:{menu_strip["left"]}px;top:{menu_strip["top"]}px;'
            f'width:{menu_strip["width"]}px;height:{menu_strip["height"]}px;pointer-events:none;">'
            if menu_strip else ""
        ),
    ]
    # 布局可点成员的 rect 与布局字典（第一趟预算，先于成员渲染）：供 series 热区
    # 裁剪使用——重叠带命中优先布局按钮（z-index:3 > series-key:2 的双保险）。
    # 所有 key 统一 normalize 成 ``a*b`` 形式，与 _infer_layout_id 的输出一致，
    # 保证 __REPLICA_LAYOUTS__ 的键、data-replica-layout 的取值、layout 区域成员
    # 的映射三点对齐（兼容 ``1*1``/``1x1``/``layout_1_1`` 等任意来源）。
    layout_lookup: dict[str, str] = {}
    clip_layout_rects: list[Rect] = []
    if layout_variants:
        for raw_key, url in layout_variants.items():
            normalized = _normalize_layout_id(raw_key) or _normalize_layout_id(str(raw_key).replace("_", " "))
            if normalized:
                layout_lookup[normalized] = url
        # 方案 A「背景层替换」：同状态下注入布局字典（normalized key → 相对 URL），
        # 点击布局按钮只改 img.replica-bg 的 src，不产生新状态、不改变路由。
        parts.append(
            f"<script>window.__REPLICA_LAYOUTS__={json.dumps(layout_lookup, ensure_ascii=False)};</script>"
        )
        for region in document.regions:
            if region.region_type != "layout":
                continue
            for member in region.members:
                variant_id = _infer_layout_id(member.dom)
                if variant_id is not None and _normalize_layout_id(variant_id) in layout_lookup:
                    clip_layout_rects.append(member.dom.rect)
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
    series_chunks: list[str] = []
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
            is_series_region = region.region_type == "series"
            is_layout_region = region.region_type == "layout"
            if series_key is not None:
                # 步骤5 去重叠：series 热区裁剪，避免与可交互布局按钮重叠；只裁
                # 本行与任一布局可点成员相交的重叠带（顶部下移 / 底部缩短）。
                clip_rect = None
                if clip_layout_rects:
                    item_top = member.dom.rect.y
                    item_bottom = float(member.dom.rect.y) + float(member.dom.rect.height)
                    for laid in clip_layout_rects:
                        lo = float(laid.y)
                        hi = float(laid.y) + float(laid.height)
                        if (
                            float(laid.x) < float(member.dom.rect.x) + float(member.dom.rect.width)
                            and float(laid.x) + float(laid.width) > float(member.dom.rect.x)
                            and lo < item_bottom
                            and hi > item_top
                        ):
                            rebased = Rect(
                                float(laid.x), float(laid.y) - (series_origin_y if tall_scroll and is_series_region else 0.0),
                                float(laid.width), float(laid.height), laid.coordinate_space,
                            )
                            clip_rect = rebased
                            break
                route = (series_route or {}).get(series_key, {})
                member_markup = _series_member_html(
                    member.dom,
                    series_key,
                    selected=bool(series_key == selected_series_key),
                    disabled=disabled_route or bool(route.get("disabled")),
                    below_fold=bool(member.dom.rect.y >= viewport_h),
                    dy=(series_origin_y if tall_scroll and is_series_region else 0.0),
                    clip_rect=clip_rect,
                )
                member_markup = _redact_known_series_identities(
                    member_markup, series_route_by_identity
                )
            elif is_layout_region:
                # 兼容性「默认关闭」：没有本页布局字典（__REPLICA_LAYOUTS__ 未注入）
                # 时，layout region 成员一律走纯装饰（老 run 行为不变）。只有方案 A
                # 落地（layout_lookup 非空）才做三态化。
                if not layout_lookup:
                    positioned = _positioned_html(member.dom)
                    member_markup = _redact_known_series_identities(
                        positioned, series_route_by_identity
                    )
                    parts.append(member_markup)
                    rendered_nodes.add(member_key)
                    if member.dom.attributes.get("id"):
                        rendered_element_ids.add(member.dom.attributes["id"])
                    continue
                # 步骤4 布局 region 全部成员三态化：
                #   可点 —— variant 可推出且存在对应布局背景 → data-replica-layout
                #   disabled —— variant 可推出但无背景 → aria-disabled（不假装可点）
                #   纯装饰 —— variant 推不出（命名不规则/纯图标）→ 保持 data-replica-overlay
                variant_id = _infer_layout_id(member.dom)
                normalized = _normalize_layout_id(variant_id) if variant_id else None
                if normalized is not None and normalized in layout_lookup:
                    member_markup = _layout_member_html(member.dom, normalized)
                elif normalized is not None:
                    member_markup = _layout_member_html(member.dom, normalized, disabled=True)
                else:
                    member_markup = _positioned_html(member.dom)
                member_markup = _redact_known_series_identities(
                    member_markup, series_route_by_identity
                )
            else:
                positioned = _positioned_html(member.dom)
                if is_series_region:
                    positioned = _redact_series_snapshot_markup(member.dom, positioned)
                member_markup = _redact_known_series_identities(
                    positioned, series_route_by_identity
                )
            if tall_scroll and is_series_region:
                series_chunks.append(member_markup)
            else:
                parts.append(member_markup)
            rendered_nodes.add(member_key)
            if member.dom.attributes.get("id"):
                rendered_element_ids.add(member.dom.attributes["id"])
    if tall_scroll and series_chunks:
        # The series panel is its own scroll region: only the list rows scroll
        # as live DOM content (no moving background strip), the report
        # background stays pinned. Rows are visible from the start so the list
        # reads like the real panel (scrolling repositions the entries).
        parts.append(
            f'<div class="series-scroll" style="position:absolute;'
            f'left:{series_origin_x:.0f}px;top:{series_origin_y:.0f}px;'
            f'width:{series_pane_w:.0f}px;height:{series_view_h:.0f}px;'
            f'overflow-y:auto;overscroll-behavior:contain;scrollbar-gutter:stable">'
            f'<div class="series-content" style="position:relative;height:{series_pane_h:.0f}px">'
        )
        parts.extend(series_chunks)
        parts.append("</div></div>")
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


# A full-content (real record) 更多 menu-open snapshot shows the revealed tool
# button row. On FT the whole row sits in the top-right band x in [~844, 1504)
# with the Tags button at its right end (measured diffs of before/after
# snapshots). The strip window is FT-specific by design; other viewers without
# such a row simply keep the plain per-branch Tags button below.
_MENU_STRIP_X0 = 844  # measured left edge of the revealed FT tool-button row

def _find_real_tags_menu_source(
    flow: ReplicaFlow, source_root: Path
) -> dict[str, object] | None:
    """Locate the recorded (non-synthetic) 更多 menu-open document.

    Returns the captured ``tool-tags`` button (its real rect + DOM, the only
    sibling we actually route through) and the snapshot that visually contains
    the whole revealed tool-button row. ``None`` when no such capture exists
    (synthetic fixtures / older runs), which degrades ``_augment_meta_two_step``.
    """
    for state in flow.states:
        for document in state.documents or []:
            if not document.screenshot_asset_relpath:
                continue
            for region in document.regions or []:
                if region.region_type != "metadata" or region.root is None:
                    continue
                root = region.root
                klass = " ".join((root.attributes.get("class", ""), root.attributes.get("id", ""))).lower()
                if "tool-tags" not in klass or any(
                    token in klass for token in ("tagsbox", "box-tags", "dicom", "metadata")
                ):
                    continue
                return {
                    "rect": root.rect,
                    "outer_html": root.outer_html,
                    "screenshot_relpath": document.screenshot_asset_relpath,
                }
    return None


def _make_menu_strip(
    tags_menu: dict[str, object], source_root: Path, output_root: Path
) -> dict[str, object] | None:
    """Crop the real tool-button row out of the menu-open snapshot as an asset.

    The strip is painted under the synthetic Tags button so the intermediate
    state shows the recorded one-row toolbar with Tags at its right end instead
    of a lone floating label (P-sibling: only Tags is routed to the Metadata
    state; the rest of the row is inert pixels). Returns ``None`` to degrade if
    the source image cannot be opened.
    """
    from PIL import Image  # build-time only; availability is runtime-independent

    src = source_root / str(tags_menu["screenshot_relpath"])
    if not src.exists():
        jpeg = src.with_suffix(".jpeg")
        if jpeg.exists():
            src = jpeg
        else:
            return None
    rect = tags_menu["rect"]
    try:
        im = Image.open(src).convert("RGB")
    except Exception:  # noqa: BLE001 - degrade to the plain Tags button
        return None
    width, height = im.size
    if rect.x < width * 0.5:  # Tags not near the right edge: don't guess the row
        return None
    x0 = min(_MENU_STRIP_X0, max(0, width - 1))
    left = min(int(rect.x) - 4, max(x0, 0))
    top = max(0, int(rect.y) - 2)
    right = width
    bottom = min(height, int(rect.y) + int(rect.height) + 2)
    if right - left <= 8 or bottom - top <= 4:
        return None
    strip = im.crop((left, top, right, bottom))
    import io
    buf = io.BytesIO()
    try:
        strip.save(buf, format="JPEG", quality=88)
        payload = buf.getvalue()
    except Exception:  # noqa: BLE001 - JPEG unsupported degrades gracefully
        return None
    digest = hashlib.sha256(payload).hexdigest()[:16]
    relpath = f"assets/by-hash/menu_strip_{digest}.jpeg"
    dest = output_root / relpath
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists():
        dest.write_bytes(payload)
    return {
        "relpath": relpath,
        "left": left,
        "top": top,
        "width": right - left,
        "height": bottom - top,
    }


def _augment_meta_two_step(
    flow: ReplicaFlow,
    states: dict[str, ReplicaState],
    tags_menu: dict[str, object] | None = None,
    menu_strip: dict[str, object] | None = None,
) -> None:
    """Restore the recorded 更多 -> Tags two-step Metadata open on offline pages.

    Two fidelity gaps make the replica diverge from the real page:

    - The recorded main-path Tags click can carry a transition but no rendered
      DOM element (its snapshot was taken too late / collapsed), leaving the
      "Tags" step a dead end. Synthesize a clickable Tags row there.
    - Branch Metadata is collapsed by ``_synthetic_meta_open_target`` into a
      single ``series:<branch>:meta_open`` action jumping straight to the branch
      Metadata state. Insert a per-branch intermediate ``btags_<branch>`` state
      that reuses the branch's own Viewer document + screenshot and adds a
      clickable "Tags" row; route the Viewer's 更多 through it, so opening
      Metadata observes the real two-step interaction.

    ``tags_menu`` / ``menu_strip`` (from ``_find_real_tags_menu_source`` /
    ``_make_menu_strip``) restore the real one-row toolbar: the Tags button is
    placed at its captured rect with its real ``tool tool-tags`` markup, and the
    recorded button-row pixels are painted in as the intermediate state.
    """
    fallback_rect = Rect(1264, 44, 96, 34, "page_viewport_css")

    def tags_rect_for(doc: ReplicaDocument) -> Rect:
        for region in doc.regions:
            if region.region_type == "metadata" and region.root.rect is not None:
                return region.root.rect
        return fallback_rect

    def synthetic_tags_target(action_id: str, doc: ReplicaDocument) -> ActionTarget:
        if tags_menu is not None:
            # Real toolbar button: exact rect + markup, kept as a transparent
            # hit-layer (pixels come from the menu strip under it).
            rect = tags_menu["rect"]
            dom = DomNodeSnapshot(
                "a", "Tags",
                {"class": "tool tool-tags", "title": "Tags", "data-tool": "tags"},
                rect, str(tags_menu["outer_html"]), {},
            )
            return ActionTarget(
                action_id, "m_tags", "click", "locator", {},
                None, dom, None, None, None, "execute", None, doc.document_id, None,
            )
        rect = tags_rect_for(doc)
        dom = DomNodeSnapshot(
            "a", "Tags", {"data-replica-visible": ""},
            rect, '<a data-replica-visible="">Tags</a>', {},
        )
        return ActionTarget(
            action_id, "m_tags", "click", "locator", {},
            None, dom, None, None, None, "execute", None, doc.document_id, None,
        )

    metadata_state_ids = {
        state.state_id
        for state in flow.states
        if any(_is_metadata_panel(region) for doc in state.documents for region in doc.regions)
    }
    rendered_action_ids_by_state = {
        state.state_id: {target.action_id for doc in state.documents for target in (doc.targets or [])}
        for state in flow.states
    }

    # 1) Main path: give a recorded-but-unrendered Metadata step a clicked element.
    for state in flow.states:
        if state.state_id in metadata_state_ids or not state.documents:
            continue
        for transition in state.transitions:
            if transition.to_state_id not in metadata_state_ids:
                continue
            if transition.action_id in rendered_action_ids_by_state[state.state_id]:
                continue
            doc = state.documents[0]
            doc.targets = list(doc.targets or []) + [synthetic_tags_target(transition.action_id, doc)]

    # 2) Branch viewers: synthesize the intermediate Tags-menu state per branch.
    for branch in flow.series_branches:
        if branch.viewer_state_id not in states or branch.metadata_state_id not in states:
            continue
        viewer_state = states[branch.viewer_state_id]
        # Only the expansion's synthetic one-step collapse needs re-splitting; a
        # branch whose Metadata open is driven by ordinary recorded actions stays
        # untouched (its own transitions already encode the real steps).
        synthetic_open = next(
            (
                transition for transition in viewer_state.transitions
                if transition.action_id == f"series:{branch.branch_id}:meta_open"
            ),
            None,
        )
        if synthetic_open is None:
            continue
        src_page = next(
            (p for p in viewer_state.pages if p.page_var == viewer_state.active_page_var),
            viewer_state.pages[0] if viewer_state.pages else None,
        )
        if src_page is None:
            continue
        viewer_doc = next(
            (d for d in viewer_state.documents if d.document_id == src_page.entry_document_id),
            viewer_state.documents[0] if viewer_state.documents else None,
        )
        if viewer_doc is None:
            continue
        btags_id = f"btags_{branch.branch_id}"
        # In the real menu-open state 更多 is highlighted (active). The branch
        # viewer snapshot predates the click, so mark the copied target active
        # without mutating the viewer state's own target.
        branch_targets = []
        for viewer_target in list(viewer_doc.targets or []):
            if (
                viewer_target.action_id == f"series:{branch.branch_id}:meta_open"
                and viewer_target.dom is not None
                and "tool-more" in viewer_target.dom.outer_html
                and "active" not in viewer_target.dom.outer_html
            ):
                viewer_target = replace(
                    viewer_target,
                    dom=replace(
                        viewer_target.dom,
                        outer_html=viewer_target.dom.outer_html.replace(
                            "tool-more", "tool-more active", 1
                        ),
                    ),
                )
            branch_targets.append(viewer_target)
        tags_doc = replace(
            viewer_doc,
            document_id=f"{btags_id}__doc",
            targets=branch_targets
            + [synthetic_tags_target(f"series:{branch.branch_id}:tags", viewer_doc)],
        )
        pages = [replace(page, entry_document_id=tags_doc.document_id) for page in viewer_state.pages]
        btags_state = ReplicaState(
            btags_id,
            viewer_state.ordinal,
            viewer_state.source_url,
            viewer_state.active_page_var,
            pages,
            [tags_doc],
            [ReplicaTransition(
                f"t_tags_{branch.branch_id}", f"series:{branch.branch_id}:tags",
                btags_id, branch.metadata_state_id, "page", "page", "same_page",
            )],
            copy.copy(viewer_state.evidence),
        )
        states[btags_id] = btags_state
        flow.states.append(btags_state)
        # Re-point the Viewer's 更多 (meta_open) through the Tags step.
        for transition in viewer_state.transitions:
            if (
                transition.action_id == f"series:{branch.branch_id}:meta_open"
                and transition.to_state_id == branch.metadata_state_id
            ):
                transition.to_state_id = btags_id


def _promote_series_regions_to_earliest_documents(flow: ReplicaFlow) -> int:
    """Mirror the recorded series list onto every main-path state that owns the
    same viewer document and has no series region yet.

    A viewer whose series click lands late in the recording (e.g. zscloud's
    popup viewer: the recorded dblclick happens at s_004, after s_001-s_003)
    only ends up with a ``series`` InteractionRegion in the later states, so the
    replica's *entry* viewer exposes no clickable series list even though every
    branch was captured. FT (series click early) already carries the list in its
    entry viewer document. Promoting a main-path series region to *every* main
    path state that owns the same ``document_id`` without a series region (not
    just the earliest one, 步骤3) makes any replica with captured branches show
    the clickable list from entry through all intermediate layout states
    (s_001/s_002/s_003). Branch states (``bviewer_``/``bmeta_``/``btags_``) are
    never sources or targets: their series regions already render in each branch
    viewer. Returns the number of documents promoted.
    """
    branch_state_ids = {
        state_id
        for branch in flow.series_branches
        for state_id in (branch.viewer_state_id, branch.metadata_state_id, branch.return_state_id)
        if state_id
    }
    main_states = sorted(
        (state for state in flow.states if state.state_id not in branch_state_ids),
        key=lambda state: state.ordinal,
    )
    first_series: dict[str, tuple[ReplicaState, InteractionRegion]] = {}
    for state in main_states:
        for document in state.documents:
            if document.document_id in first_series:
                continue
            series_region = next(
                (region for region in document.regions if region.region_type == "series"),
                None,
            )
            if series_region is not None:
                first_series[document.document_id] = (state, series_region)
    promoted = 0
    for document_id, (source_state, series_region) in first_series.items():
        for target_state in main_states:
            if target_state is source_state:
                continue
            target_document = next(
                (document for document in target_state.documents if document.document_id == document_id),
                None,
            )
            if target_document is None:
                continue
            if any(region.region_type == "series" for region in target_document.regions):
                continue
            # Deep-copy so the promoted overlay never mutates the captured source
            # region (which keeps rendering in its own later states).
            target_document.regions.append(copy.deepcopy(series_region))
            promoted += 1
    return promoted


def _reroute_branch_series_regions_to_viewer_documents(flow: ReplicaFlow) -> int:
    """Re-home branch-viewer series regions that landed on the *outer* document.

    ``_capture_viewer_topology`` appends each branch's captured series region to
    the first topology document (``docs_out[0]``). For a popup-style viewer whose
    main page is a shell/share page and whose viewer lives in a ``page1`` iframe
    (e.g. zscloud's Dapeng), that first document is the main page, so the region
    renders onto the share-page background nobody visits while the viewer
    iframe document the user actually reaches exposes no clickable list (the
    mirror image of the main path, where the list correctly sits on
    ``d_p_001_f_001``). This migrates each branch's series region onto the
    branch document whose leaf id matches a main-path series document, so the
    branch viewer becomes clickable again. Only branch-viewer states are
    sources; main-path and synthetic metadata/Tags states are never touched.
    Returns the number of regions re-homed.
    """
    branch_state_ids = {
        state_id
        for branch in flow.series_branches
        for state_id in (branch.viewer_state_id, branch.metadata_state_id, branch.return_state_id)
        if state_id
    }
    main_series_leaf_ids: set[str] = {
        document.document_id.rsplit("__", 1)[-1]
        for state in flow.states
        if state.state_id not in branch_state_ids
        for document in state.documents
        if any(region.region_type == "series" for region in document.regions)
    }
    if not main_series_leaf_ids:
        return 0
    rerouted = 0
    for branch in flow.series_branches:
        if not branch.viewer_state_id:
            continue
        state = next(
            (candidate for candidate in flow.states if candidate.state_id == branch.viewer_state_id),
            None,
        )
        if state is None:
            continue
        for document in state.documents:
            for region in list(document.regions):
                if region.region_type != "series":
                    continue
                if document.document_id.rsplit("__", 1)[-1] in main_series_leaf_ids:
                    # Already on a viewer document matched by the main path.
                    continue
                target = next(
                    (
                        candidate
                        for candidate in state.documents
                        if candidate.document_id != document.document_id
                        and candidate.document_id.rsplit("__", 1)[-1] in main_series_leaf_ids
                    ),
                    None,
                )
                if target is None:
                    continue
                document.regions.remove(region)
                region.document_id = target.document_id
                target.regions.append(region)
                rerouted += 1
    return rerouted


def _propagate_layout_variants_across_documents(flow: ReplicaFlow) -> int:
    """Share a captured document's layout variants with every state that owns
    the same viewer document, so the entry/popup viewer (which precedes the
    recorded layout-marker state) still shows clickable layout buttons.

    Layout capture attaches ``layout_variants`` to the document in the state
    after the recorded layout action (e.g. zscloud s_002). The popup viewer the
    user actually opens is an earlier state (s_001) with the same
    ``document_id`` but no variants. Copying variants to every state owning the
    same ``document_id`` (deep-copy so no shared mutable state) makes the layout
    background-layer switch available from the very first viewer, matching series
    region promotion. Returns the number of documents backfilled.
    """
    by_document: dict[str, dict[str, str]] = {}
    # 「带有效（非全 0 rect）layout region」的首个 state document —— 浮层完整展开时
    # 成员 rect 才非 0；早期状态（布局 marker 前）浮层未展开，成员 rect 全 0，
    # 渲染成 (0,0,0,0) 挤在角落 → 布局按钮不可点（zscloud s_001 vs s_002）。
    layout_region_by_document: dict[str, InteractionRegion] = {}
    for state in flow.states:
        for document in state.documents:
            if document.layout_variants:
                by_document[document.document_id] = dict(document.layout_variants)
            if document.document_id in layout_region_by_document:
                continue
            layout_region = next(
                (region for region in document.regions if region.region_type == "layout"),
                None,
            )
            if layout_region is None:
                continue
            # 有效 = 至少一个「布局选项成员」（可推断 variant）rect 非 0；按钮本体
            # (butt-cellStyleN_N, 40x40) 不能算 —— 浮层未展开时它也在但选项全 0。
            if any(
                (member.dom.rect.width > 0 and member.dom.rect.height > 0)
                and _infer_layout_id(member.dom) is not None
                for member in layout_region.members
            ):
                layout_region_by_document[document.document_id] = layout_region
    propagated = 0
    if not (by_document or layout_region_by_document):
        return 0
    for state in flow.states:
        for document in state.documents:
            variants = by_document.get(document.document_id)
            if variants and not document.layout_variants:
                document.layout_variants = dict(variants)
                propagated += 1
            # 早期状态的 layout region 若「没有任何可推断且 rect 非 0 的布局选项成员」
            # （浮层未展开），用后续状态的完整 region 深拷贝替换，让布局按钮在正确
            # 坐标渲染（zscloud s_001：选项 rect 全 0 → 挤在左上角不可点）。
            target_region = next(
                (region for region in document.regions if region.region_type == "layout"),
                None,
            )
            if target_region is not None and not any(
                (member.dom.rect.width > 0 and member.dom.rect.height > 0)
                and _infer_layout_id(member.dom) is not None
                for member in target_region.members
            ):
                source_region = layout_region_by_document.get(document.document_id)
                if source_region is None or source_region is target_region:
                    continue
                index = next(
                    (i for i, region in enumerate(document.regions) if region is target_region),
                    None,
                )
                if index is not None:
                    document.regions[index] = copy.deepcopy(source_region)
                    propagated += 1
    return propagated


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
    # Route every captured series list onto the earliest state owning the same
    # viewer document, so the entry viewer is clickable from the start (see
    # _promote_series_regions_to_earliest_documents). Runs before the branch
    # metadata/Tags augmentation so synthetic branch states are never the source
    # or target of a promotion, and before rendering so the promoted region
    # participates in series-key / route binding.
    _promote_series_regions_to_earliest_documents(flow)
    # Re-home branch-viewer series regions that capture attached to the outer
    # (share-page) document of a popup-style viewer, so each branch viewer the
    # user actually reaches exposes its own clickable list (zscloud Dapeng).
    # Runs after the entry promotion and before synthetic metadata/Tags states
    # are added; branch states are their only source.
    _reroute_branch_series_regions_to_viewer_documents(flow)
    # 布局变体向所有拥有同一 viewer document 的状态传播，让入口/popup viewer
    #（先于录制布局 marker 的状态）也有可点的布局按钮（同 series 提升思路）。
    # 在 asset 复制前执行，使每个 state 的变体资产都被按需复制。
    _propagate_layout_variants_across_documents(flow)
    tags_menu = _find_real_tags_menu_source(flow, source_root)
    menu_strip = _make_menu_strip(tags_menu, source_root, output_root) if tags_menu else None
    _augment_meta_two_step(flow, states, tags_menu=tags_menu, menu_strip=menu_strip)
    asset_paths: dict[tuple[str, str], Path] = {}
    series_list_asset_paths: dict[tuple[str, str], Path] = {}
    # 方案 A：layout variant -> by-hash asset Path，映射键 (state_id, doc_id)。
    layout_asset_paths: dict[tuple[str, str], dict[str, Path]] = {}
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
            if destination_asset not in copied_hashes:
                destination_asset.parent.mkdir(parents=True, exist_ok=True)
                if visual_source.exists():
                    shutil.copy2(visual_source, destination_asset)
                    total_asset_bytes += destination_asset.stat().st_size
            copied_hashes.add(destination_asset)
            if document.series_list_full_asset_relpath:
                tall_source = source_root / document.series_list_full_asset_relpath
                tall_visual = tall_source.with_suffix(".jpeg") if tall_source.with_suffix(".jpeg").exists() else tall_source
                tall_suffix = tall_visual.suffix or ".png"
                if tall_visual.exists():
                    tall_hash = sha256_file(tall_visual)
                    series_list_asset_paths[(state.state_id, document.document_id)] = Path("assets") / "by-hash" / f"{tall_hash}{tall_suffix}"
                    tall_dest = output_root / series_list_asset_paths[(state.state_id, document.document_id)]
                    if tall_dest not in copied_hashes:
                        tall_dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(tall_visual, tall_dest)
                        total_asset_bytes += tall_dest.stat().st_size
                    copied_hashes.add(tall_dest)
            # 方案 A 布局变体背景：每个 layout_variants 资产也按 by-hash 复制，
            # 供 __REPLICA_LAYOUTS__ 引用（同状态背景层替换）。
            if document.layout_variants:
                layout_variant_paths: dict[str, Path] = {}
                for variant, variant_relpath in document.layout_variants.items():
                    layout_source = source_root / variant_relpath
                    layout_visual = layout_source.with_suffix(".jpeg") if layout_source.with_suffix(".jpeg").exists() else layout_source
                    if not layout_visual.exists():
                        continue
                    layout_hash = sha256_file(layout_visual)
                    layout_rel = Path("assets") / "by-hash" / f"{layout_hash}{layout_visual.suffix or '.png'}"
                    layout_dest = output_root / layout_rel
                    if layout_dest not in copied_hashes:
                        layout_dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(layout_visual, layout_dest)
                        total_asset_bytes += layout_dest.stat().st_size
                    copied_hashes.add(layout_dest)
                    layout_variant_paths[variant] = layout_rel
                layout_asset_paths[(state.state_id, document.document_id)] = layout_variant_paths
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
            def _state_entry_abs(candidate: ReplicaState) -> Path | None:
                """Resolve a state's active entry document to an absolute path."""
                candidate_main_page = next(
                    (page for page in candidate.pages if page.page_var == "page"),
                    candidate.pages[0] if candidate.pages else None,
                )
                candidate_page = next(
                    (page for page in candidate.pages if page.page_var == candidate.active_page_var),
                    candidate_main_page,
                )
                if candidate_page is None:
                    return None
                candidate_doc = next(
                    (d for d in candidate.documents if d.document_id == candidate_page.entry_document_id),
                    candidate.documents[0] if candidate.documents else None,
                )
                if candidate_doc is None:
                    return None
                candidate_main_entry_id = candidate_main_page.entry_document_id if candidate_main_page else ""
                return _state_root(flow, candidate, output_root) / _document_path(candidate_doc, candidate_main_entry_id)

            def _ordinal_predecessor_abs(candidate: ReplicaState) -> Path | None:
                """Absolute path of the candidate's immediate ordinal predecessor."""
                ordered_by_ordinal = sorted(flow.states, key=lambda s: s.ordinal)
                position = [i for i, s in enumerate(ordered_by_ordinal) if s.state_id == candidate.state_id]
                if not position or position[0] == 0:
                    return None
                return _state_entry_abs(ordered_by_ordinal[position[0] - 1])

            # 死胡同兜底判定（步骤3）：非入口、无 out transition、仍有可交互内容
            # （series region / 布局按钮），且非 metadata / branch 状态——纯兜底，
            # 只对「无任何可点击出口」的状态注入返回入口，其余状态行为完全不变。
            state_has_exit = bool(state.transitions)
            state_has_interactive = any(
                any(region.region_type in {"series", "layout"} for region in doc.regions)
                for doc in state.documents
            )
            is_branch_state = state.state_id.startswith(("bviewer_", "bmeta_", "btags_"))
            is_dead_end = (
                state.state_id != flow.entry_state_id
                and not state_has_exit
                and state_has_interactive
                and not active_page_has_metadata
                and not is_branch_state
            )
            back_abs = None
            if state.state_id != flow.entry_state_id and branch_metadata_return is not None:
                back_abs = branch_metadata_return
            elif state.state_id != flow.entry_state_id and is_dead_end:
                # 回「前一可交互状态」：回溯序数，跳到最近一个「可交互」的状态——
                # 可交互 = 入口、或尚有出口、或带 series/layout 内容（可点列表 /
                # 布局按钮）。保证 s_003 之类的死角能回到能继续操作的状态。
                ordered_by_ordinal = sorted(flow.states, key=lambda s: s.ordinal)
                position = [i for i, s in enumerate(ordered_by_ordinal) if s.state_id == state.state_id]
                if position and position[0] > 0:
                    for candidate in reversed(ordered_by_ordinal[: position[0]]):
                        candidate_exit = bool(candidate.transitions)
                        candidate_has_content = any(
                            any(region.region_type in {"series", "layout"} for region in doc.regions)
                            for doc in candidate.documents
                        )
                        if (
                            candidate.state_id == flow.entry_state_id
                            or candidate_exit
                            or candidate_has_content
                        ):
                            back_abs = _state_entry_abs(candidate)
                            break
            elif state.state_id != flow.entry_state_id:
                # 普通中间态（含 metadata）：保持现存 ordinal 前驱回退语义。
                back_abs = _ordinal_predecessor_abs(state)
            back_target = _relative_url(destination, back_abs) if back_abs is not None else None
            show_metadata_back = (
                document.document_id == active_entry_document_id
                and active_page_has_metadata
            )
            show_dead_end_back = (
                document.document_id == active_entry_document_id
                and is_dead_end
                and back_abs is not None
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
                back_target if (show_metadata_back or show_dead_end_back) else None,
                series_route=series_route if series_route else None,
                series_key_by_member=series_key_by_member or None,
                selected_series_key=selected_series_key,
                series_route_by_identity=series_route_by_identity or None,
                series_list_asset=(
                    _relative_url(destination, output_root / series_list_asset_paths[(state.state_id, document.document_id)])
                    if (state.state_id, document.document_id) in series_list_asset_paths else None
                ),
                # Only the synthetic per-branch Tags-menu states paint the real
                # recorded tool-button row over their viewer background.
                menu_strip=(menu_strip if state.state_id.startswith("btags_") else None),
                # 方案 A：把 document.layout_variants 的资产 relpath resolve 成相对
                # destination 的 URL，供同状态背景层替换（__REPLICA_LAYOUTS__）。
                layout_variants=(
                    {
                        variant: _relative_url(
                            destination,
                            output_root / layout_asset_paths[(state.state_id, document.document_id)][variant],
                        )
                        for variant in document.layout_variants
                        if variant in layout_asset_paths.get((state.state_id, document.document_id), {})
                    }
                    if document.layout_variants else None
                ),
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
