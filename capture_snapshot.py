"""Visual-state capture helpers used by the live replica runner."""

from __future__ import annotations

import io
import hashlib
import re
import time
from pathlib import Path
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from PIL import Image, ImageChops, ImageFilter
from lxml import html

from replica_models import DiffMetrics, DomNodeSnapshot, InteractionRegion, Rect, RegionMember, ReplicaDocument, ReplicaPage, SelectorClosure, SeriesCollectionEvidence, SeriesDescriptor, StateDiffProfile, StateEvidence


def _load_grayscale(png_bytes: bytes) -> Image.Image:
    with Image.open(io.BytesIO(png_bytes)) as source:
        return source.convert("L").filter(ImageFilter.GaussianBlur(radius=1))


def _write_visual_jpeg(png_bytes: bytes, destination: Path) -> None:
    """Persist a compact visual artifact while retaining PNG bytes for state diffing."""
    with Image.open(io.BytesIO(png_bytes)) as image:
        image.convert("RGB").save(destination, format="JPEG", quality=95, optimize=True)


def _mask_rect(image: Image.Image, rect: Mapping[str, float]) -> tuple[int, int, int, int]:
    bleed = 3
    x = max(0, round(float(rect["x"])) - bleed)
    y = max(0, round(float(rect["y"])) - bleed)
    right = min(image.width, round(float(rect["x"]) + float(rect["width"])) + bleed)
    bottom = min(image.height, round(float(rect["y"]) + float(rect["height"])) + bleed)
    return x, y, right, bottom


def compute_image_diff(
    before_png: bytes,
    after_png: bytes,
    profile: StateDiffProfile,
    mask_rects: Sequence[Mapping[str, float]] = (),
) -> DiffMetrics:
    """Compare CSS-pixel PNG images after blur, excluding dynamic CSS rectangles."""
    before = _load_grayscale(before_png)
    after = _load_grayscale(after_png)
    if before.size != after.size:
        raise ValueError("visual comparison requires images with identical CSS dimensions")

    differences = ImageChops.difference(before, after)
    pixels = list(differences.get_flattened_data())
    masked = set()
    for rect in mask_rects:
        left, top, right, bottom = _mask_rect(before, rect)
        masked.update(y * before.width + x for y in range(top, bottom) for x in range(left, right))

    compared = len(pixels) - len(masked)
    if not compared:
        return DiffMetrics(0.0, 0.0, 0, 0, len(masked))
    active = [value for index, value in enumerate(pixels) if index not in masked]
    changed = sum(value > profile.pixel_channel_threshold for value in active)
    return DiffMetrics(
        changed_pixel_ratio=changed / compared,
        mean_abs_diff=sum(active) / compared,
        changed_pixel_count=changed,
        compared_pixel_count=compared,
        masked_pixel_count=len(masked),
    )


def is_visual_change(metrics: DiffMetrics, profile: StateDiffProfile) -> bool:
    """Apply the documented regional visual-change thresholds."""
    return (
        metrics.changed_pixel_ratio >= profile.regional_changed_ratio
        or metrics.mean_abs_diff >= profile.regional_mean_abs_diff
    )


def decide_state(evidence: StateEvidence, profile: StateDiffProfile, always_after: bool = False) -> tuple[bool, str]:
    """Apply the documented evidence order when deciding whether an action adds a state."""
    if evidence.topology_changed:
        return True, "topology_changed"
    if evidence.popup_changed:
        return True, "popup_changed"
    if evidence.url_changed:
        return True, "url_changed"
    if always_after:
        return True, "marker_always_after"
    if evidence.region_dom_changed:
        return True, "region_dom_changed"
    if evidence.regional_changed_pixel_ratio >= profile.regional_changed_ratio:
        return True, "regional_changed_ratio"
    if evidence.regional_mean_abs_diff >= profile.regional_mean_abs_diff:
        return True, "regional_mean_abs_diff"
    if evidence.global_changed_pixel_ratio >= profile.global_changed_ratio:
        return True, "global_changed_ratio"
    return False, "no_material_change"


def sanitize_html(source_html: str) -> str:
    """Remove executable, credential-bearing, and remote-content HTML attributes."""
    root = html.fragment_fromstring(source_html, create_parent="div")
    for element in root.xpath(".//script | .//style | .//iframe | .//object | .//embed"):
        element.drop_tree()
    for element in root.xpath(".//input[@type='password' or @type='hidden']"):
        element.drop_tree()
    for element in root.iter():
        for attribute, value in list(element.attrib.items()):
            lowered = attribute.lower()
            identity = f"{lowered}={value}".lower()
            if lowered.startswith("on") or lowered in {"srcdoc", "integrity", "nonce"}:
                del element.attrib[attribute]
            elif lowered == "action" or "token" in identity or "csrf" in identity or "password" in identity:
                del element.attrib[attribute]
            elif lowered in {"src", "href", "action", "poster"} and value.lower().startswith(("http://", "https://", "//", "javascript:")):
                del element.attrib[attribute]
    return "".join(html.tostring(child, encoding="unicode") for child in root)


def dom_snapshot_from_payload(payload: Mapping[str, Any], coordinate_space: str) -> DomNodeSnapshot:
    """Convert browser-evaluated DOM data into the shared manifest model."""
    rect = payload["rect"]
    return DomNodeSnapshot(
        tag_name=str(payload["tag_name"]),
        text=str(payload.get("text", "")),
        attributes={str(key): str(value) for key, value in payload.get("attributes", {}).items()},
        rect=Rect(float(rect["x"]), float(rect["y"]), float(rect["width"]), float(rect["height"]), coordinate_space),
        outer_html=sanitize_html(str(payload.get("outer_html", ""))),
        computed_style={str(key): str(value) for key, value in payload.get("computed_style", {}).items()},
    )


def capture_locator_snapshot(locator: Any, coordinate_space: str = "page_viewport_css") -> DomNodeSnapshot | None:
    """Capture the locator's selector-relevant DOM state in its own frame context.

    多元素 locator 归一 ``.first``，避免 strict-mode evaluate 抛异常被上层静默吞掉
    （Z1：序列列表 ``#HLeftThumnail li.ui-draggable`` 多匹配 -> target.json 不落盘）。
    仅当 count()==0（真·无匹配）时返回 None，调用方需显式处理；count() 本身抛异常
    （元素被移除等）原样外抛，由调用方自己的 try/except 决定——绝不吞真实的
    evaluate/选择器错误，保持「有匹配返回真快照、无匹配才 None」的清晰契约。
    """
    count = locator.count()
    if count == 0:
        return None  # ← 真·无匹配，调用方需处理
    if count > 1:
        locator = locator.first  # ← 多匹配归一，返回仍非空
    payload = locator.evaluate(
        """element => {
            const rect = element.getBoundingClientRect();
            const style = getComputedStyle(element);
            const attributes = Object.fromEntries(Array.from(element.attributes, attribute => [attribute.name, attribute.value]));
            if (element instanceof HTMLInputElement || element instanceof HTMLTextAreaElement) {
                attributes.value = element.value;
            }
            return {
                tag_name: element.tagName.toLowerCase(),
                text: (element.innerText || element.textContent || '').trim(),
                attributes,
                rect: {x: rect.x, y: rect.y, width: rect.width, height: rect.height},
                outer_html: element.outerHTML,
                computed_style: {
                    display: style.display,
                    visibility: style.visibility,
                    position: style.position,
                    font: style.font,
                    color: style.color,
                    backgroundColor: style.backgroundColor,
                },
            };
        }"""
    )
    return dom_snapshot_from_payload(payload, coordinate_space)


def capture_selector_closure(locator: Any, action_id: str) -> SelectorClosure | None:
    """Preserve minimal structural evidence needed to audit an offline locator.

    与 ``capture_locator_snapshot`` 同源的 strict-mode 风险：多元素 locator 归一
    ``.first``；count()==0 时返回 None（调用方决定不写 selector_closure.json）。
    """
    count = locator.count()
    if count == 0:
        return None
    if count > 1:
        locator = locator.first
    payload = locator.evaluate(
        """element => {
            let ancestors = 0;
            for (let parent = element.parentElement; parent; parent = parent.parentElement) ancestors++;
            const sources = ['aria-label', 'aria-labelledby', 'title', 'name', 'data-testid']
                .filter(name => element.hasAttribute(name));
            return {outer: element.outerHTML, ancestors, siblings: element.parentElement ? element.parentElement.children.length - 1 : 0, sources};
        }"""
    )
    return SelectorClosure(action_id, sanitize_html(payload["outer"]), int(payload["ancestors"]), int(payload["siblings"]), list(payload["sources"]))


def capture_interaction_region(root_locator: Any, region_type: str, document_id: str) -> InteractionRegion | None:
    """Capture a region root and all native/ARIA controls required for offline replay.

    root 无匹配（capture_locator_snapshot 返回 None）时返回 None，调用方跳过该 region。
    """
    root = capture_locator_snapshot(root_locator)
    if root is None:
        return None  # 真·无匹配：调用方不 append 该 region
    controls = root_locator.locator("button, input, select, textarea, canvas, [role], [data-testid], [id], [class]")
    members = []
    if root.tag_name in {"button", "input", "select", "textarea", "canvas"} or "role" in root.attributes or "data-testid" in root.attributes:
        members.append(RegionMember(f"{document_id}_{region_type}_root", root.tag_name, root))
    for index in range(controls.count()):
        locator = controls.nth(index)
        snapshot = capture_locator_snapshot(locator, "region_content_css")
        if snapshot is None:
            continue  # 单成员无匹配：跳过该成员，不让 None 泄漏进 RegionMember
        members.append(RegionMember(f"{document_id}_{region_type}_{index:03d}", snapshot.tag_name, snapshot))
    return InteractionRegion(f"{document_id}_{region_type}", region_type, document_id, root, members, None)


_SERIES_ITEM_SELECTOR = "option, [data-series], [role='option'], .series-item, li"
# Highest-priority stable attribute wins; the first *non-empty* attribute on this
# list (in order) is used as the identity of a series. Later attributes are kept
# as descriptive stable_attributes but never as the primary identity.
_SERIES_IDENTITY_ATTRS = ("data-series-uid", "data-series", "data-uid", "value", "id")
_SERIES_FRAME_ATTRS = ("data-frame-count", "data-frames", "data-frame_total", "data-total-frames")


def normalize_series_text(text: str) -> str:
    """Normalize stable series text while ignoring transient download progress."""
    normalized = " ".join((text or "").split())
    normalized = re.sub(
        r"(\d{1,6}\s*(?:幅|帧|张|frames?|images?))\s+\d{1,6}$",
        r"\1",
        normalized,
        flags=re.IGNORECASE,
    )
    return normalized.lower()


_normalize_series_text = normalize_series_text


def _series_frame_count_from_text(text: str) -> int | None:
    match = re.search(r"(\d{1,6})\s*(?:幅|帧|frames?|images?)\b", text or "", flags=re.IGNORECASE)
    return int(match.group(1)) if match else None


def _series_stable_attributes(snapshot: DomNodeSnapshot, identity_attrs: Sequence[str] | None = None) -> dict[str, str]:
    stable: dict[str, str] = {}
    attrs = _SERIES_IDENTITY_ATTRS if identity_attrs is None else tuple(identity_attrs)
    for name in attrs:
        value = snapshot.attributes.get(name)
        if value:
            stable[name] = value
    return stable


def _series_identity(snapshot: DomNodeSnapshot, identity_attrs: Sequence[str] | None = None) -> tuple[tuple[str, str], dict[str, str], str | None]:
    """Return (identity_key, stable_attributes, key_basis).

    ``identity_key`` drives cross-scroll dedup; distinct keys mean distinct
    logical series. ``key_basis`` is the human-meaningful identity value used to
    build ``series_key`` (None when only the text-fallback exists, so the caller
    appends the document id + same-name occurrence index).
    """
    attrs = _SERIES_IDENTITY_ATTRS if identity_attrs is None else tuple(identity_attrs)
    stable = _series_stable_attributes(snapshot, attrs)
    for name in attrs:
        if name in stable:
            return (f"attr:{name}", stable[name]), stable, stable[name]
    norm = _normalize_series_text(snapshot.text)
    return ("text", norm), stable, None


def _series_selected(snapshot: DomNodeSnapshot) -> bool:
    attributes = snapshot.attributes
    if attributes.get("aria-selected", "").lower() == "true":
        return True
    if "selected" in attributes:
        return True
    if attributes.get("data-selected", "").lower() in ("true", "1", "selected"):
        return True
    return False


def _series_explicit_frame_count(snapshot: DomNodeSnapshot) -> int | None:
    for name in _SERIES_FRAME_ATTRS:
        value = snapshot.attributes.get(name)
        if value and value.strip().isdigit():
            return int(value.strip())
    return None


def discover_series_candidates(
    root_locator: Any,
    document_id: str,
    max_scroll_steps: int = 40,
    max_duration_s: float = 10.0,
    item_selector: str | None = None,
    identity_attrs: Sequence[str] | None = None,
) -> tuple[list[SeriesDescriptor], list[RegionMember], SeriesCollectionEvidence]:
    """Deterministically enumerate scrollable series rows into stable descriptors.

    This is the single scroll-harvest discovery algorithm shared by ordinary
    snapshot capture and auto-exploration. It restores the original
    ``scrollTop`` on every exit path, dedups virtualized list nodes that are
    reused across scroll positions, and never stores Locators, element handles
    or absolute coordinates -- only stable descriptions.

    ``item_selector`` / ``identity_attrs`` override the hardcoded per-viewer
    defaults so real sites whose series rows are not ``.series-item``/``li``
    (e.g. FTImage's ``a > div.desc > span.total``) can be enumerated without
    changing the shared defaults (``None`` keeps the current behavior).

    Returns ``(descriptors, members, evidence)`` where each descriptor's
    ``member_id`` matches the corresponding region member, so the discovered
    count is directly auditable against the collected region members.
    """
    id_attrs = _SERIES_IDENTITY_ATTRS if identity_attrs is None else tuple(identity_attrs)
    items = root_locator.locator(item_selector or _SERIES_ITEM_SELECTOR)
    initial = root_locator.evaluate("element => ({top: element.scrollTop, height: element.clientHeight, scrollHeight: element.scrollHeight})")
    discovered: dict[tuple[str, str], dict[str, Any]] = {}
    order: list[tuple[str, str]] = []
    reached_end = initial["scrollHeight"] <= initial["height"]
    steps = 0
    deadline = time.monotonic() + max_duration_s
    try:
        while True:
            for index in range(items.count()):
                snapshot = capture_locator_snapshot(items.nth(index), "region_content_css")
                if snapshot is None:
                    continue  # 该行瞬变无匹配：跳过，不影响其余行枚举
                identity_key, stable, _ = _series_identity(snapshot, id_attrs)
                if identity_key not in discovered:
                    discovered[identity_key] = {
                        "snapshot": snapshot,
                        "label": snapshot.text,
                        "stable": stable,
                        "selected": _series_selected(snapshot),
                        "explicit_frames": _series_explicit_frame_count(snapshot),
                        "activation": snapshot.attributes.get("data-activation") or None,
                    }
                    order.append(identity_key)
                else:
                    record = discovered[identity_key]
                    # A later scroll window may expose the node in its selected
                    # state or with more complete text; merge rather than discard.
                    if _series_selected(snapshot):
                        record["selected"] = True
                    if not record["snapshot"].text and snapshot.text:
                        record["snapshot"] = snapshot
                        record["label"] = snapshot.text
            if reached_end or steps >= max_scroll_steps or time.monotonic() >= deadline:
                break
            previous = root_locator.evaluate("element => element.scrollTop")
            current = root_locator.evaluate("element => { element.scrollTop += Math.max(1, element.clientHeight); return element.scrollTop; }")
            steps += 1
            reached_end = current == previous or current + initial["height"] >= initial["scrollHeight"]
            if current == previous:
                break
    finally:
        root_locator.evaluate("(element, top) => element.scrollTop = top", initial["top"])

    virtualized = initial["scrollHeight"] > initial["height"]
    warning = "series_virtualized_partial" if virtualized and not reached_end else None

    descriptors: list[SeriesDescriptor] = []
    members: list[RegionMember] = []
    same_name_index: dict[str, int] = {}
    for position, identity_key in enumerate(order):
        record = discovered[identity_key]
        member_id = f"{document_id}_series_{len(members):03d}"
        snapshot = record["snapshot"]
        members.append(RegionMember(member_id, snapshot.tag_name, snapshot))
        if record["stable"]:
            series_key = next(record["stable"][name] for name in id_attrs if name in record["stable"])
        else:
            norm = _normalize_series_text(snapshot.text)
            occurrence = same_name_index.get(norm, 0)
            same_name_index[norm] = occurrence + 1
            series_key = f"{document_id}::{norm}::x{occurrence}"
        descriptors.append(SeriesDescriptor(
            series_key=series_key,
            label=record["label"],
            ordinal=position,
            document_id=document_id,
            member_id=member_id,
            stable_attributes=record["stable"],
            selected=record["selected"],
            explicit_frame_count=record["explicit_frames"],
            inferred_frame_count=_series_frame_count_from_text(snapshot.text),
            activation=record["activation"],
        ))

    evidence = SeriesCollectionEvidence(
        "scroll_harvest", virtualized, items.count(), len(descriptors), steps, reached_end, warning, len(descriptors)
    )
    return descriptors, members, evidence


def capture_series_interaction_region(
    root_locator: Any,
    document_id: str,
    max_scroll_steps: int = 40,
    item_selector: str | None = None,
    identity_attrs: Sequence[str] | None = None,
) -> InteractionRegion | None:
    """Harvest scrollable series rows as a region, restoring scroll position afterward.

    Delegates to :func:`discover_series_candidates` (the single scroll-harvest
    algorithm) and packages its region members into an ``InteractionRegion``.
    root 无匹配时返回 None，调用方跳过该 region。
    """
    _, members, evidence = discover_series_candidates(
        root_locator, document_id, max_scroll_steps=max_scroll_steps,
        item_selector=item_selector, identity_attrs=identity_attrs,
    )
    root = capture_locator_snapshot(root_locator)
    if root is None:
        return None
    return InteractionRegion(f"{document_id}_series", "series", document_id, root, members, evidence)


_MARKER_REGION_CANDIDATES: dict[str, tuple[str, tuple[str, ...]]] = {
    "报告截图": ("report", ("#report", "[data-testid*='report' i]", "main", "body")),
    "序列布局切换": ("layout", ("[data-testid*='layout' i]", "[class*='layout' i]", "[aria-label*='layout' i]", "body")),
    "序列选择": ("series", ("[data-testid*='series' i]", "[class*='series' i]", "[aria-label*='series' i]", "body")),
    "Meta 信息工具": ("metadata", ("[id*='tags' i]", "[class*='tags' i]", "[data-testid*='tags' i]", "[roles*='tags' i]", "[id*='info' i]", "[class*='info' i]", "[data-testid*='dicom' i]", "[class*='dicom' i]", "[class*='metadata' i]", "[aria-label*='dicom' i]", ".ui-dialog:has(.ui-dialog-titlebar-close)", "[role='dialog']", "body")),
    "窗宽窗位 WL/WW": ("wlww", ("[data-testid*='window' i]", "[class*='window-level' i]", "[class*='wlww' i]", "[aria-label*='window' i]", "[role='dialog']", "body")),
    "影像画布交互": ("canvas", ("canvas", "[class*='cornerstone-canvas' i]", "[data-testid*='canvas' i]", "body")),
}


def marker_region_type(marker_label: str) -> str:
    """Return the stable manifest region type for a marker label."""
    return _MARKER_REGION_CANDIDATES.get(marker_label, ("generic", ()))[0]


def _first_visible_marker_root(scope: Any, candidates: Sequence[str]) -> Any:
    for selector in candidates:
        locator = scope.locator(selector)
        for index in range(locator.count()):
            item = locator.nth(index)
            if item.is_visible():
                return item
    return scope.locator("body")


def capture_marker_interaction_region(
    scope: Any,
    marker_label: str,
    document_id: str,
    target_locator: Any | None = None,
    max_scroll_steps: int = 40,
    item_selector: str | None = None,
    identity_attrs: Sequence[str] | None = None,
) -> InteractionRegion | None:
    """Capture the smallest documented interaction region for a marker action.

    ``scope`` is a Playwright ``Page`` or ``Frame``.  The selectors deliberately
    stay viewer-agnostic and finish at ``body`` so an unfamiliar viewer still has
    auditable DOM evidence instead of silently losing the region. For the
    ``series`` region type, ``item_selector`` / ``identity_attrs`` are forwarded
    to :func:`capture_series_interaction_region` to support per-viewer row
    structures (hardcoded defaults otherwise). root 真·无匹配时返回 None。
    """
    region_type, candidates = _MARKER_REGION_CANDIDATES.get(marker_label, ("generic", ("body",)))
    # Metadata panels are click-opened scroll containers whose HTML differs per
    # hospital. The trigger button lives OUTSIDE the panel (the panel is a
    # sibling container), so we locate the panel root via the candidate selectors
    # and keep its complete outerHTML — every row stays reachable regardless of
    # per-viewer structure. Fall back to the generic region otherwise.
    if region_type == "metadata":
        panel = capture_marker_panel_region(scope, candidates, document_id)
        if panel is not None:
            return panel
    root_locator = _first_visible_marker_root(scope, candidates)
    if target_locator is not None and root_locator.count() == 0:
        root_locator = target_locator
    if region_type == "series":
        return capture_series_interaction_region(
            root_locator, document_id, max_scroll_steps,
            item_selector=item_selector, identity_attrs=identity_attrs,
        )
    return capture_interaction_region(root_locator, region_type, document_id)


def capture_marker_panel_region(
    scope: Any,
    candidates: Sequence[str],
    document_id: str,
) -> InteractionRegion | None:
    """Capture a click-opened panel container's complete DOM, viewer-agnostic.

    Resolves the first *visible* candidate root inside ``scope`` (same selector
    resolution as the generic region capture) and keeps its full ``outerHTML``
    verbatim, so the replica renders every row and lets the user scroll. The
    panel is an independent sibling container of the action button, so we do not
    walk up from the button — we look for the container directly.
    """
    root_locator = None
    for selector in candidates:
        matches = scope.locator(selector)
        for index in range(matches.count()):
            candidate = matches.nth(index)
            if not candidate.is_visible():
                continue
            tag = candidate.evaluate("element => element.tagName.toLowerCase()")
            if tag in {"a", "button", "i", "input", "select", "span", "svg", "textarea"}:
                continue
            candidate_data = candidate.evaluate(
                """element => {
                    const rect = element.getBoundingClientRect();
                    const identity = `${element.id || ''} ${element.className || ''}`.toLowerCase();
                    return {
                        area: rect.width * rect.height,
                        text: (element.innerText || element.textContent || '').trim(),
                        identity,
                        role: (element.getAttribute('role') || '').toLowerCase(),
                    };
                }"""
            )
            is_named_panel = any(
                token in candidate_data["identity"]
                for token in ("tagsbox", "box-tags", "dicom", "metadata")
            )
            if (
                not candidate_data["text"]
                or candidate_data["area"] < 1000
                or not (is_named_panel or candidate_data["role"] == "dialog")
            ):
                continue
            root_locator = candidate
            break
        if root_locator is not None:
            break
    if root_locator is None:
        return None
    # Resolve root identity, then read outerHTML from the resolved element.
    result = root_locator.evaluate(
        """element => {
            const rect = element.getBoundingClientRect();
            return {
                html: element.outerHTML,
                rect: {x: rect.x, y: rect.y, width: rect.width, height: rect.height},
                tag: element.tagName.toLowerCase(),
            };
        }"""
    )
    if not result or not result.get("html"):
        return None
    rect = result["rect"]
    tag = (result.get("tag") or "").lower()
    # A document-level root (body/html/main) is the whole page, not a panel.
    # Fall back to the generic region logic when no real panel selector landed.
    if tag in {"html", "body", "main", "document", "documentelement"}:
        return None
    # Guard against capturing an oversized container that likely is not a panel.
    if rect["width"] > 0 and rect["height"] > 0 and result["html"].count("<") > 2000:
        return None
    root = capture_locator_snapshot(root_locator, "page_viewport_css")
    if root is None:
        return None  # root 瞬变无匹配：面板捕获返回 None（调用方已能容忍）
    # The panel root is rendered verbatim as a scrollable region (complete
    # outerHTML). Around it the page may still show sibling interactive
    # controls (WL/WW inputs, confirm button, canvas, series toolbar) that the
    # offline replay must keep reachable — collect them as members, excluding
    # anything already inside the panel root so it is not duplicated.
    members: list[RegionMember] = []
    raw_root_html = result.get("html", "")
    controls = scope.locator("button, input, select, textarea, canvas, [role], [data-testid], [id], [class]")
    for index in range(controls.count()):
        locator = controls.nth(index)
        try:
            raw_html = locator.evaluate("element => element.outerHTML") or ""
            if not raw_html:
                continue
            if raw_root_html and (
                raw_html in raw_root_html or raw_root_html in raw_html
            ):
                continue
            snapshot = capture_locator_snapshot(locator, "region_content_css")
        except Exception:
            continue
        if snapshot is None:
            continue  # 兄弟控件瞬变无匹配：跳过，避免 None 泄漏进 RegionMember
        members.append(RegionMember(f"{document_id}_metadata_sibling_{index:03d}", snapshot.tag_name, snapshot))
    return InteractionRegion(
        f"{document_id}_metadata",
        "metadata",
        document_id,
        root,
        members,
        None,
    )


def _frame_descriptor(frame: Any) -> dict[str, Any]:
    """Read the owning iframe from the frame context without top-level DOM traversal."""
    return frame.evaluate(
        """() => {
            const element = window.frameElement;
            if (!element) return null;
            return {id: element.id || null, name: element.name || null};
        }"""
    )


def _frame_selector(descriptor: Mapping[str, Any]) -> str:
    if descriptor.get("id"):
        return f"#{descriptor['id']}"
    if descriptor.get("name"):
        return f'iframe[name="{descriptor["name"]}"]'
    return "iframe"


def capture_page_topology(
    named_pages: Sequence[tuple[str, Any]],
    asset_root: Path,
) -> tuple[list[ReplicaPage], list[ReplicaDocument]]:
    """Capture page/popup and nested frame documents as CSS-scale PNG assets."""
    asset_root = Path(asset_root)
    asset_dir = asset_root / "assets"
    asset_dir.mkdir(parents=True, exist_ok=True)
    pages: list[ReplicaPage] = []
    documents: list[ReplicaDocument] = []
    frame_documents: dict[Any, str] = {}
    page_ids: dict[Any, str] = {}

    for page_index, (page_var, page) in enumerate(named_pages):
        page_id = f"p_{page_index:03d}"
        page_ids[page] = page_id
        entry_document_id = f"d_{page_id}_root"
        screenshot = page.screenshot(type="png", scale="css")
        relative_asset = Path("assets") / f"{entry_document_id}.png"
        (asset_root / relative_asset).write_bytes(screenshot)
        _write_visual_jpeg(screenshot, asset_root / relative_asset.with_suffix(".jpeg"))
        viewport = page.viewport_size or {"width": 0, "height": 0}
        pages.append(ReplicaPage(page_id, page_var, "main" if page_index == 0 else "popup", None, None, entry_document_id, page == named_pages[0][1], page.is_closed()))
        root_document = ReplicaDocument(
            entry_document_id, page_id, page_var, "main" if page_index == 0 else "popup", None, None, None, None,
            viewport, 1.0, "css", 0.0, 0.0, str(relative_asset).replace("\\", "/"), hashlib.sha256(screenshot).hexdigest(), len(screenshot),
        )
        documents.append(root_document)
        frame_documents[page.main_frame] = entry_document_id

        child_frames = [frame for frame in page.frames if frame != page.main_frame]
        for frame_index, frame in enumerate(child_frames, start=1):
            descriptor = _frame_descriptor(frame)
            parent_id = frame_documents.get(frame.parent_frame)
            frame_element = frame.frame_element()
            if descriptor is None:
                descriptor = frame_element.evaluate("element => ({id: element.id || null, name: element.name || null})")
            box = frame_element.bounding_box()
            if not box:
                continue
            screenshot = frame.locator("html").screenshot(type="png")
            document_id = f"d_{page_id}_f_{frame_index:03d}"
            relative_asset = Path("assets") / f"{document_id}.png"
            (asset_root / relative_asset).write_bytes(screenshot)
            _write_visual_jpeg(screenshot, asset_root / relative_asset.with_suffix(".jpeg"))
            viewport_data = frame.evaluate("() => ({width: innerWidth, height: innerHeight, scrollX, scrollY})")
            documents.append(ReplicaDocument(
                document_id, page_id, page_var, "popup" if page_index else "main", parent_id, _frame_selector(descriptor), descriptor.get("id"), descriptor.get("name"),
                {"width": int(viewport_data["width"]), "height": int(viewport_data["height"])}, 1.0, "css", float(viewport_data["scrollX"]), float(viewport_data["scrollY"]),
                str(relative_asset).replace("\\", "/"), hashlib.sha256(screenshot).hexdigest(), len(screenshot),
            ))
            frame_documents[frame] = document_id
    return pages, documents


def wait_for_visual_stability(
    capture_png: Callable[[], bytes],
    profile: StateDiffProfile,
    timeout_ms: int,
) -> tuple[bytes, bool]:
    """Return a PNG after consecutive unchanged samples, or the final sample on timeout."""
    deadline = time.monotonic() + timeout_ms / 1000
    previous = capture_png()
    stable_rounds = 0
    while time.monotonic() <= deadline:
        if profile.stability_interval_ms:
            time.sleep(profile.stability_interval_ms / 1000)
        current = capture_png()
        metrics = compute_image_diff(previous, current, profile)
        if metrics.changed_pixel_count == 0:
            stable_rounds += 1
            if stable_rounds >= profile.stability_rounds:
                return current, True
        else:
            stable_rounds = 0
        previous = current
    return previous, False
