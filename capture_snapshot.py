"""Visual-state capture helpers used by the live replica runner."""

from __future__ import annotations

import io
import hashlib
import time
from pathlib import Path
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from PIL import Image, ImageChops, ImageFilter
from lxml import html

from replica_models import DiffMetrics, DomNodeSnapshot, InteractionRegion, Rect, RegionMember, ReplicaDocument, ReplicaPage, SelectorClosure, SeriesCollectionEvidence, StateDiffProfile, StateEvidence


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


def capture_locator_snapshot(locator: Any, coordinate_space: str = "page_viewport_css") -> DomNodeSnapshot:
    """Capture the locator's selector-relevant DOM state in its own frame context."""
    payload = locator.evaluate(
        """element => {
            const rect = element.getBoundingClientRect();
            const style = getComputedStyle(element);
            return {
                tag_name: element.tagName.toLowerCase(),
                text: (element.innerText || element.textContent || '').trim(),
                attributes: Object.fromEntries(Array.from(element.attributes, attribute => [attribute.name, attribute.value])),
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


def capture_selector_closure(locator: Any, action_id: str) -> SelectorClosure:
    """Preserve minimal structural evidence needed to audit an offline locator."""
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


def capture_interaction_region(root_locator: Any, region_type: str, document_id: str) -> InteractionRegion:
    """Capture a region root and all native/ARIA controls required for offline replay."""
    root = capture_locator_snapshot(root_locator)
    controls = root_locator.locator("button, input, select, textarea, canvas, [role], [data-testid], [id], [class]")
    members = []
    if root.tag_name in {"button", "input", "select", "textarea", "canvas"} or "role" in root.attributes or "data-testid" in root.attributes:
        members.append(RegionMember(f"{document_id}_{region_type}_root", root.tag_name, root))
    for index in range(controls.count()):
        locator = controls.nth(index)
        snapshot = capture_locator_snapshot(locator, "region_content_css")
        members.append(RegionMember(f"{document_id}_{region_type}_{index:03d}", snapshot.tag_name, snapshot))
    return InteractionRegion(f"{document_id}_{region_type}", region_type, document_id, root, members, None)


def capture_series_interaction_region(root_locator: Any, document_id: str, max_scroll_steps: int = 40) -> InteractionRegion:
    """Harvest scrollable series rows, restoring the source scroll position afterward."""
    root = capture_locator_snapshot(root_locator)
    items = root_locator.locator("option, [data-series], [role='option'], .series-item, li")
    initial = root_locator.evaluate("element => ({top: element.scrollTop, height: element.clientHeight, scrollHeight: element.scrollHeight})")
    collected: dict[tuple[str, str, str], RegionMember] = {}
    reached_end = initial["scrollHeight"] <= initial["height"]
    steps = 0
    try:
        while True:
            for index in range(items.count()):
                snapshot = capture_locator_snapshot(items.nth(index), "region_content_css")
                key = (snapshot.attributes.get("data-series", ""), snapshot.attributes.get("value", ""), snapshot.text)
                collected.setdefault(key, RegionMember(f"{document_id}_series_{len(collected):03d}", snapshot.tag_name, snapshot))
            if reached_end or steps >= max_scroll_steps:
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
    evidence = SeriesCollectionEvidence("scroll_harvest", virtualized, items.count(), len(collected), steps, reached_end, warning)
    return InteractionRegion(f"{document_id}_series", "series", document_id, root, list(collected.values()), evidence)


_MARKER_REGION_CANDIDATES: dict[str, tuple[str, tuple[str, ...]]] = {
    "报告截图": ("report", ("#report", "[data-testid*='report' i]", "main", "body")),
    "序列布局切换": ("layout", ("[data-testid*='layout' i]", "[class*='layout' i]", "[aria-label*='layout' i]", "body")),
    "序列选择": ("series", ("[data-testid*='series' i]", "[class*='series' i]", "[aria-label*='series' i]", "body")),
    "Meta 信息工具": ("metadata", ("[data-testid*='dicom' i]", "[class*='dicom' i]", "[class*='metadata' i]", "[aria-label*='dicom' i]", "[role='dialog']", "body")),
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
) -> InteractionRegion:
    """Capture the smallest documented interaction region for a marker action.

    ``scope`` is a Playwright ``Page`` or ``Frame``.  The selectors deliberately
    stay viewer-agnostic and finish at ``body`` so an unfamiliar viewer still has
    auditable DOM evidence instead of silently losing the region.
    """
    region_type, candidates = _MARKER_REGION_CANDIDATES.get(marker_label, ("generic", ("body",)))
    root_locator = _first_visible_marker_root(scope, candidates)
    if target_locator is not None and root_locator.count() == 0:
        root_locator = target_locator
    if region_type == "series":
        return capture_series_interaction_region(root_locator, document_id, max_scroll_steps)
    return capture_interaction_region(root_locator, region_type, document_id)


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
            screenshot = page.screenshot(type="png", scale="css", clip=box)
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
