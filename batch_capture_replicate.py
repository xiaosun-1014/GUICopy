"""Isolated command-line entrypoint for replica build stages."""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import queue
import threading
import yaml
from datetime import datetime, timezone
from dataclasses import asdict, dataclass, replace
from collections.abc import Callable
from pathlib import Path

from PIL import Image, ImageStat

from build_replica import build_replica
from capture_readiness import _metadata_candidate_allowed, canvas_hash, metadata_panel_signature, metadata_uid_sha256_prefix, screenshot_nonblank, viewer_dom_fingerprint, wait_for_metadata_panel_state
from capture_snapshot import capture_interaction_region, capture_locator_snapshot, capture_marker_interaction_region, capture_page_topology, capture_selector_closure, compute_image_diff, decide_state, marker_region_type, discover_series_candidates, normalize_series_text, sanitize_html, _MARKER_REGION_CANDIDATES
from process_runner import ManagedProcess
from replay_helpers import read_manifest, sha256_file, write_manifest
from rewrite_script import ActionPlan, parse_action_plan
from replica_models import ActionTarget, CaptureTimingProfile, DomNodeSnapshot, InteractionRegion, LocatorRecipe, Point, Rect, RegionMember, ReplicaDocument, ReplicaFlow, ReplicaPage, ReplicaState, ReplicaTransition, SeriesBranch, SeriesCollectionEvidence, SeriesDescriptor, SeriesExpansionEvidence, StateDiffProfile, StateEvidence
from runtime_python import codegen_python_executable


def wait_for_pre_action_state(
    page: object,
    marker_label: str,
    locator_factory: object | None = None,
) -> None:
    """Wait until the recorded target is actionable before capturing its input state."""
    if marker_label == "报告截图" and locator_factory is not None:
        try:
            target = locator_factory()
            target.wait_for(state="visible", timeout=30000)
            target.click(trial=True, timeout=30000)
        except Exception:
            pass
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


def _wait_for_report_popup_state(
    page: object,
    timeout_s: float = 30.0,
    stable_s: float = 1.0,
) -> bool:
    """Wait for a newly opened viewer page to finish rendering its first image."""
    try:
        popup_pages = [candidate for candidate in page.context.pages if candidate is not page]
    except Exception:
        return False
    if not popup_pages:
        return True
    deadline = time.monotonic() + timeout_s
    warmup_s = min(20.0, timeout_s / 3.0)
    viewer_seen_since: float | None = None
    stable_since: float | None = None
    while time.monotonic() < deadline:
        dom_ready = False
        ready_frame = None
        for popup in popup_pages:
            try:
                frames = popup.frames[1:]
            except Exception:
                continue
            for frame in frames:
                try:
                    dom_ready = bool(frame.evaluate(
                        """() => {
                            const visibleArea = element => {
                                const style = getComputedStyle(element);
                                const rect = element.getBoundingClientRect();
                                return style.display !== 'none' && style.visibility !== 'hidden'
                                    ? rect.width * rect.height : 0;
                            };
                            const canvasReady = Array.from(document.querySelectorAll('canvas'))
                                .some(element => visibleArea(element) >= 1000);
                            const imageReady = Array.from(document.querySelectorAll('img, video'))
                                .some(element => visibleArea(element) >= 10000);
                            return document.readyState !== 'loading'
                                && (canvasReady || imageReady);
                        }"""
                    ))
                except Exception:
                    dom_ready = False
                if dom_ready:
                    ready_frame = frame
                    break
            if dom_ready:
                break
        ready = False
        if dom_ready and ready_frame is not None:
            viewer_seen_since = viewer_seen_since or time.monotonic()
            try:
                canvases = ready_frame.locator("canvas")
                largest = None
                largest_area = 0.0
                for index in range(canvases.count()):
                    candidate = canvases.nth(index)
                    box = candidate.bounding_box()
                    area = (box or {}).get("width", 0) * (box or {}).get("height", 0)
                    if area > largest_area:
                        largest = candidate
                        largest_area = area
                target = largest if largest is not None else ready_frame.locator("html")
                screenshot = target.screenshot(type="png")
                with Image.open(io.BytesIO(screenshot)) as image:
                    grayscale = image.convert("L").resize((160, 90))
                    ready = ImageStat.Stat(grayscale).stddev[0] >= 2.0
            except Exception:
                ready = False
        else:
            viewer_seen_since = None
        now = time.monotonic()
        warmed_up = (
            viewer_seen_since is not None
            and now - viewer_seen_since >= warmup_s
        )
        if ready and warmed_up:
            stable_since = stable_since or now
            if now - stable_since >= stable_s:
                return True
        else:
            stable_since = None
        page.wait_for_timeout(250)
    return False


def ensure_post_action_state(
    page: object,
    marker_label: str,
    locator_factory: object | None = None,
    timeout_s: float = 10.0,
    stable_s: float = 1.0,
) -> None:
    """Retry a series transition once when the recorded dblclick did not change UI state."""
    if marker_label == "报告截图":
        if not _wait_for_report_popup_state(
            page,
            timeout_s=max(timeout_s, 60.0),
            stable_s=stable_s,
        ):
            raise TimeoutError("viewer popup did not render non-blank content")
        return
    if marker_label == "Meta 信息工具":
        if not callable(locator_factory):
            return
        try:
            if locator_factory().count() == 0:
                return
        except Exception:
            return
        if wait_for_metadata_panel_state(page, locator_factory, timeout_s, stable_s):
            return
        # The panel never stabilized (or its container is not matched by the
        # candidate selectors). Do NOT raise here: raising would abort the
        # after capture in LiveCaptureSession.after, leaving no after
        # topology.json and silently dropping the whole marked action from the
        # flow (see _has_snapshot_pair). Falling through captures an at-most
        # empty / unrendered panel state, which is no worse than the legacy
        # behavior that never waited for metadata at all.
        return
    if wait_for_post_action_state(page, marker_label, timeout_s, stable_s):
        return
    if marker_label == "序列选择" and callable(locator_factory):
        locator_factory().dblclick()
        if wait_for_post_action_state(page, marker_label, timeout_s, stable_s):
            return
    raise TimeoutError(f"post-action state did not stabilize for marker: {marker_label}")


# ---------------------------------------------------------------------------
# Phase 5/6: single-series transaction and all-series explorer
# ---------------------------------------------------------------------------

# Minimal, same-frame series-list item selector used for re-locating a target
# row inside a (possibly virtualized) series list. Kept local so the explorer
# does not depend on private selectors drifting out of capture_snapshot.
_SERIES_ITEM_SELECTOR = "option, [data-series], [role='option'], .series-item, li"
_SERIES_IDENTITY_ATTRS = ("data-series-uid", "data-series", "data-uid", "value", "id")


class HubUnrecoverableError(RuntimeError):
    """Raised when a series transaction cannot recover the series list hub."""


@dataclass
class CaptureBranchOutcome:
    """Serializable outcome of one per-series transaction (safe, non-sensitive)."""

    branch_id: str
    series_key: str
    label: str
    ordinal: int
    document_id: str
    source_member_id: str
    activation: str
    capture_status: str  # captured|partial|failed|skipped_duplicate
    fail_stage: str | None
    error_type: str | None
    warning: str | None
    viewer_documents: int = 0
    metadata_captured: bool = False
    metadata_uid_hash_prefix: str | None = None


@dataclass
class SeriesBranchCapture:
    """Loaded snapshot evidence for one branch, ready to merge into a flow."""

    branch_id: str
    series_key: str
    label: str
    ordinal: int
    document_id: str
    source_member_id: str
    activation: str
    capture_status: str
    warning: str | None
    viewer_pages: list[ReplicaPage]
    viewer_documents: list[ReplicaDocument]
    metadata_pages: list[ReplicaPage]
    metadata_documents: list[ReplicaDocument]
    metadata_rows: list[dict[str, object]]
    meta_open_dom: DomNodeSnapshot | None = None


def _normalize_series_text(text: str) -> str:
    return normalize_series_text(str(text))


def _matches_descriptor(snapshot: DomNodeSnapshot, descriptor: SeriesDescriptor) -> bool:
    """Return True when a re-parsed series row matches a stable descriptor."""
    if descriptor.stable_attributes:
        for name, value in descriptor.stable_attributes.items():
            if snapshot.attributes.get(name) == value:
                return True
    if descriptor.label:
        return _normalize_series_text(snapshot.text) == _normalize_series_text(descriptor.label)
    return False


def _series_descriptor_matches(source: SeriesDescriptor, target: SeriesDescriptor) -> bool:
    """Match two descriptors by stable description (attribute identity or text).

    Member ids differ across documents (discovery hub vs a branch viewer), so a
    member id equality check would never match; the route binding instead relies
    on the stable semantic identity of the series.
    """
    if source.stable_attributes and target.stable_attributes:
        if any(source.stable_attributes.get(name) == value for name, value in target.stable_attributes.items()):
            return True
    if source.series_key and source.series_key == target.series_key:
        return True
    if source.label and target.label:
        return _normalize_series_text(source.label) == _normalize_series_text(target.label)
    return False


def _evidence_satisfied(evidence: set[str]) -> bool:
    """(P1#4) A selection is ready only when at least two *independent* evidence
    types hold. ``screenshot_nonblank`` is deliberately excluded from the count:
    a non-blank screenshot may just be the previous series, so it never counts
    toward readiness on its own or as one of the two.
    """
    core = evidence - {"screenshot_nonblank"}
    return len(core) >= 2


def _safe_series_key(descriptor: SeriesDescriptor) -> str:
    """Return a non-sensitive internal slug for the series branch directory.

    The slug embeds only the ordinal and a short SHA-256 of the stable identity;
    the raw SeriesInstanceUID / patient name / accession number never appear in
    filenames or public logs.
    """
    digest = hashlib.sha256(f"{descriptor.document_id}::{descriptor.series_key}".encode("utf-8")).hexdigest()
    return f"b{descriptor.ordinal:03d}_{digest[:12]}"


_SERIES_EVENT_NAMES = frozenset({
    "series_discovery_started",
    "series_discovered",
    "series_capture_started",
    "series_capture_completed",
    "series_capture_partial",
    "series_capture_failed",
    "series_expansion_completed",
})


def _emit_series_event(event: dict[str, object]) -> None:
    """Emit a safe ``series_*`` event as one JSON line on stdout (P1#7).

    The instrumented replay subprocess's stdout is parsed by ``ManagedProcess``
    and every dict carrying an ``"event"`` key is forwarded to the orchestrator /
    GUI via ``on_event``. Only non-sensitive branch id / ordinal / count /
    status / stage fields are emitted; raw series_key / UID / patient text never
    reach this channel. The event name must be one of
    ``_SERIES_EVENT_NAMES`` (mirroring ``orchestrator_events.SERIES_EVENT_NAMES``).
    """
    event = dict(event)
    event.setdefault("event", "")
    if event.get("event") not in _SERIES_EVENT_NAMES:
        return
    print(json.dumps(event, ensure_ascii=False), flush=True)


# Shared per-viewer series-discovery config (skills/_shared/viewers.yaml).
# Derivable from ``Path(__file__).resolve().parents[0]`` because this module
# lives in the repo root. Kept as a module constant so unit tests/stubs can
# point it at a controlled fixture without touching the shared file.
_SERIES_VIEWERS_YAML = Path(__file__).resolve().parents[0] / "skills" / "_shared" / "viewers.yaml"


def _series_viewer_config_for(page: object) -> dict[str, object]:
    """Match a live ``page`` URL against skills/_shared/viewers.yaml and return
    the per-viewer series-discovery configuration.

    Returns ``{'item_container_selector','item_selector','identity_attrs'}``
    (only the keys present in the matched viewer's ``sequence_select``) so the
    series hub/row structure of real sites (e.g. FTImage's ``a > span.total``
    rows, zscloud's ``li.ui-draggable`` rows) is selected from config instead of
    the hardcoded defaults. An empty dict means "no matching viewer — keep the
    hardcoded shared defaults", and *any* failure (missing file, bad YAML,
    unreadable page URL) also yields ``{}`` so a broken config can never abort
    capture.
    """
    try:
        url = getattr(page, "url", None)
        if not url:
            return {}
        viewers_path = _SERIES_VIEWERS_YAML
        with open(viewers_path, "r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
        viewers = (data or {}).get("viewers") or {}
        for _name, viewer in viewers.items():
            if not isinstance(viewer, dict):
                continue
            patterns = viewer.get("url_patterns") or []
            if any(pattern and str(pattern) in str(url) for pattern in patterns):
                sequence_select = viewer.get("sequence_select") or {}
                cfg: dict[str, object] = {}
                for key in ("item_container_selector", "item_selector", "identity_attrs"):
                    if key in sequence_select and sequence_select.get(key) is not None:
                        cfg[key] = sequence_select[key]
                return cfg
    except Exception:
        return {}
    return {}


def _capture_series_list_full(
    root_locator: object,
    capture_root: Path,
    document_id: str,
    page: object,
) -> tuple[str | None, int]:
    """Scroll-stitch a full-content screenshot of the series list container.

    The real list panel is taller than the viewport, so a single element
    screenshot would miss rows below the fold (and for virtualized lists, rows
    that are not yet rendered). We capture the container window at every scroll
    position and stitch the tiles into one tall image covering the whole
    ``scrollHeight``, then restore the original scroll position. Returns
    ``(asset_relpath_relative_to_capture_root, content_height)``, or
    ``(None, 0)`` when the container does not overflow its window.
    """
    try:
        metrics = root_locator.evaluate(
            "el => ({top: el.scrollTop, clientH: el.clientHeight, scrollH: el.scrollHeight})"
        )
        client_h = max(1, int(metrics["clientH"]))
        if metrics["scrollH"] <= metrics["clientH"] + 2:
            return None, 0
        root_locator.evaluate("""el => {
            const s = document.createElement('style');
            s.id = '__replica_noscrollbar';
            s.textContent = '*::-webkit-scrollbar{display:none !important}';
            document.head.appendChild(s);
        }""")
        tiles: list[bytes] = []
        steps = math.ceil(metrics["scrollH"] / client_h)
        for index in range(steps):
            root_locator.evaluate("el => el.scrollTop = %d" % (index * client_h))
            page.wait_for_timeout(120)
            tiles.append(root_locator.screenshot(type="png"))
        root_locator.evaluate("""el => {
            el.scrollTop = 0;
            const s = document.getElementById('__replica_noscrollbar');
            if (s) s.remove();
        }""")
        widths = {Image.open(io.BytesIO(tile)).size[0] for tile in tiles}
        width = max(widths) if widths else 0
        canvas = Image.new("RGB", (width, int(metrics["scrollH"])), (3, 6, 9))
        offset = 0
        for tile in tiles:
            image = Image.open(io.BytesIO(tile)).convert("RGB")
            canvas.paste(image, (0, offset))
            offset += image.size[1]
        assets_dir = capture_root / "assets"
        assets_dir.mkdir(parents=True, exist_ok=True)
        path = assets_dir / f"series_list_full_{document_id.replace(':', '_')}.jpeg"
        canvas.save(path, "JPEG", quality=90)
        rel = str(path.relative_to(capture_root)).replace("\\", "/")
        return rel, int(metrics["scrollH"])
    except Exception:
        return None, 0


def _series_scope_root(target_locator: object, container_selector: str | None = None) -> object:
    """Return the scrollable series-list container that owns the target, same-frame.

    Mirrors the marker-aware series-region root resolution (walking only the
    target's own frame via ``ancestor::html`` — never a top-level DOM traversal
    cross into a child iframe). When ``container_selector`` is given (from the
    per-viewer config) it is preferred over the generic candidate selectors —
    the first *visible* match inside the target's own frame wins (e.g. FTImage's
    ``div.os-viewport`` has two instances; zscloud's list lives inside the
    viewer's second-level iframe, which ``ancestor::html`` already bounds). Any
    container-matching failure falls through to the existing candidate/body logic.
    """
    scope = target_locator.locator("xpath=ancestor::html")
    if container_selector:
        try:
            matches = scope.locator(container_selector)
            for index in range(matches.count()):
                candidate = matches.nth(index)
                if candidate.is_visible():
                    return candidate
        except Exception:
            pass
    for selector in _MARKER_REGION_CANDIDATES["序列选择"][1]:
        matches = scope.locator(selector)
        for index in range(matches.count()):
            candidate = matches.nth(index)
            try:
                if candidate.is_visible():
                    return candidate
            except Exception:
                continue
    return scope.locator("body")


def _layout_variant_id(member_text: str) -> str | None:
    """Infer a layout variant id from a layout-option member's text.

    ``*1 Shift+1`` -> ``1*1``（``*N`` 简写 = 1×N）；``2*2`` -> ``2*2``；
    ``1*2`` -> ``1*2``；``1×2`` / ``3x3`` 统一归一为 ``N*N``。
    优先级：先匹配 ``N*N``（数字乘数字，含半角 ``*``/``x``/``X`` 与全角 ``×``），
    再无则匹配 ``*N``（星号+数字=1×N）。
    匹配不到（如多列品字布局只有图标无数字文本）返回 None，由调用方跳过该成员。
    """
    text = member_text or ""
    match = re.search(r"(\d+)\s*[*xX×]\s*(\d+)", text)
    if match:
        return f"{match.group(1)}*{match.group(2)}"
    shorthand = re.search(r"(?:^|\s)\*\s*(\d+)(?:\s|$)", text)
    if shorthand:
        return f"1*{shorthand.group(1)}"
    return None


def _canvas_hash_or_none(page: object) -> int | None:
    """Return a canvas fingerprint (or None) used to detect post-click change."""
    try:
        return canvas_hash(page)
    except Exception:
        return None


def _canvas_png_or_none(page: object) -> bytes | None:
    """Return the largest visible canvas PNG (or None), for visual stability sampling.

    ⚠ Dapeng viewer 的页面里有 38 个 68×68 序列缩略图 canvas ——「最大 canvas」
    会截到缩略图而非主影像。这是视觉稳定性采样用（点击后画布是否变化），
    不是布局背景的来源；布局背景应截 viewer 根元素（见 _html_root_png_or_none）。
    """
    try:
        count = page.locator("canvas").count()
    except Exception:
        return None
    if count == 0:
        return None
    best = None
    best_area = -1
    for index in range(min(count, 20)):
        try:
            canvas = page.locator("canvas").nth(index)
            box = canvas.bounding_box()
            if not box or box["width"] <= 0 or box["height"] <= 0:
                continue
            area = box["width"] * box["height"]
            if area > best_area:
                best = canvas
                best_area = area
        except Exception:
            continue
    if best is None:
        best = page.locator("canvas").nth(0)
    try:
        return best.screenshot(type="png")
    except Exception:
        return None


def _html_root_png_or_none(scope: object) -> bytes | None:
    """Return a full-page PNG of the viewer scope's ``<html>`` root (or None).

    布局变体背景用这个而不是单个 canvas：Dapeng 的主影像画布是 div 背景 /
    svg 容器，不是大 canvas；截 html 根得到整个 1×1/2×2 布局的完整画面
    （~1888×880），铺满 replica-bg 才是「布局切换」的正确视觉效果。
    ``scope`` 是 bound 到 viewer frame 的 locator/FrameLocator（有 .locator）。
    """
    try:
        html_root = scope.locator("html")
        if html_root.count() == 0:
            return None
        return html_root.first.screenshot(type="png", animations="disabled")
    except Exception:
        return None


def _save_png_by_hash(png_bytes: bytes, capture_root: Path, document_id: str, variant_id: str) -> str | None:
    """Persist a layout background PNG under ``assets/by-hash/<sha256>.jpeg``.

    返回相对 ``capture_root`` 的 relpath；失败返回 None。复用 capture_page_topology 的
    by-hash 落盘语义：JPEG + SHA-256 命名，重复内容只存一份。
    """
    try:
        digest = hashlib.sha256(png_bytes).hexdigest()
        asset_dir = capture_root / "assets" / "by-hash"
        asset_dir.mkdir(parents=True, exist_ok=True)
        jpeg_path = asset_dir / f"{digest}.jpeg"
        if not jpeg_path.exists():
            with Image.open(io.BytesIO(png_bytes)) as image:
                image.convert("RGB").save(jpeg_path, format="JPEG", quality=95, optimize=True)
        return str(jpeg_path.relative_to(capture_root)).replace("\\", "/")
    except Exception:
        return None


def _sample_layout_background(
    page: object,
    canvas_screenshot_fn: Callable[[], bytes | None],
    capture_root: Path,
    document_id: str,
    variant_id: str,
    poll_interval_s: float = 0.15,
    stability_timeout_s: float = 1.5,
) -> str | None:
    """Sample one layout variant's stable viewer background, saving by-hash.

    轮询等待画布稳定：``canvas.width > 0``（画布已重建）是前置条件；满足后连续两次
    PNG（像素级）不可变即稳定。上限 ``stability_timeout_s``，超时返回 None（该变体失败，
    不入 ``layout_variants``，绝不阻断整个 marker 组）。
    """
    deadline = time.monotonic() + stability_timeout_s
    previous: bytes | None = None
    seen_nonempty = False
    while time.monotonic() <= deadline:
        # ⚠ 稳定性取决于「截图是否非空且连续两次不变」——不依赖 canvas 定位
        # （Dapeng 主影像画布是 div/svg 而非大 canvas；顶层 page 跨 frame 也查不到）。
        # 背景截图是 viewer html 根整页，布局切换后整页画面变化即视为不稳定的
        # 前兆，回到稳定 = 连续两次 PNG 相同。
        current = canvas_screenshot_fn()
        if current is not None:
            # 非空白判定 OR：截图尺寸近似整页（>50×50）或页面有可见 canvas。
            shot_big = False
            try:
                import io as _io
                from PIL import Image as _Image
                with _Image.open(_io.BytesIO(current)) as im:
                    _w, _h = im.size
                shot_big = _w > 50 and _h > 50
            except Exception:
                shot_big = False
            canvas_ok = False
            try:
                canvas_ok = int(
                    page.evaluate(
                        """() => {
                            const canvases = Array.from(document.querySelectorAll('canvas'));
                            return canvases.some(c => c.getBoundingClientRect().width > 0) ? 1 : 0;
                        }"""
                    )
                ) > 0
            except Exception:
                canvas_ok = False
            seen_nonempty = shot_big or canvas_ok
        if seen_nonempty:
            if current is not None and previous is not None and current == previous:
                return _save_png_by_hash(current, capture_root, document_id, variant_id)
            if current is not None:
                previous = current
        time.sleep(poll_interval_s)
    return None


def _sample_all_layout_variants(
    page: object,
    layout_root: object | None,
    target_document: ReplicaDocument,
    capture_root: Path,
    log: Callable[[str], None] = lambda message: sys.stderr.write(message + "\n"),
    viewer_scope: object | None = None,
) -> tuple[dict[str, str], str]:
    """(步骤 2) 连点采样所有可见布局选项的背景帧，回填 ``layout_variants``。

    输入 ``layout_root`` 是已解析的 ``#cellStyle`` 容器 locator（可能为 None），
    ``target_document`` 是布局 region 所在的 document（回填 ``layout_variants`` +
    ``default_layout``）。``viewer_scope`` 是 bound 到 viewer frame 的
    locator/FrameLocator（背景截图用它截整页 html 根）。返回 ``(variants, default_layout)``：
    - 对每个可见布局选项成员，点击 → 等画布稳定（canvas.width>0 轮询 + 1.5s 上限，
      勿用纯固定 sleep）→ 截 viewer 整页背景 → by-hash 落盘；
    - variant_id 从成员文本/title 推断（``*1 Shift+1`` -> ``1*1``，``2*2`` -> ``2*2``）；
    - 单个选项失败（画布无变化/浮层不可见）不入 variants，记 warning，绝不阻断整组；
    - 仅当所有变体都失败时降级为 partial（记 ``layout_capture_partial`` 到 log）。
    """
    variants: dict[str, str] = {}
    raw_default = target_document.default_layout or ""
    if layout_root is None:
        log("layout capture skipped: #cellStyle 容器不可解析")
        return variants, raw_default
    # 背景截图：优先 viewer html 根（整页布局画面），退化为最大 canvas（稳定性采样）。
    def background_shot() -> bytes | None:
        if viewer_scope is not None:
            shot = _html_root_png_or_none(viewer_scope)
            if shot is not None:
                return shot
        return _canvas_png_or_none(page)

    def current_hash() -> int | None:
        shot = background_shot()
        if shot is None:
            return None
        return hashlib.sha256(shot).digest()[:8]
    try:
        members = layout_root.locator("button, a, [role='button'], [role='menuitem'], li, [class*='cell']")
        count = members.count()
    except Exception as exc:
        log(f"layout capture error: {type(exc).__name__}: {exc}")
        return variants, raw_default
    captured: list[str] = []
    for index in range(count):
        try:
            option = members.nth(index)
        except Exception:
            continue
        try:
            if not option.is_visible():
                continue
            # Dapeng 布局选项 button 的文本为空、布局规格在 title 属性里
            # （``title="2*2 Shift+4"``）；先 title/aria-label，再 innerText 兜底。
            hint = (
                option.get_attribute("title") or option.get_attribute("aria-label")
                or option.inner_text() or option.text_content() or ""
            ).strip()
        except Exception:
            continue
        variant_id = _layout_variant_id(hint)
        if variant_id is None:
            continue  # 无数字文本的图标项（品字等）不入 variants
        before_hash = current_hash()
        try:
            option.click(trial=True)
            option.click()
        except Exception:
            continue  # 点击失败：该变体跳过，不阻断
        if before_hash is not None and current_hash() == before_hash:
            # 点击后画布指纹未变化：该布局选项在当前序列/层级下无内容或切换失败，
            # 直接跳过该变体（降级），绝不阻断整个 marker 组。
            log(f"layout variant skipped (canvas hash unchanged): {variant_id}")
            continue
        sampled = _sample_layout_background(
            page,
            background_shot,
            capture_root,
            target_document.document_id,
            variant_id,
        )
        if sampled is None:
            log(f"layout variant failed (稳定帧/背景不可得): {variant_id}")
            continue
        variants[variant_id] = sampled
        if variant_id not in captured:
            captured.append(variant_id)
    if captured and not raw_default:
        raw_default = captured[0]
    if not variants:
        log("layout_capture_partial: 所有布局变体均未捕获到稳定背景")
    return variants, raw_default


def _resolve_locator_recipe(recipe: LocatorRecipe, pages: dict[str, object]) -> object | None:
    """Re-resolve a serialized LocatorRecipe into a live Playwright Locator.

    Almost never caches: each call returns a freshly resolved Locator bound to
    the current live page/frame, so virtualized-list re-location stays valid.
    """
    page_obj = pages.get(recipe.page_var)
    if page_obj is None:
        return None
    locator = page_obj
    for hop in recipe.frame_chain:
        try:
            locator = locator.frame_locator(hop.selector)
        except Exception:
            return None
    locator_args = dict(recipe.locator_args or {})
    filter_kwargs = locator_args.pop("_filter", {})
    positional = list(locator_args.get("args", []))
    keywords = {key: value for key, value in locator_args.items() if key != "args"}
    method = {
        "css": "locator", "role": "get_by_role", "text": "get_by_text",
        "test_id": "get_by_test_id", "label": "get_by_label", "title": "get_by_title",
    }.get(recipe.locator_kind, "locator")
    try:
        resolved = getattr(locator, method)(*positional, **keywords)
        if filter_kwargs:
            resolved = resolved.filter(**filter_kwargs)
        if recipe.ordinal_op == "first":
            resolved = resolved.first
        elif recipe.ordinal_op == "last":
            resolved = resolved.last
        elif recipe.ordinal_op == "nth":
            resolved = resolved.nth(recipe.ordinal_value or 0)
        return resolved
    except Exception:
        return None


def _live_pages_map(page: object) -> dict[str, object]:
    """Build ``{page_var: page}`` from the context, mirroring replay conventions."""
    try:
        context_pages = list(page.context.pages) if getattr(page, "context", None) else [page]
    except Exception:
        context_pages = [page]
    return {("page" if index == 0 else f"page{index}"): candidate for index, candidate in enumerate(context_pages)}


def _find_viewer_frame(page: object) -> object:
    """Return the frame holding the largest/any canvas, else the main frame."""
    try:
        frames = list(page.frames)
    except Exception:
        return page
    for frame in frames:
        try:
            if frame.locator("canvas").count() > 0:
                return frame
        except Exception:
            continue
    return frames[0] if frames else page


_VIEWER_IDENTITY_SELECTORS = (
    "#current-series",
    "[data-current-series]",
    ".series-current",
    "[class*='current-series' i]",
    "[class*='current_series' i]",
    "[aria-current='true']",
    "[class*='current' i]",
)


def _viewer_current_series_label(viewer_frame: object) -> str | None:
    """Return the normalized label of the series the Viewer currently displays.

    The readiness logic must compare the *viewer's* displayed identity to the
    target descriptor, never the target row's own label (which is constant once
    the locator resolves and would otherwise be a tautology). This looks for a
    common "current series / selected series" indicator slot in the viewer frame;
    returns ``None`` when no such slot is present or readable.
    """
    for selector in _VIEWER_IDENTITY_SELECTORS:
        try:
            matches = viewer_frame.locator(selector)
            for index in range(min(matches.count(), 8)):
                item = matches.nth(index)
                text = (item.inner_text() or item.text_content() or "").strip()
                if text:
                    normalized = _normalize_series_text(text)
                    if normalized and normalized not in {"", " ", "current", "series"}:
                        return normalized
        except Exception:
            continue
    return None


def _capture_viewer_small_screenshot(frame: object) -> bytes | None:
    """Return a compact PNG of the viewer frame for non-blank readiness checks."""
    try:
        return frame.locator("html").screenshot(type="png")
    except Exception:
        return None


def _locate_series_row(
    root_locator: object,
    descriptor: SeriesDescriptor,
    max_scroll_steps: int = 40,
    item_selector: str | None = None,
) -> tuple[object | None, int]:
    """Re-locate the target series row after virtual-list scrolling.

    Re-parses the item locator on demand and scrolls through the list (restoring
    the original scroll position on every exit) until a row whose stable
    attributes / normalized text match the descriptor resolves and is visible.
    ``item_selector`` overrides the default row selector; the activation path
    must pass the configured per-viewer selector or rows like FTImage's
    ``a:has(span.total)`` are never found (the default ``option, [...], li``
    does not match them).
    Returns ``(locator_or_None, scroll_steps_taken)``.
    """
    try:
        initial_top = root_locator.evaluate("element => element.scrollTop")
    except Exception:
        return None, 0
    try:
        items = root_locator.locator(item_selector or _SERIES_ITEM_SELECTOR)
        try:
            root_locator.evaluate("element => element.scrollTop = 0")
        except Exception:
            pass
        for step in range(max_scroll_steps + 1):
            try:
                count = items.count()
            except Exception:
                count = 0
            for index in range(count):
                item = items.nth(index)
                try:
                    if not item.is_visible():
                        continue
                    snapshot = capture_locator_snapshot(item)
                except Exception:
                    continue
                if snapshot is None:
                    continue  # 该行瞬变无匹配：_matches_descriptor 不能收 None
                if _matches_descriptor(snapshot, descriptor):
                    return item, step
            try:
                previous = root_locator.evaluate("element => element.scrollTop")
                current = root_locator.evaluate("element => { element.scrollTop += Math.max(1, element.clientHeight); return element.scrollTop; }")
            except Exception:
                break
            if current <= previous:
                break
        return None, max_scroll_steps
    finally:
        try:
            root_locator.evaluate("(element, top) => element.scrollTop = top", initial_top)
        except Exception:
            pass


class LiveCaptureSession:
    """Persist only marked-action page/frame snapshots during a live replay."""

    def __init__(self, output_root: str | Path) -> None:
        self.output_root = Path(output_root)
        self.series_branches_root = self.output_root / "series_branches"
        self._series_cfg: dict[str, object] = {}

    def _ensure_series_cfg(self, page: object) -> None:
        """Load the per-viewer series discovery config once, when the page has a URL.

        Caches the first successful load in ``self._series_cfg`` so repeated
        per-action hooks do not re-read the YAML on every call. An empty result
        (unknown viewer / config failure) is deliberately not cached as
        "definitive" — nothing is cached at all for those, so a later call on a
        page with a real URL re-tries the match.
        """
        if self._series_cfg:
            return
        if not getattr(page, "url", None):
            return
        self._series_cfg = _series_viewer_config_for(page)

    def _capture(
        self,
        action_id: str,
        phase: str,
        page: object,
        locator_factory: object | None = None,
        marker_label: str = "",
    ) -> None:
        capture_root = self.output_root / "snapshots" / action_id / phase
        self._ensure_series_cfg(page)
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
                # The recorded locator may match several elements (e.g. a series
                # list with many items); the frame-owner probe only needs one, so
                # resolve `.first` — a strict-mode evaluate on a multi-match
                # locator would otherwise raise and silently drop the snapshot.
                frame_owner = target_locator.first.evaluate("""element => {
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
                if marker_label == "序列选择":
                    # Per-viewer row structure (item_selector / identity_attrs
                    # from viewers.yaml) lets the top-page variant of the series
                    # harvest enumerate real-site rows, not just hardcoded ones.
                    region = capture_marker_interaction_region(
                        page, marker_label, target_document.document_id, target_locator,
                        item_selector=self._series_cfg.get("item_selector"),
                        identity_attrs=self._series_cfg.get("identity_attrs"),
                    )
                else:
                    region = capture_marker_interaction_region(page, marker_label, target_document.document_id, target_locator)
            elif marker_label in {"Meta 信息工具", "序列选择"}:
                # Nested-frame markers are captured from the frame's own context,
                # not the top page. Metadata panels have long used the owning
                # document (ancestor html rooted in the target's frame); series
                # lists must route through the same marker-aware path so the
                # 序列选择 scroll harvest runs inside the (possibly innermost)
                # frame that actually holds the list. Generic/layout/WLWW/canvas
                # markers keep the existing else behavior.
                owning_document = target_locator.locator("xpath=ancestor::html")
                if marker_label == "序列选择":
                    region = capture_marker_interaction_region(
                        owning_document,
                        marker_label,
                        target_document.document_id,
                        target_locator,
                        item_selector=self._series_cfg.get("item_selector"),
                        identity_attrs=self._series_cfg.get("identity_attrs"),
                    )
                else:
                    region = capture_marker_interaction_region(
                        owning_document,
                        marker_label,
                        target_document.document_id,
                        target_locator,
                    )
            else:
                region = capture_interaction_region(target_locator.locator("xpath=.."), marker_region_type(marker_label), target_document.document_id)
            if region is not None:
                target_document.regions.append(region)
            if marker_label == "序列选择" and target_locator is not None:
                container = _series_scope_root(
                    target_locator,
                    (self._series_cfg or {}).get("item_container_selector"),
                )
                rel, content_h = _capture_series_list_full(
                    container, capture_root, target_document.document_id, page
                )
                if rel:
                    target_document.series_list_full_asset_relpath = rel
                    target_document.series_list_content_height = content_h
            # (步骤 2) 布局捕获扩展：对「序列布局切换」marker 的 after 快照，连点采样
            # 所有可见布局选项的背景帧，回填 target_document.layout_variants / default_layout，
            # 再写 topology.json（asdict 序列化时带这两个字段）。真实连点仅在浏览器可访问时生效；
            # 任何失败都降级（该变体不入 variants / 全部失败记 layout_capture_partial），
            # 绝不阻断整个 marker 组。
            try:
                # ⚠ 布局采样是 live 交互增强；任何异常只降级，绝不外抛——
                # 外抛会让 after 快照丢失（a_001_002 空 after），action 无快照对
                # 被跳过，整个布局 marker 组消失（Z1 模式）。
                if marker_label == "序列布局切换" and phase == "after":
                    layout_region = next(
                        (region for region in target_document.regions if region.region_type == "layout"),
                        None,
                    )
                    layout_root = None
                    viewer_scope = None  # bound 到 viewer frame 的 locator（布局浮层/整页截图所在）
                    # layout region 存在且 root 非 None 时，从 DOM 解析 #cellStyle 容器。
                    # 无 region（无匹配/无布局浮层）则跳过采样，保持兼容老 viewer。
                    if layout_region is not None and layout_region.root is not None:
                        try:
                            # ⚠ 布局浮层 #cellStyle 在 viewer iframe 内（page1 的
                            # content_frame）。headed 回放下 _find_viewer_frame 能
                            # 从 page.frames 拿到真 viewer 帧（有 evaluate/locator）；
                            # 兜底再用录制 locator 的 xpath=ancestor::html。
                            viewer_scope = _find_viewer_frame(page)
                            if viewer_scope is None or getattr(viewer_scope, "locator", None) is None:
                                viewer_scope = None
                                if target_locator is not None:
                                    try:
                                        scope_candidate = target_locator.locator("xpath=ancestor::html")
                                        if scope_candidate.count() > 0:
                                            viewer_scope = scope_candidate.first
                                    except Exception:
                                        viewer_scope = None
                            scope = viewer_scope if viewer_scope is not None else page
                            candidates = ["#cellStyle", "[id*='cellStyle' i]", "[class*='cellStyle' i]"]
                            layout_root = scope.locator(candidates[0])
                            if layout_root.count() == 0:
                                layout_root = None
                        except Exception:
                            layout_root = None
                    variants, default_layout = _sample_all_layout_variants(
                        page,
                        layout_root, target_document, capture_root,
                        viewer_scope=viewer_scope,
                    )
                    if variants:
                        target_document.layout_variants = variants
                        target_document.default_layout = default_layout
            except Exception as layout_error:
                sys.stderr.write(f"layout capture degraded: {type(layout_error).__name__}: {layout_error}\n")
        (capture_root / "topology.json").write_text(
            json.dumps({"pages": [asdict(item) for item in pages], "documents": [asdict(item) for item in documents]}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        if target_locator is not None:
            # 步骤 1：不再静默吞多匹配/无匹配。capture_locator_snapshot 内部已做
            # ``.first`` 归一（多匹配不再抛 strict-mode）且 count()==0 返回 None。
            # 这里只对「真·无匹配 / evaluate 异常」显式记录 warning：target.json 缺失
            # 会让 build 侧标 ``missing_target_evidence``，使离线转场无载体可审计。
            target = None
            closure = None
            try:
                target = capture_locator_snapshot(target_locator)
            except Exception as exc:
                sys.stderr.write(f"snapshot error: {action_id} → {type(exc).__name__}: {exc}\n")
            if target is None:
                sys.stderr.write(f"snapshot missing: {action_id} → 该 action 的离线转场无载体\n")
            else:
                (capture_root / "target.json").write_text(json.dumps(asdict(target), ensure_ascii=False, indent=2), encoding="utf-8")
                try:
                    closure = capture_selector_closure(target_locator, action_id)
                except Exception as exc:
                    sys.stderr.write(f"selector closure error: {action_id} → {type(exc).__name__}: {exc}\n")
                if closure is not None:
                    (capture_root / "selector_closure.json").write_text(json.dumps(asdict(closure), ensure_ascii=False, indent=2), encoding="utf-8")

    def before(self, action_id: str, page: object, locator_factory: object | None = None, marker_label: str = "") -> None:
        self._ensure_series_cfg(page)
        wait_for_pre_action_state(page, marker_label, locator_factory)
        self._capture(action_id, "before", page, locator_factory, marker_label)

    def after(self, action_id: str, page: object, locator_factory: object | None = None, marker_label: str = "") -> None:
        self._ensure_series_cfg(page)
        ensure_post_action_state(page, marker_label, locator_factory)
        self._capture(action_id, "after", page, locator_factory, marker_label)

    def expand_series(
        self,
        page: object,
        series_locator_factory: object | None = None,
        series_action_id: str = "",
        close_action_id: str = "",
    ) -> None:
        """Run the bounded all-series exploration when the template is complete.

        This is the session-side expansion entry that ``capture_hook_expand_series``
        delegates to. It reconstructs the recording template from the instrumented
        replay source (the only stable source available inside the subprocess) and
        delegates to ``finalize_series_branches``. It never touches the web outside
        the instrumented replay subprocess and performs no implicit after-snapshot
        (that stays ``after()``'s single duty).
        """
        self._ensure_series_cfg(page)
        replay_source = self.output_root / "instrumented_replay.py"
        try:
            if not replay_source.is_file():
                return
            plan = parse_action_plan(replay_source.read_text(encoding="utf-8"))
        except Exception:
            return
        template = classify_recording_template(plan)
        if not template.complete:
            return
        # A failure here is recorded as an event by ``capture_hook_expand_series``;
        # it never escapes the instrumented replay's action boundary.
        self.finalize_series_branches(
            page,
            template,
            series_action_id=series_action_id,
            close_action_id=close_action_id,
        )

    def _capture_series_region(
        self,
        root: object,
        viewer_doc: ReplicaDocument,
        descriptor: SeriesDescriptor,
        max_scroll_steps: int,
    ) -> str:
        """(P0#1/P0#3) Harvest the series region into ``viewer_doc`` and return
        the member id that corresponds to ``descriptor`` in the viewer's own region.

        Runs the same ``discover_series_candidates`` scroll harvest (the single
        shared algorithm) against the recording template's series root, packages
        the members + ``SeriesCollectionEvidence`` into a ``region_type="series"``
        ``InteractionRegion``, and appends it to ``viewer_doc.regions`` so the
        offline replica's Viewer page can enumerate/click the other sequences.

        The returned member id is bound (not the discovery-local ``d_series_hub``
        id) so the builder's ``source_member_id -> route`` lookup hits a real
        member in this branch's own document. Only Frame/FrameLocator/Locator are
        used; no ``contentDocument`` traversal.
        """
        descriptors, members, evidence = discover_series_candidates(
            root, viewer_doc.document_id, max_scroll_steps=max_scroll_steps,
            item_selector=self._series_cfg.get("item_selector"),
            identity_attrs=self._series_cfg.get("identity_attrs"),
        )
        root_snapshot = capture_locator_snapshot(root)
        if root_snapshot is None:
            # root 真·无匹配：跳过整个 series vector 添加（调用方 _capture_viewer_topology
            # 已有外层 try/except 兜底，这里不再让 InteractionRegion 收 None root）。
            return descriptor.member_id
        viewer_doc.regions.append(InteractionRegion(
            f"{viewer_doc.document_id}_series", "series", viewer_doc.document_id,
            root_snapshot, members, evidence,
        ))
        # Bind to the member that matches the descriptor by stable description
        # (member ids differ across documents, so match on attributes/text).
        for candidate in descriptors:
            if _series_descriptor_matches(candidate, descriptor):
                return candidate.member_id
        # Fallback: reuse the descriptor's own (hub) member id; it will still be
        # rendered by the generic member path even if route binding misses.
        return descriptor.member_id

    def _capture_viewer_topology(
        self,
        session_dir: Path,
        page: object,
        pages: dict[str, object],
        root: object,
        descriptor: SeriesDescriptor,
        max_scroll_steps: int,
    ) -> tuple[list[ReplicaPage], list[ReplicaDocument], str]:
        """Capture viewer topology and stitch the series region into the entry doc.

        Returns ``(pages_out, docs_out, source_member_id)``.
        """
        pages_out, docs_out = capture_page_topology(
            [("page" if index == 0 else f"page{index}", candidate) for index, candidate in enumerate(pages.values())],
            session_dir / "viewer",
        )
        source_member_id = descriptor.member_id
        if docs_out:
            try:
                source_member_id = self._capture_series_region(
                    root, docs_out[0], descriptor, max_scroll_steps=max_scroll_steps
                )
            except Exception:
                source_member_id = descriptor.member_id
        self._write_topology(session_dir / "viewer", pages_out, docs_out)
        return pages_out, docs_out, source_member_id

    @staticmethod
    def _write_topology(topology_root: Path, pages: list[ReplicaPage], documents: list[ReplicaDocument]) -> None:
        topology_root.mkdir(parents=True, exist_ok=True)
        (topology_root / "topology.json").write_text(
            json.dumps({"pages": [asdict(item) for item in pages], "documents": [asdict(item) for item in documents]},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def capture_one_series(
        self,
        page: object,
        descriptor: SeriesDescriptor,
        template: RecordingTemplate,
        pages: dict[str, object],
        config: dict[str, object] | None,
    ) -> CaptureBranchOutcome:
        """Capture one series branch as a bounded single-series transaction.

        Re-parses Page/Frame and series locators on *every* call (no cached
        Locator), re-locates the target row after virtual-list scrolling,
        inherits the recorded click/dblclick, waits on a *combination* of
        readiness evidence (never a fixed sleep alone), captures the Viewer and
        Metadata snapshots, and restores scrollTop / hub state in ``finally``
        (P1#5). Returns an outcome dataclass; raises ``HubUnrecoverableError``
        for hub-level failures so the caller can drive a controlled reload.
        """
        self._ensure_series_cfg(page)
        container = self._series_cfg.get("item_container_selector")
        session_dir = self.series_branches_root / _safe_series_key(descriptor)
        metadata_dir = session_dir / "metadata"
        session_dir.mkdir(parents=True, exist_ok=True)

        per_series_timeout_s = float(config.get("per_series_timeout_s") or 20) if config else 20.0
        max_series = int(config.get("max_series") or 40) if config else 40

        (session_dir / "descriptor.json").write_text(
            json.dumps(asdict(descriptor), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        status: dict[str, object] = {
            "branch_id": _safe_series_key(descriptor),
            "series_key_sha256": hashlib.sha256(
                f"{descriptor.document_id}::{descriptor.series_key}".encode("utf-8")
            ).hexdigest(),
            "ordinal": descriptor.ordinal,
            "capture_status": "failed",
            "fail_stage": "init",
            "error_type": None,
            "activation": descriptor.activation or "click",
        }
        root: object | None = None
        initial_hub: dict[str, object] | None = None
        source_member_id = descriptor.member_id
        try:
            status["fail_stage"] = "reparse"
            recipe = template.series_action.locator
            series_locator = _resolve_locator_recipe(recipe, pages)
            if series_locator is None:
                # The series list / hub is unreachable: classify as hub-unrecoverable
                # so finalize_series_branches can drive one controlled reload.
                raise HubUnrecoverableError("series list locator unresolved")
            root = _series_scope_root(series_locator, container)
            # P1#5: snapshot the hub/panel state once for this transaction so a
            # finally-restore always runs on every exit path.
            initial_hub = self._snapshot_hub_state(root, template, pages)
            # P1#4: resolve the viewer frame fresh for this transaction (no cache
            # of a pre-activation Frame/Locator across polls).
            viewer_frame = _find_viewer_frame(page)
            previous_canvas_hash = canvas_hash(viewer_frame)

            status["fail_stage"] = "locate"
            row_locator, _steps = _locate_series_row(
                root, descriptor, max_scroll_steps=max_series,
                item_selector=self._series_cfg.get("item_selector"),
            )
            if row_locator is None:
                raise HubUnrecoverableError("target series row not found in hub")

            status["fail_stage"] = "activate"
            activation = descriptor.activation or "click"
            self._perform_activation(row_locator, activation)

            status["fail_stage"] = "readiness"
            # P1#4: every poll re-resolves the series root from the stable recipe
            # against the latest pages/frame (never a cached pre-activation
            # root_locator), so an iframe/root replacement after activation is
            # recovered rather than pinning a stale Locator.
            ready = self._wait_for_series_ready(
                page=page, recipe=recipe, descriptor=descriptor,
                previous_canvas_hash=previous_canvas_hash, timeout_s=per_series_timeout_s,
            )
            if not ready:
                # First action produced no change evidence; re-resolve the root
                # from the recipe and re-locate the row (the Viewer may have
                # rebuilt its iframe / virtualized list) and retry activation once.
                retry_root = self._reparse_series_root(recipe, page, container)
                if retry_root is None:
                    raise HubUnrecoverableError("series root lost on retry")
                row_locator, _steps = _locate_series_row(
                    retry_root, descriptor, max_scroll_steps=max_series,
                    item_selector=self._series_cfg.get("item_selector"),
                )
                if row_locator is None:
                    raise HubUnrecoverableError("target series row lost on retry")
                self._perform_activation(row_locator, activation)
                ready = self._wait_for_series_ready(
                    page=page, recipe=recipe, descriptor=descriptor,
                    previous_canvas_hash=previous_canvas_hash, timeout_s=per_series_timeout_s,
                )

            status["fail_stage"] = "viewer_capture"
            pages_out, docs_out, source_member_id = self._capture_viewer_topology(
                session_dir, page, pages, root, descriptor, max_scroll_steps=max_series
            )

            status["fail_stage"] = "metadata"
            meta = self._capture_metadata_transaction(page, template, pages, metadata_dir, per_series_timeout_s, descriptor)
            meta_open_dom = meta.get("meta_open_dom")
            if meta_open_dom is not None:
                (session_dir / "meta_open_target.json").write_text(
                    json.dumps(asdict(meta_open_dom), ensure_ascii=False, indent=2), encoding="utf-8"
                )

            if meta["ok"]:
                status["metadata_captured"] = True
                if meta.get("uid_hash_prefix"):
                    status["metadata_uid_sha256_prefix"] = meta["uid_hash_prefix"]
                status["capture_status"] = "captured"
                status["fail_stage"] = None
            else:
                # Viewer already succeeded; metadata failure degrades to partial.
                status["capture_status"] = "partial"
                status["fail_stage"] = meta["fail_stage"]
                status["error_type"] = meta["error_type"]

            if not ready and status["capture_status"] == "captured":
                # No independent change evidence but viewer captured: honest partial.
                status["capture_status"] = "partial"
                status["warning"] = "no_visual_change_evidence"

            status["source_member_id"] = source_member_id
            (session_dir / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
        except HubUnrecoverableError as error:
            # P1#5: hub unrecoverable -> persist a terminal failed status and let
            # finalize_series_branches drive the controlled reload.
            status["capture_status"] = "failed"
            status["fail_stage"] = status.get("fail_stage") or "hub_unrecoverable"
            status["error_type"] = type(error).__name__
            if not status.get("warning"):
                status["warning"] = "series_hub_unrecoverable"
            (session_dir / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
            raise
        except Exception as error:
            # Persist a terminal failed status even on an unexpected exception so
            # the branch remains a first-class, auditable entry in the flow, then
            # re-raise for the caller (finalize_series_branches) to isolate it.
            status["capture_status"] = "failed"
            status["fail_stage"] = status.get("fail_stage") or "transaction"
            status["error_type"] = type(error).__name__
            if not status.get("warning"):
                status["warning"] = "series_capture_failed"
            (session_dir / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
            raise
        else:
            # P1#5: per-branch restore + VERIFY on the clean (no-exception) path.
            # A failed restoration (panel not hidden, hub inoperable, or
            # selection/scroll not restored) is never swallowed: it degrades the
            # branch to partial and is recorded in status before the outcome is
            # built.
            if root is not None and initial_hub is not None:
                try:
                    restore_ok, restore_problem = self._restore_hub_state(page, root, template, pages, initial_hub)
                except Exception as error:
                    restore_ok, restore_problem = False, type(error).__name__
                if not restore_ok:
                    status["capture_status"] = "partial"
                    status["fail_stage"] = status.get("fail_stage") or "restore"
                    status["error_type"] = status.get("error_type") or restore_problem
                    if not status.get("warning"):
                        status["warning"] = "hub_restore_failed"
            (session_dir / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
            return self._outcome(descriptor, status)
        finally:
            # Safety net for exceptional exits (failed/partial raised paths):
            # best-effort restoration, but never mask the original exception.
            # A restore failure on an exceptional path is surfaced via a warning
            # on the already-persisted status rather than silently swallowed.
            if root is not None and initial_hub is not None:
                try:
                    restore_ok, restore_problem = self._restore_hub_state(page, root, template, pages, initial_hub)
                    if not restore_ok and not status.get("warning"):
                        status["warning"] = "hub_restore_failed"
                except Exception:
                    pass

    def _outcome(self, descriptor: SeriesDescriptor, status: dict[str, object]) -> CaptureBranchOutcome:
        return CaptureBranchOutcome(
            branch_id=str(status.get("branch_id")),
            series_key=descriptor.series_key,
            label=descriptor.label,
            ordinal=descriptor.ordinal,
            document_id=descriptor.document_id,
            source_member_id=str(status.get("source_member_id") or descriptor.member_id),
            activation=descriptor.activation or "click",
            capture_status=str(status.get("capture_status")),
            fail_stage=status.get("fail_stage"),
            error_type=status.get("error_type"),
            warning=status.get("warning"),
            metadata_captured=bool(status.get("metadata_captured")),
            metadata_uid_hash_prefix=status.get("metadata_uid_sha256_prefix"),
        )

    def _meta_factory(self, action: ActionTarget | None, pages: dict[str, object]):
        if action is None or action.locator is None:
            return None
        recipe = action.locator

        def factory():
            return _resolve_locator_recipe(recipe, pages)

        return factory

    def _metadata_scope_factory(self, template: RecordingTemplate, pages: dict[str, object]):
        """Resolve the latest still-attached Metadata-open locator.

        Popover entries such as FTImage's ``Tags`` link are removed after the
        click.  Prefer the final open step while it remains attached, then fall
        back through earlier stable triggers in the same recorded chain.
        """
        factories = [self._meta_factory(action, pages) for action in template.metadata_open_actions]
        factories = [factory for factory in factories if factory is not None]
        if not factories:
            return None

        def factory():
            for candidate_factory in reversed(factories):
                candidate = candidate_factory()
                try:
                    if candidate is not None and candidate.count() > 0:
                        return candidate
                except Exception:
                    continue
            return None

        return factory

    def _perform_activation(self, locator: object, activation: str) -> None:
        if activation == "dblclick":
            locator.dblclick()
        else:
            try:
                locator.click()
            except Exception:
                locator.dblclick()

    def _wait_for_series_ready(
        self,
        page: object,
        recipe: LocatorRecipe | None,
        descriptor: SeriesDescriptor,
        previous_canvas_hash: int | None,
        timeout_s: float,
        stable_s: float = 0.8,
    ) -> bool:
        """Return True once at least two independent readiness evidence types hold stable (P1#4).

        On *every* poll the Viewer frame is re-found and the series root is
        re-resolved from the stable LocatorRecipe against the latest live
        pages/frame, then the target row is re-located from it (never a cached
        Locator/Frame/ElementHandle), because the real Viewer may rebuild its
        iframe or reuse virtualized list nodes after a transition. If the root
        cannot momentarily resolve the ready window resets. A selection is
        satisfied only when :func:`_evidence_satisfied` reports two core
        evidence types held for ``stable_s``; a single
        ``screenshot_nonblank`` or a target's own label never suffices.
        """
        deadline = time.monotonic() + timeout_s
        stable_since: float | None = None
        while time.monotonic() < deadline:
            viewer_frame = _find_viewer_frame(page)  # fresh every poll
            root = self._reparse_series_root(recipe, page, self._series_cfg.get("item_container_selector"))
            if root is None:
                stable_since = None
                time.sleep(0.15)
                continue
            row = self._reparse_target_row(root, descriptor, item_selector=self._series_cfg.get("item_selector"))
            evidence = self._collect_evidence(row, descriptor, viewer_frame, previous_canvas_hash)
            if _evidence_satisfied(evidence):
                stable_since = stable_since or time.monotonic()
                if time.monotonic() - stable_since >= stable_s:
                    return True
            else:
                stable_since = None
            time.sleep(0.15)
        return False

    @staticmethod
    def _reparse_target_row(
        root_locator: object,
        descriptor: SeriesDescriptor,
        item_selector: str | None = None,
    ) -> object | None:
        """Re-locate the target series row from its stable descriptor (P1#4)."""
        row, _steps = _locate_series_row(root_locator, descriptor, item_selector=item_selector)
        return row

    @staticmethod
    def _reparse_series_root(recipe: LocatorRecipe | None, page: object, container_selector: str | None = None) -> object | None:
        """Re-resolve the scrollable series root from a stable LocatorRecipe.

        (P1#4) Resolves the series locator against the *latest* live pages/frame
        (never holding a pre-activation ``root_locator`` across polls), so an
        iframe / series-root replacement after activation is transparently
        recovered. ``container_selector`` (from the per-viewer config) is
        forwarded to :func:`_series_scope_root`; ``None`` keeps the hardcoded
        defaults. Returns ``None`` when the recipe cannot currently resolve.
        """
        if recipe is None:
            return None
        series_locator = _resolve_locator_recipe(recipe, _live_pages_map(page))
        if series_locator is None:
            return None
        try:
            return _series_scope_root(series_locator, container_selector)
        except Exception:
            return None

    def _collect_evidence(
        self,
        target_locator: object,
        descriptor: SeriesDescriptor,
        viewer_frame: object,
        previous_canvas_hash: int | None,
    ) -> set[str]:
        """Score the combination of series-selection readiness evidence (P1#4).

        Evidence is classified so a selection is only declared once *two* change/
        identity signals hold:
        - ``selected``: the (re-parsed) target row reports aria-selected/selected
          or active/current class — an identity signal.
        - ``name_match``: the Viewer's *currently displayed* series identity
          equals the target descriptor (never the row's own label, which is
          constant after the locator resolves).
        - ``canvas_changed``: the Viewer canvas fingerprint differs from the
          pre-activation hash.
        - ``dom_stable``: two consecutive Viewer DOM fingerprints are equal.
        - ``screenshot_nonblank``: coarse non-blank check — recorded but never a
          qualifying success signal (excluded by :func:`_evidence_satisfied`).
        """
        evidence: set[str] = set()
        if target_locator is not None:
            try:
                state = target_locator.evaluate(
                    """element => ({
                        aria_selected: element.getAttribute('aria-selected'),
                        selected_attr: element.hasAttribute('selected'),
                        data_selected: element.getAttribute('data-selected'),
                        active_class: /(^|\\s)(active|current|selected)(\\s|$)/.test(element.className || ''),
                    })"""
                )
            except Exception:
                state = None
            if state:
                if (
                    str(state.get("aria_selected") or "").lower() == "true"
                    or state.get("selected_attr")
                    or str(state.get("data_selected") or "").lower() in ("true", "1", "selected")
                ):
                    evidence.add("selected")
                elif state.get("active_class"):
                    evidence.add("selected")

        # Compare the Viewer's *current displayed* series identity to the target,
        # not the target row's own (constant) label.
        current_label = _viewer_current_series_label(viewer_frame)
        if current_label and descriptor.label and current_label == _normalize_series_text(descriptor.label):
            evidence.add("name_match")

        current_hash = canvas_hash(viewer_frame)
        if previous_canvas_hash is not None and current_hash is not None and current_hash != previous_canvas_hash:
            evidence.add("canvas_changed")

        first = viewer_dom_fingerprint(viewer_frame)
        if first is not None:
            # Take a second sample after a brief poll and compare the two
            # fingerprints *for stability* (equal), not merely for non-emptiness.
            time.sleep(0.2)
            second = viewer_dom_fingerprint(viewer_frame)
            if second is not None and second == first:
                evidence.add("dom_stable")

        small = _capture_viewer_small_screenshot(viewer_frame)
        if screenshot_nonblank(small):
            evidence.add("screenshot_nonblank")

        return evidence

    def _capture_metadata_transaction(
        self,
        page: object,
        template: RecordingTemplate,
        pages: dict[str, object],
        metadata_dir: Path,
        per_series_timeout_s: float,
        descriptor: SeriesDescriptor,
    ) -> dict[str, object]:
        """Open Metadata, wait for a stable signature, capture + parse, then close.

        The captured panel ``outerHTML`` is sanitized before it is persisted
        (P1#8) and is also assembled into the metadata document as a
        ``region_type="metadata"`` InteractionRegion (P0#1) so the offline replica
        renders a complete scrollable Metadata panel. The Metadata *close* result
        is honored: if the panel fails to hide within the bounded wait, the branch
        degrades to partial (an open panel would pollute the next branch).
        """
        meta_close = template.metadata_close
        open_factories = [self._meta_factory(action, pages) for action in template.metadata_open_actions]
        open_factory = self._metadata_scope_factory(template, pages)
        close_factory = self._meta_factory(meta_close, pages)
        meta_dir = metadata_dir
        meta_dir.mkdir(parents=True, exist_ok=True)
        result: dict[str, object] = {"ok": False, "fail_stage": "open", "error_type": None, "uid_hash_prefix": None, "meta_open_dom": None}

        try:
            if not open_factories or any(factory is None for factory in open_factories) or open_factory is None or close_factory is None:
                result["fail_stage"] = "template"
                result["error_type"] = "missing_metadata_locator"
                return result

            for index, step_factory in enumerate(open_factories):
                opened = step_factory()
                if opened is None:
                    result["fail_stage"] = "open"
                    result["error_type"] = "resolve_failed"
                    return result
                # Preserve the first visible trigger (for example FTImage's
                # "更多") as the single offline Metadata transition target.
                if index == 0:
                    try:
                        result["meta_open_dom"] = capture_locator_snapshot(opened)
                    except Exception:
                        pass
                opened.click()

            stable = wait_for_metadata_panel_state(
                page, open_factory, timeout_s=per_series_timeout_s, stable_s=0.6
            )
            rows, raw_outer_html, uid_hash = self._capture_metadata_panel(open_factory)
            if not stable and not raw_outer_html:
                result.update({"ok": False, "fail_stage": "stabilize", "error_type": "metadata_timeout"})
                return result
            if not stable:
                if uid_hash is None:
                    # Content captured but carries no series-identity evidence;
                    # it may be a stale/partial panel from another series. Do not
                    # claim per-series metadata we cannot attribute to this branch.
                    result.update({"ok": False, "fail_stage": "stabilize", "error_type": "metadata_unstable_no_uid"})
                    return result
                result["warning"] = "metadata_unstable_snapshot"
            # P1#8: run the unified HTML sanitizer before any persistence.
            outer_html = sanitize_html(raw_outer_html) if raw_outer_html else ""
            (meta_dir / "metadata_rows.json").write_text(
                json.dumps({"rows": rows, "outer_html": outer_html, "raw_outer_html_lines": 0 if outer_html else None,
                            "uid_sha256_prefix": uid_hash}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            pages_out, docs_out = capture_page_topology(
                [("page" if index == 0 else f"page{index}", candidate) for index, candidate in enumerate(pages.values())],
                meta_dir,
            )
            # P0#1: synthesize a Metadata region into the metadata document.
            if docs_out and outer_html:
                panel_action = template.metadata_open_actions[-1]
                owner_id = _document_id_for_recipe(panel_action.locator, pages_out, docs_out)
                owner = next(
                    (document for document in docs_out if document.document_id == owner_id),
                    docs_out[0],
                )
                self._attach_metadata_region(owner, outer_html)
            self._write_topology(meta_dir, pages_out, docs_out)

            closed = close_factory()
            close_error: str | None = None
            if closed is not None:
                try:
                    closed.click()
                except Exception as error:
                    close_error = type(error).__name__
            # P1#5: the hidden-wait result is effective — a panel that never
            # hides degrades the branch to partial (never a silent success).
            hidden = self._wait_for_metadata_hidden(page, open_factory, timeout_s=min(2.0, per_series_timeout_s))
            if not hidden:
                result.update({"ok": False, "fail_stage": "close", "error_type": "metadata_not_hidden",
                               "warning": "metadata_panel_not_hidden"})
                return result
            if close_error:
                result.update({"ok": True, "fail_stage": None, "uid_hash_prefix": uid_hash,
                               "row_count": len(rows), "close_error": close_error})
                return result

            result.update({
                "ok": True,
                "fail_stage": None,
                "uid_hash_prefix": uid_hash,
                "row_count": len(rows),
            })
            return result
        except Exception as error:
            result.update({"ok": False, "fail_stage": result.get("fail_stage") or "metadata", "error_type": type(error).__name__})
            return result

    @staticmethod
    def _attach_metadata_region(meta_doc: ReplicaDocument, outer_html: str) -> None:
        """Synthesize a ``region_type="metadata"`` InteractionRegion onto a document.

        The region root carries the sanitized full panel ``outerHTML`` (and a
        non-empty extracted text) so the builder's ``_is_metadata_panel`` renders
        it as a ``.replica-metadata`` scrollable panel. Attributes are chosen to
        satisfy the named-panel / dialog identity check.
        """
        panel_snapshot = _metadata_panel_snapshot(meta_doc, outer_html)
        meta_doc.regions.append(InteractionRegion(
            f"{meta_doc.document_id}_metadata", "metadata", meta_doc.document_id,
            panel_snapshot, [], None,
        ))

    def _wait_for_metadata_hidden(self, page: object, open_factory: object, timeout_s: float = 2.0) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if metadata_panel_signature(open_factory) is None:
                return True
            page.wait_for_timeout(100)
        return False

    def _capture_metadata_panel(self, open_factory: object) -> tuple[list[dict[str, object]], str, str | None]:
        """Capture the full Metadata panel outerHTML and parse tag/value rows.

        Extracts and validates SeriesNumber / SeriesDescription / SeriesInstanceUID
        (when present). A raw SeriesInstanceUID is reduced to a non-reversible
        SHA-256 prefix for audit and never written into filenames/logs. The raw
        ``outerHTML`` returned here is sanitized by the caller before persistence.
        """
        try:
            target = open_factory()
            scope = target.locator("xpath=ancestor::html")
            candidates = _MARKER_REGION_CANDIDATES["Meta 信息工具"][1]
            outer_html = ""
            panel_text = ""
            for selector in candidates:
                matches = scope.locator(selector)
                for index in range(matches.count()):
                    candidate = matches.nth(index)
                    try:
                        if not candidate.is_visible():
                            continue
                        payload = candidate.evaluate(
                            "el => ({tag: el.tagName.toLowerCase(), text: (el.innerText || el.textContent || '').trim()})"
                        )
                        if not _metadata_candidate_allowed(selector, payload["text"], payload["tag"]):
                            continue
                        outer_html = candidate.evaluate("el => el.outerHTML")
                        panel_text = payload["text"]
                        break
                    except Exception:
                        continue
                if outer_html:
                    break
            rows = self._parse_metadata_rows(panel_text, outer_html)
            uid = self._find_uid(rows, outer_html)
            uid_hash = metadata_uid_sha256_prefix(uid) if uid else None
            return rows, outer_html, uid_hash
        except Exception:
            return [], "", None

    def _parse_metadata_rows(self, text: str, outer_html: str) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        seen = set()
        candidates = (text or "").splitlines() + re.split(r"<[^>]+>", outer_html or "")
        for line in candidates:
            line = re.sub(r"\s+", " ", line).strip()
            if not line or line in seen:
                continue
            seen.add(line)
            normalized = line.lower()
            known = any(
                label in normalized
                for label in ("series number", "series description", "seriesinstanceuid", "series instance uid", "seriesnumber")
            )
            if known:
                rows.append({"row": line})
        # Ensure the three audited identity fields are represented (if absent, the
        # transactional audit still records that they were not found).
        found = " ".join(str(row["row"]).lower() for row in rows)
        for label in ("series number", "series description", "seriesinstanceuid", "series instance uid"):
            if label not in found:
                rows.append({"row_text_not_found": label})
        return rows

    def _find_uid(self, rows: list[dict[str, object]], outer_html: str) -> str | None:
        for row in rows:
            text = str(row.get("row", ""))
            m = re.search(r"(?:series.?instance.?uid|x[0-9a-f]{8})[:：]?\s*([0-9.]+)", text, re.IGNORECASE)
            if m:
                return m.group(1)
        m = re.search(r"(?:seriesinstanceuid|x[0-9a-f]{8})[:：]?\s*([0-9.]+)", (outer_html or ""), re.IGNORECASE)
        return m.group(1) if m else None

    def finalize_series_branches(
        self,
        page: object,
        template: RecordingTemplate,
        series_action_id: str = "",
        close_action_id: str = "",
        config: dict[str, object] | None = None,
    ) -> list[CaptureBranchOutcome]:
        """Discover all series and serially capture each, restoring original state.

        Saves the initially-selected series, scrollTop and panel open/closed
        state, enumerates descriptors, then walks them one at a time (never in
        parallel) with per-descriptor ``try/finally`` failure isolation. After a
        bounded number of hub-unrecoverable failures one controlled reload is
        allowed; a second is fatal and marks the overall pass partial/failed.
        The original selection, scrollTop and panel state are restored at the end,
        and a ``series_capture_manifest.json`` is written with audited counts.
        """
        config = config if config is not None else _expansion_config_value()
        per_series_timeout_s = float(config.get("per_series_timeout_s") or 20) if config else 20.0
        total_timeout_s = float(config.get("total_series_timeout_s") or 900) if config else 900.0
        max_series = int(config.get("max_series") or 40) if config else 40

        self._ensure_series_cfg(page)
        container = self._series_cfg.get("item_container_selector")

        # P1#7: emit only safe discovery/phase events (branch id / ordinal /
        # counts / status / stage). Raw series_key / UID / patient text never
        # reach the event stream.
        _emit_series_event({"event": "series_discovery_started"})
        started: float = 0.0
        bootstrap_error: str | None = None
        try:
            pages = _live_pages_map(page)
            series_locator = _resolve_locator_recipe(template.series_action.locator, pages)
            if series_locator is None:
                raise HubUnrecoverableError("series list locator unresolved at discovery")
            root = _series_scope_root(series_locator, container)
            doc_id = "d_series_hub"
            initial_state = self._snapshot_hub_state(root, template, pages)
            # Started just before discovery/iteration so the total-time budget is
            # measured only over the exploration loop (a stable budget baseline).
            started = time.monotonic()

            try:
                descriptors, _members, evidence = discover_series_candidates(
                    root, doc_id, max_scroll_steps=max_series, max_duration_s=min(10.0, total_timeout_s),
                    item_selector=self._series_cfg.get("item_selector"),
                    identity_attrs=self._series_cfg.get("identity_attrs"),
                )
            except Exception:
                descriptors, evidence = [], SeriesCollectionEvidence("scroll_harvest", False, 0, 0, 0, False, "series_discovery_failed", 0)
            recorded_activation = (
                template.series_action.action_type
                if template.series_action.action_type in {"click", "dblclick"}
                else "click"
            )
            descriptors = [
                replace(descriptor, activation=descriptor.activation or recorded_activation)
                for descriptor in descriptors
            ]
            # P1#6: discovered is the discovery's absolute count; every descriptor
            # must receive a terminal status (captured/partial/failed/skipped).
            discovered = len(descriptors)
            _emit_series_event({
                "event": "series_discovered",
                "discovered": discovered,
                "reached_end": bool(evidence.reached_end),
                "warning": evidence.warning,
            })

            outcomes: list[CaptureBranchOutcome] = []
            consecutive_hub_failures = 0
            reloaded = False
            budget_exhausted = False
            emitted_terminal_ordinals: set[int] = set()
            for position, descriptor in enumerate(descriptors[:max_series]):
                if time.monotonic() - started >= total_timeout_s:
                    budget_exhausted = True
                    for extra in descriptors[position:]:
                        outcomes.append(CaptureBranchOutcome(
                            branch_id=_safe_series_key(extra), series_key=extra.series_key, label=extra.label,
                            ordinal=extra.ordinal, document_id=extra.document_id, source_member_id=extra.member_id,
                            activation=extra.activation or "click", capture_status="skipped_budget",
                            fail_stage="budget", error_type=None, warning="series_budget_exhausted",
                        ))
                    break
                if consecutive_hub_failures >= 3 and reloaded:
                    # Second hub-unrecoverable block: stop and mark overall partial.
                    for extra in descriptors[position:]:
                        outcomes.append(CaptureBranchOutcome(
                            branch_id=_safe_series_key(extra), series_key=extra.series_key, label=extra.label,
                            ordinal=extra.ordinal, document_id=extra.document_id, source_member_id=extra.member_id,
                            activation=extra.activation or "click", capture_status="failed",
                            fail_stage="hub_unrecoverable", error_type="hub_unrecoverable",
                            warning="series_hub_unrecoverable",
                        ))
                    break
                # P1#7: per-branch capture_started event (safe ordinal only).
                _emit_series_event({"event": "series_capture_started", "branch_id": _safe_series_key(descriptor), "ordinal": descriptor.ordinal})
                try:
                    outcome = self.capture_one_series(page, descriptor, template, pages, config)
                    consecutive_hub_failures = 0
                except HubUnrecoverableError:
                    consecutive_hub_failures += 1
                    if consecutive_hub_failures >= 3 and not reloaded:
                        try:
                            page.reload()
                            page.wait_for_load_state()
                        except Exception:
                            pass
                        reloaded = True
                        consecutive_hub_failures = 0
                        # P1#5: after the controlled reload, re-build pages/root/
                        # template locators — never reuse pre-reload locators.
                        pages = _live_pages_map(page)
                        series_locator = _resolve_locator_recipe(template.series_action.locator, pages)
                        if series_locator is not None:
                            root = _series_scope_root(series_locator, container)
                        try:
                            outcome = self.capture_one_series(page, descriptor, template, pages, config)
                            consecutive_hub_failures = 0
                        except Exception as error:
                            outcome = CaptureBranchOutcome(
                                branch_id=_safe_series_key(descriptor), series_key=descriptor.series_key,
                                label=descriptor.label, ordinal=descriptor.ordinal, document_id=descriptor.document_id,
                                source_member_id=descriptor.member_id, activation=descriptor.activation or "click",
                                capture_status="failed", fail_stage="transaction", error_type=type(error).__name__,
                                warning=None,
                            )
                    elif consecutive_hub_failures >= 3:
                        for extra in descriptors[position:]:
                            outcomes.append(CaptureBranchOutcome(
                                branch_id=_safe_series_key(extra), series_key=extra.series_key, label=extra.label,
                                ordinal=extra.ordinal, document_id=extra.document_id, source_member_id=extra.member_id,
                                activation=extra.activation or "click", capture_status="failed",
                                fail_stage="hub_unrecoverable", error_type="hub_unrecoverable",
                                warning="series_hub_unrecoverable",
                            ))
                        break
                    else:
                        outcome = CaptureBranchOutcome(
                            branch_id=_safe_series_key(descriptor), series_key=descriptor.series_key,
                            label=descriptor.label, ordinal=descriptor.ordinal, document_id=descriptor.document_id,
                            source_member_id=descriptor.member_id, activation=descriptor.activation or "click",
                            capture_status="failed", fail_stage="hub_unrecoverable", error_type="hub_unrecoverable",
                            warning=None,
                        )
                except Exception as error:
                    outcome = CaptureBranchOutcome(
                        branch_id=_safe_series_key(descriptor), series_key=descriptor.series_key,
                        label=descriptor.label, ordinal=descriptor.ordinal, document_id=descriptor.document_id,
                        source_member_id=descriptor.member_id, activation=descriptor.activation or "click",
                        capture_status="failed", fail_stage="transaction", error_type=type(error).__name__,
                        warning=None,
                    )
                outcomes.append(outcome)
                # P1#7: per-branch terminal event mapped cleanly onto SERIES_EVENT_NAMES.
                terminal_event = {
                    "captured": "series_capture_completed",
                    "partial": "series_capture_partial",
                    "failed": "series_capture_failed",
                }.get(outcome.capture_status)
                if terminal_event:
                    emitted_terminal_ordinals.add(outcome.ordinal)
                    _emit_series_event({
                        "event": terminal_event,
                        "branch_id": outcome.branch_id,
                        "ordinal": outcome.ordinal,
                        "error_type": outcome.error_type,
                    })

            # P1#6: discovered stays the discovery absolute count; conservation is
            # enforced across the four terminal buckets. Every discovered descriptor
            # (including any max_series / budget-capped / hub tail) gets a terminal
            # status so the denominator is never silently shrunk, and the full
            # closed set ``manifest_outcomes`` is the returned/ persisted page set
            # (P1#6 closure): tails enter the returned outcomes, receive a
            # safe persisted branch artefact (status + descriptor), surface in the
            # flow's ``series_branches`` via the loader, and get a conventional
            # terminal event (skipped strictly maps to ``series_capture_partial``).
            handled_ordinals = {o.ordinal for o in outcomes}
            manifest_outcomes = list(outcomes)
            for descriptor in descriptors:
                if descriptor.ordinal in handled_ordinals:
                    continue
                tail = CaptureBranchOutcome(
                    branch_id=_safe_series_key(descriptor), series_key=descriptor.series_key,
                    label=descriptor.label, ordinal=descriptor.ordinal, document_id=descriptor.document_id,
                    source_member_id=descriptor.member_id, activation=descriptor.activation or "click",
                    capture_status="skipped_budget", fail_stage="limit", error_type=None,
                    warning="series_limit_reached",
                )
                manifest_outcomes.append(tail)

            # Persist a safe, loadable branch artefact for every terminal that
            # never ran ``capture_one_series`` (max / budget / hub tails) so the
            # loader surfaces each in the flow's ``series_branches`` just like any
            # other branch (P1#6 closure).
            descriptor_by_ordinal = {d.ordinal: d for d in descriptors}
            for outcome in manifest_outcomes:
                if (self.series_branches_root / outcome.branch_id / "status.json").is_file():
                    continue
                descriptor = descriptor_by_ordinal.get(outcome.ordinal)
                if descriptor is None:
                    continue
                self._persist_unattempted_branch(descriptor, outcome)

            # P1#7: every terminal outcome that has not yet produced a series
            # event gets one now. Skipped statuses have no dedicated SERIES_EVENT
            # name, so they map to the agreed equivalent partial terminal event;
            # ordinal/branch-id/safe-stage only, never raw identity.
            for outcome in manifest_outcomes:
                if outcome.ordinal in emitted_terminal_ordinals:
                    continue
                terminal_event = {
                    "captured": "series_capture_completed",
                    "partial": "series_capture_partial",
                    "failed": "series_capture_failed",
                    "skipped_budget": "series_capture_partial",
                    "skipped_duplicate": "series_capture_partial",
                }.get(outcome.capture_status)
                if not terminal_event:
                    continue
                emitted_terminal_ordinals.add(outcome.ordinal)
                _emit_series_event({
                    "event": terminal_event,
                    "branch_id": outcome.branch_id,
                    "ordinal": outcome.ordinal,
                    "error_type": outcome.error_type,
                })

            captured_count = sum(1 for o in manifest_outcomes if o.capture_status == "captured")
            partial_count = sum(1 for o in manifest_outcomes if o.capture_status == "partial")
            failed_count = sum(1 for o in manifest_outcomes if o.capture_status == "failed")
            skipped_count = sum(
                1 for o in manifest_outcomes
                if o.capture_status in ("skipped_duplicate", "skipped_budget")
            )
            # P1#6: overall success requires a fully confirmed, fully-captured pass;
            # any partial/failed/skipped or unreached-end forces overall_ok=False.
            overall_ok = (
                bool(evidence.reached_end)
                and captured_count == discovered
                and partial_count == 0
                and failed_count == 0
                and skipped_count == 0
            )
            # §6.4: after all branches, restore the original series/scroll/panel.
            # A failed end-of-pass restoration is never swallowed: it forces
            # overall_ok=False and is audited as an expansion warning (P1#5).
            end_restore_ok = True
            try:
                end_restore_ok, _problem = self._restore_hub_state(page, root, template, pages, initial_state)
            except Exception:
                end_restore_ok = False
            if not end_restore_ok:
                overall_ok = False
            self._write_series_capture_manifest(
                manifest_outcomes, discovered, captured_count, partial_count, failed_count, skipped_count,
                evidence, overall_ok, started, budget_exhausted, reloaded=reloaded,
                restore_failed=not end_restore_ok,
            )
            _emit_series_event({"event": "series_expansion_completed", "overall_ok": overall_ok})
            return manifest_outcomes
        except Exception as error:
            # P1#7: exploration infrastructure failure is surfaced through the
            # series event channel as a failed/phase event, never swallowed.
            bootstrap_error = type(error).__name__
            _emit_series_event({"event": "series_expansion_completed", "overall_ok": False, "error_type": bootstrap_error})
            try:
                outcomes: list[CaptureBranchOutcome] = []
                self._write_series_capture_manifest(
                    outcomes, 0, 0, 0, 0, 0,
                    SeriesCollectionEvidence("scroll_harvest", False, 0, 0, 0, False,
                                             "series_discovery_failed" if not isinstance(error, HubUnrecoverableError) else "series_hub_unrecoverable", 0),
                    False, started, budget_exhausted=False,
                )
            except Exception:
                pass
            return []

    def _write_series_capture_manifest(
        self,
        outcomes: list[CaptureBranchOutcome],
        discovered: int,
        captured_count: int,
        partial_count: int,
        failed_count: int,
        skipped_count: int,
        evidence: SeriesCollectionEvidence,
        overall_ok: bool,
        started: float,
        budget_exhausted: bool = False,
        reloaded: bool = False,
        restore_failed: bool = False,
    ) -> None:
        """Persist ``series_capture_manifest.json`` with audited, non-sensitive counts."""
        expansion_warning = evidence.warning
        if budget_exhausted:
            expansion_warning = "series_budget_exhausted"
        elif reloaded:
            # One controlled reload/bootstrap was used to recover a
            # hub-unrecoverable run (see ``finalize_series_branches``). This is
            # audited so operators can see that recovery happened exactly once.
            expansion_warning = "series_reload_recovered_once"
        elif restore_failed:
            # The end-of-pass hub/panel/selection restoration did not fully
            # succeed; overall_ok is False and the run is audited (P1#5) rather
            # than silently continuing.
            expansion_warning = "hub_restore_failed"
        manifest = {
            "discovered_count": discovered,
            "captured_count": captured_count,
            "partial_count": partial_count,
            "failed_count": failed_count,
            "skipped_count": skipped_count,
            "reloaded": reloaded,
            "count_conserved": (captured_count + partial_count + failed_count + skipped_count) == discovered,
            "reached_end": bool(evidence.reached_end),
            "warning": expansion_warning,
            "overall_ok": overall_ok,
            "total_duration_ms": int((time.monotonic() - started) * 1000),
            "branches": [
                {
                    "branch_id": o.branch_id,
                    "series_key_sha256": hashlib.sha256(
                        f"{o.document_id}::{o.series_key}".encode("utf-8")
                    ).hexdigest(),
                    "ordinal": o.ordinal,
                    "capture_status": o.capture_status,
                    "fail_stage": o.fail_stage,
                    "error_type": o.error_type,
                    "warning": o.warning,
                    "metadata_captured": o.metadata_captured,
                }
                for o in outcomes
            ],
        }
        self.series_branches_root.mkdir(parents=True, exist_ok=True)
        (self.series_branches_root / "series_capture_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _persist_unattempted_branch(self, descriptor: SeriesDescriptor, outcome: CaptureBranchOutcome) -> None:
        """Persist a safe, loadable branch artefact for a tail / unattempted descriptor.

        (P1#6 closure) Budget/max/hub tails never call ``capture_one_series``, so
        they otherwise leave no ``status.json``/``descriptor.json`` and never
        surface in ``_load_series_branch_snapshots`` -> flow ``series_branches``.
        Writing the same safe artefact shape as a real branch (only the hash of
        the series identity in ``status.json``; ``descriptor.json`` stays within
        the restricted capture tree, matching real captured branches) lets the
        loader emit a first-class terminal (skipped/failed) ``SeriesBranch``.
        """
        branch_dir = self.series_branches_root / outcome.branch_id
        branch_dir.mkdir(parents=True, exist_ok=True)
        (branch_dir / "descriptor.json").write_text(
            json.dumps(asdict(descriptor), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        status: dict[str, object] = {
            "branch_id": outcome.branch_id,
            "series_key_sha256": hashlib.sha256(
                f"{descriptor.document_id}::{descriptor.series_key}".encode("utf-8")
            ).hexdigest(),
            "ordinal": descriptor.ordinal,
            "capture_status": outcome.capture_status,
            "fail_stage": outcome.fail_stage,
            "error_type": outcome.error_type,
            "activation": outcome.activation,
            "source_member_id": descriptor.member_id,
            "warning": outcome.warning,
            "metadata_captured": False,
        }
        (branch_dir / "status.json").write_text(
            json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _snapshot_hub_state(self, root: object, template: RecordingTemplate, pages: dict[str, object]) -> dict[str, object]:
        selected_series_key: str | None = None
        try:
            descriptors, _members, _ev = discover_series_candidates(
                root, "d_hub_state", max_scroll_steps=5, max_duration_s=2.0,
                item_selector=self._series_cfg.get("item_selector"),
                identity_attrs=self._series_cfg.get("identity_attrs"),
            )
            selected = next((d for d in descriptors if d.selected), None)
            selected_series_key = selected.series_key if selected else None
        except Exception:
            pass
        scroll_top = 0
        try:
            scroll_top = root.evaluate("el => el.scrollTop")
        except Exception:
            pass
        meta_open = self._metadata_scope_factory(template, pages)
        panel_open = False
        if meta_open is not None:
            panel_open = metadata_panel_signature(meta_open) is not None
        return {"selected_series_key": selected_series_key, "scroll_top": scroll_top, "panel_open": panel_open}

    @staticmethod
    def _row_is_selected(locator: object) -> bool:
        """True when a series row currently reports a selected/active state."""
        try:
            state = locator.evaluate(
                """element => ({
                    aria_selected: element.getAttribute('aria-selected'),
                    selected_attr: element.hasAttribute('selected'),
                    data_selected: element.getAttribute('data-selected'),
                    active_class: /(^|\\s)(active|current|selected)(\\s|$)/.test(element.className || ''),
                })"""
            )
        except Exception:
            return False
        if not state:
            return False
        return (
            str(state.get("aria_selected") or "").lower() == "true"
            or bool(state.get("selected_attr"))
            or str(state.get("data_selected") or "").lower() in ("true", "1", "selected")
            or bool(state.get("active_class"))
        )

    def _restore_hub_state(
        self,
        page: object,
        root: object,
        template: RecordingTemplate,
        pages: dict[str, object],
        initial: dict[str, object],
    ) -> tuple[bool, str | None]:
        """Restore and VERIFY the original selection / scrollTop / panel state.

        Returns ``(ok, problem)``: ``ok`` is False (with a short unlocalized
        ``problem`` reason) whenever any part of the restoration fails — the
        Metadata panel did not hide/open, the hub is inoperable, or the original
        selection/scrollTop were not restored. Failures are never swallowed
        (P1#5); the caller degrades the branch or records a warning.

        Restore the original series selection first (Playwright's click may
        auto-scroll to bring the row into view), then restore the exact scrollTop
        LAST so a selection-driven scroll cannot clobber it. The panel open/closed
        state is restored around the selection.
        """
        problems: list[str] = []
        if initial.get("panel_open"):
            if not self._open_metadata_if_needed(page, template, pages, want_open=True):
                problems.append("metadata_open_failed")
        else:
            if not self._open_metadata_if_needed(page, template, pages, want_open=False):
                problems.append("metadata_not_hidden")

        if initial.get("selected_series_key"):
            try:
                descriptors, _members, _ev = discover_series_candidates(
                    root, "d_hub_restore", max_scroll_steps=8, max_duration_s=3.0,
                    item_selector=self._series_cfg.get("item_selector"),
                    identity_attrs=self._series_cfg.get("identity_attrs"),
                )
                target = next((d for d in descriptors if d.series_key == initial["selected_series_key"]), None)
                if target is None:
                    problems.append("selection_lost")
                else:
                    row, _steps = _locate_series_row(
                        root, target, max_scroll_steps=12,
                        item_selector=self._series_cfg.get("item_selector"),
                    )
                    if row is None:
                        problems.append("selection_unlocatable")
                    else:
                        self._perform_activation(row, target.activation or "click")
                        if not self._row_is_selected(row):
                            problems.append("selection_not_restored")
            except Exception as error:
                problems.append(f"selection_restore_error:{type(error).__name__}")

        try:
            root.evaluate("(el, top) => el.scrollTop = top", int(initial.get("scroll_top", 0)))
            restored_top = root.evaluate("el => el.scrollTop")
            if abs(int(restored_top) - int(initial.get("scroll_top", 0))) > 2:
                problems.append("scroll_not_restored")
        except Exception as error:
            problems.append(f"scroll_restore_error:{type(error).__name__}")

        return (not problems), (";".join(problems) if problems else None)

    def _open_metadata_if_needed(self, page: object, template: RecordingTemplate, pages: dict[str, object], want_open: bool) -> bool:
        """Open/close the Metadata panel toward ``want_open`` and VERIFY the result.

        Closing now waits for the panel to actually hide (a close that does not
        take effect is reported as a failed restore rather than silently left
        open for the next branch). Returns ``True`` when the desired end state is
        reached (or no Metadata trigger is present); ``False`` on failure.
        """
        meta_open = self._metadata_scope_factory(template, pages)
        if meta_open is None:
            return True
        try:
            currently_open = metadata_panel_signature(meta_open) is not None
            if want_open and not currently_open:
                for action in template.metadata_open_actions:
                    step = self._meta_factory(action, pages)
                    opened = step() if step is not None else None
                    if opened is None:
                        return False
                    opened.click()
            elif not want_open and currently_open:
                close = self._meta_factory(template.metadata_close, pages)
                if close is not None:
                    candidate = close()
                    if candidate is not None:
                        candidate.click()
            if want_open:
                return wait_for_metadata_panel_state(page, meta_open, timeout_s=2.0, stable_s=0.2)
            # Closing: wait (bounded) for the panel to actually hide.
            return self._wait_for_metadata_hidden(page, meta_open, timeout_s=2.0)
        except Exception:
            return False



@dataclass(frozen=True)
class RecordingTemplate:
    """Static classification of the human-recorded single-series template.

    ``series_action`` is the recorded series-activation action (序列选择); its
    click/dblclick is inherited verbatim by the future explorer. ``metadata_open``
    and ``metadata_close`` are the recorded Meta 信息工具 actions. A valid
    expansion template requires series + open + close, with the close a
    distinct, later metadata action than the open.
    """

    series_action: ActionTarget | None
    metadata_open: ActionTarget | None
    metadata_close: ActionTarget | None
    metadata_open_sequence: tuple[ActionTarget, ...] = ()

    @property
    def metadata_open_actions(self) -> tuple[ActionTarget, ...]:
        """All recorded actions required to reveal the Metadata panel."""
        if self.metadata_open_sequence:
            return self.metadata_open_sequence
        return (self.metadata_open,) if self.metadata_open is not None else ()

    @property
    def complete(self) -> bool:
        return (
            self.series_action is not None
            and self.metadata_open is not None
            and self.metadata_close is not None
            and self.metadata_open != self.metadata_close
        )


def classify_recording_template(plan: ActionPlan) -> RecordingTemplate:
    """Identify the series activation and Metadata open/close recorded actions.

    Deterministic static proxy over the recorded action plan (no browser, so it
    runs in the template/preflight pass): the series action is the first
    ``序列选择`` marker action whose click/dblclick is inherited; Metadata
    actions are all ``Meta 信息工具`` actions in recording order, where the OPEN
    is the first and the CLOSE is the last (the canonical template records
    open-then-close). Phase 5 may refine open/close from live panel visibility;
    this byte-stable proxy is the Phase 4 contract the trigger depends on.
    """
    series: ActionTarget | None = None
    meta_actions: list[ActionTarget] = []
    for group in plan.marker_groups:
        normalized = normalize_label(group.marker_label)
        for action in group.actions:
            if normalized == normalize_label("序列选择") and series is None:
                series = action
            elif normalized == normalize_label("Meta 信息工具"):
                meta_actions.append(action)
    open_actions = tuple(meta_actions[:-1] if len(meta_actions) >= 2 else meta_actions)
    open_action = open_actions[0] if open_actions else None
    close_action = meta_actions[-1] if len(meta_actions) >= 2 else None
    return RecordingTemplate(series, open_action, close_action, open_actions)


_LIVE_SESSION: LiveCaptureSession | None = None
_INTERACTIVE_AUTH_DONE = False
_EXPANSION_CONFIG: dict[str, object] = {}


def _expansion_config_value() -> dict[str, object]:
    """Return the expansion run config, cached from the subprocess environment.

    ``run_live_capture`` serializes the expansion config into the
    ``REPLICA_EXPANSION_CONFIG`` environment variable of the instrumented
    replay subprocess; this is the only cross-process channel (mirroring how
    ``REPLICA_CAPTURE_OUTPUT`` reaches the live session). Tests that exercise
    only text instrumentation set the config on ``instrument_marked_actions``
    directly and never reach this path.
    """
    global _EXPANSION_CONFIG
    if not _EXPANSION_CONFIG:
        raw = os.environ.get("REPLICA_EXPANSION_CONFIG")
        if raw:
            try:
                _EXPANSION_CONFIG = json.loads(raw)
            except (ValueError, TypeError):
                _EXPANSION_CONFIG = {}
    return _EXPANSION_CONFIG


def _expansion_enabled(config: dict[str, object] | None) -> bool:
    return bool((config or {}).get("expand_all_series"))


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


def capture_hook_expand_series(
    page: object,
    series_locator_factory: object | None = None,
    series_action_id: str = "",
    close_action_id: str = "",
) -> None:
    """Runtime hook imported by instrumented scripts (independent expansion trigger).

    This is a *separate* hook from ``capture_hook_after``: it is injected by
    ``instrument_marked_actions`` into the Metadata-close action's success
    ``else:`` branch, immediately after that action's ``capture_hook_after``.
    It exists so the future all-series explorer runs after the panel is closed,
    while ``capture_hook_after`` keeps its single-after-snapshot duty. It passes
    only stable recipes (locator factory / action ids / page var) — no LLM
    decision happens here. When expansion is disabled or no session is wired,
    it is a safe no-op.
    """
    if not _expansion_enabled(_expansion_config_value()):
        return
    session = _LIVE_SESSION or _session_from_environment()
    if session is None:
        return
    try:
        session.expand_series(page, series_locator_factory, series_action_id, close_action_id)
    except Exception as error:
        # P1#7: a top-level exploration infrastructure failure is surfaced through
        # the series event channel using a SERIES_EVENT_NAMES name (never the
        # orphaned ``series_expansion_failed``). The Session itself emits the
        # per-branch events; this only catches failures that escape the explorer
        # (e.g. template/parse crashes) — never raw UID / patient text.
        _emit_series_event({
            "event": "series_expansion_completed",
            "overall_ok": False,
            "error_type": f"infra_{type(error).__name__}",
        })


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


def instrument_marked_actions(source: str, use_storage_state: bool = False, interactive_auth: bool = False, marker_annotations: object | None = None, expansion_config: dict[str, object] | None = None) -> str:
    """Insert capture hooks around marked Playwright action statements without executing source.

    When ``expansion_config`` enables ``expand_all_series``, the recording
    template must contain a series-activation action plus a Metadata open and a
    Metadata close; an independent ``capture_hook_expand_series`` call is then
    injected into the Metadata-close action's success ``else:`` branch, directly
    after that action's own ``capture_hook_after``. When disabled (default), no
    expansion hook is produced and prior recording behavior is unchanged.
    """
    plan = parse_action_plan(source, _coerce_marker_annotations(marker_annotations))
    expansion = dict(expansion_config or {})
    expand = _expansion_enabled(expansion)
    template = classify_recording_template(plan)
    if expand and not template.complete:
        raise ValueError(
            "expansion requires a series-select action plus a Metadata open and "
            "a Metadata close in the recording template"
        )
    close_action_id = template.metadata_close.action_id if (expand and template.metadata_close) else None
    series_action = template.series_action if expand else None
    series_src = series_action.locator.source_expression if (series_action is not None and series_action.locator) else ""
    series_action_id = series_action.action_id if series_action is not None else ""
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
    hook_import = "capture_hook_after, capture_hook_before, capture_hook_expand_series, capture_hook_failed" if expand else "capture_hook_after, capture_hook_before, capture_hook_failed"
    output = ["import os" if use_storage_state else "", f"from batch_capture_replicate import {hook_import}"]
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
            if expand and action_id == close_action_id:
                series_factory = f"lambda: {series_src}" if series_src else "None"
                output.append(f"{indent}    capture_hook_expand_series({page_var}, {series_factory}, {series_action_id!r}, {close_action_id!r})")
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
    marker_annotations: object | None = None,
    expansion_config: dict[str, object] | None = None,
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
        instrument_marked_actions(script_path.read_text(encoding="utf-8"), storage_state is not None, interactive_auth, marker_annotations, expansion_config),
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
    if _expansion_enabled(expansion_config):
        environment["REPLICA_EXPANSION_CONFIG"] = json.dumps(expansion_config, ensure_ascii=False)
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
        # The scroll-stitched series-list background is stored phase-relative; rebase
        # it against capture_root exactly like the screenshot so the builder can
        # resolve it and switch to the full panel-scroll replica.
        if item.get("series_list_full_asset_relpath"):
            item["series_list_full_asset_relpath"] = str(
                (phase_root / item["series_list_full_asset_relpath"]).relative_to(capture_root)
            ).replace("\\", "/")
        # 布局变体背景同样存于 phase 级（a_001_002/after/assets/by-hash/...）；rebase
        # 到 capture_root，否则 build 侧 asset 复制在 capture_root/assets 下找不到。
        if item.get("layout_variants"):
            item["layout_variants"] = {
                variant: str((phase_root / relpath).relative_to(capture_root)).replace("\\", "/")
                for variant, relpath in item["layout_variants"].items()
            }
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


def _load_branch_topology(capture_root: Path, branch_dir: Path, subdir: str):
    """Load a branch viewer/metadata topology, rebasing asset relpaths to capture_root.

    Reuses ``ReplicaDocument.from_dict`` (the same decoder the snapshots use) so
    there is exactly one topology decoder in this module — never a second one.
    """
    payload = json.loads((branch_dir / subdir / "topology.json").read_text(encoding="utf-8"))
    pages = [ReplicaPage(**item) for item in payload.get("pages", [])]
    documents = []
    for item in payload.get("documents", []):
        rel = item.get("screenshot_asset_relpath")
        if rel:
            try:
                item["screenshot_asset_relpath"] = str((branch_dir / subdir / rel).resolve().relative_to(capture_root.resolve())).replace("\\", "/")
            except ValueError:
                pass
        tall_rel = item.get("series_list_full_asset_relpath")
        if tall_rel:
            try:
                item["series_list_full_asset_relpath"] = str(
                    (branch_dir / subdir / tall_rel).resolve().relative_to(capture_root.resolve())
                ).replace("\\", "/")
            except ValueError:
                pass
        layout_variants = item.get("layout_variants")
        if layout_variants:
            rebased: dict[str, str] = {}
            for variant, relpath in layout_variants.items():
                try:
                    rebased[variant] = str(
                        (branch_dir / subdir / relpath).resolve().relative_to(capture_root.resolve())
                    ).replace("\\", "/")
                except ValueError:
                    continue
            item["layout_variants"] = rebased
        documents.append(ReplicaDocument.from_dict(item))
    return pages, documents


def _metadata_panel_text(outer_html: str) -> str:
    """Extract concise visible text from a sanitized Metadata panel outerHTML.

    The builder's ``_is_metadata_panel`` requires a non-empty ``root.text`` in
    addition to a matching id/class/role; the panel's visible text is derived
    from the captured HTML so the panel is recognized and rendered complete.
    """
    text = re.sub(r"<[^>]+>", " ", outer_html or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:2000]


def _metadata_panel_snapshot(meta_doc: ReplicaDocument, outer_html: str) -> DomNodeSnapshot:
    panel_attributes = {"id": "metadata-panel", "class": "tagsBox", "role": "dialog"}
    rect = Rect(0, 0, float(meta_doc.viewport.get("width") or 0), float(meta_doc.viewport.get("height") or 0), "page_viewport_css")
    return DomNodeSnapshot("div", _metadata_panel_text(outer_html), panel_attributes, rect, outer_html, {})


def _attach_metadata_region_to_doc(meta_doc: ReplicaDocument, outer_html: str) -> None:
    """Attach a ``region_type="metadata"`` InteractionRegion to a document (P0#1).

    The region root carries the sanitized full Metadata panel ``outerHTML`` (and a
    non-empty extracted text) so the builder's ``_is_metadata_panel`` renders it
    as a complete scrollable ``.replica-metadata`` panel.
    """
    panel_snapshot = _metadata_panel_snapshot(meta_doc, outer_html)
    meta_doc.regions.append(InteractionRegion(
        f"{meta_doc.document_id}_metadata", "metadata", meta_doc.document_id,
        panel_snapshot, [], None,
    ))


def _load_series_branch_snapshots(
    capture_root: Path,
) -> tuple[list[SeriesBranchCapture], list[str], dict[str, object] | None]:
    """Read every Phase-5 branch snapshot under ``capture/series_branches/*/``.

    Returns ``(snapshots, warnings, expansion)`` where ``expansion`` is the
    aggregate manifest evidence (or ``None`` when no branches were captured).
    Only decode/viewer snapshots are surfaced; each ``SeriesBranchCapture`` carries
    the viewer/metadata state evidence needed to build a v2 flow. No raw UID or
    patient text is emitted here.
    """
    root = Path(capture_root) / "series_branches"
    snapshots: list[SeriesBranchCapture] = []
    warnings: list[str] = []
    if not root.is_dir():
        return snapshots, warnings, None
    manifest_path = root / "series_capture_manifest.json"
    expansion = None
    if manifest_path.is_file():
        expansion = json.loads(manifest_path.read_text(encoding="utf-8"))
    for branch_dir in sorted(root.iterdir()):
        if not branch_dir.is_dir():
            continue
        status_path = branch_dir / "status.json"
        if not status_path.is_file():
            warnings.append(f"series_branch_missing_status:{branch_dir.name}")
            continue
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
            descriptor = SeriesDescriptor(**json.loads((branch_dir / "descriptor.json").read_text(encoding="utf-8")))
        except Exception:
            warnings.append(f"series_branch_unreadable:{branch_dir.name}")
            continue
        capture_status = str(status.get("capture_status"))
        viewer_pages: list[ReplicaPage] = []
        viewer_documents: list[ReplicaDocument] = []
        metadata_pages: list[ReplicaPage] = []
        metadata_documents: list[ReplicaDocument] = []
        try:
            viewer_pages, viewer_documents = _load_branch_topology(capture_root, branch_dir, "viewer")
        except Exception:
            viewer_pages, viewer_documents = [], []
        try:
            metadata_pages, metadata_documents = _load_branch_topology(capture_root, branch_dir, "metadata")
        except Exception:
            metadata_pages, metadata_documents = [], []
        metadata_rows: list[dict[str, object]] = []
        metadata_outer_html: str = ""
        # The writer persists ``metadata/metadata_rows.json`` (see
        # capture_one_series -> _capture_metadata_transaction); this is the only
        # authoritative location. Load it so captured rows/outerHTML restore the
        # Metadata region even when an older/partial artifact's topology did not
        # already embed a ``metadata`` region (P0#1 loader fallback).
        rows_payload_path = branch_dir / "metadata" / "metadata_rows.json"
        if rows_payload_path.is_file():
            try:
                payload = json.loads(rows_payload_path.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    metadata_rows = payload.get("rows", [])
                    metadata_outer_html = str(payload.get("outer_html") or "")
            except Exception:
                metadata_rows = []
        # P0#1: assemble the captured (sanitized) Metadata outerHTML into the
        # metadata document as a real ``metadata`` InteractionRegion so the offline
        # replica renders a complete, scrollable Metadata panel. Only attach when
        # the loaded topology does not already carry a ``metadata`` region so a
        # fallback never duplicates a region the writer already embedded.
        if (
            metadata_outer_html
            and metadata_documents
            and not any(
                region.region_type == "metadata"
                for doc in metadata_documents
                for region in doc.regions
            )
        ):
            try:
                _attach_metadata_region_to_doc(metadata_documents[0], metadata_outer_html)
            except Exception:
                pass
        # P0#1: load the captured Metadata open-target DOM (if any) so the branch
        # Viewer state can render a real clickable Metadata trigger button.
        meta_open_dom: DomNodeSnapshot | None = None
        meta_open_target_path = branch_dir / "meta_open_target.json"
        if meta_open_target_path.is_file():
            try:
                meta_open_payload = json.loads(meta_open_target_path.read_text(encoding="utf-8"))
                meta_open_payload["rect"] = Rect(**meta_open_payload["rect"])
                meta_open_dom = DomNodeSnapshot(**meta_open_payload)
            except Exception:
                meta_open_dom = None
        if not viewer_documents and capture_status in ("captured", "partial"):
            warnings.append(f"series_branch_missing_viewer:{branch_dir.name}")
            continue
        snapshots.append(SeriesBranchCapture(
            branch_id=str(status.get("branch_id") or branch_dir.name),
            series_key=descriptor.series_key,
            label=descriptor.label,
            ordinal=descriptor.ordinal,
            document_id=descriptor.document_id,
            source_member_id=str(status.get("source_member_id") or descriptor.member_id),
            activation=descriptor.activation or "click",
            capture_status=capture_status,
            warning=status.get("warning"),
            viewer_pages=viewer_pages,
            viewer_documents=viewer_documents,
            metadata_pages=metadata_pages,
            metadata_documents=metadata_documents,
            metadata_rows=metadata_rows,
            meta_open_dom=meta_open_dom,
        ))
        if status.get("warning"):
            warnings.append(f"series_branch_warning:{branch_dir.name}::{status['warning']}")
        if status.get("fail_stage"):
            warnings.append(f"series_branch_failed_stage:{branch_dir.name}::{status['fail_stage']}")
    return snapshots, warnings, expansion


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


def _action_marker_id(state: ReplicaState, action_id: str) -> str | None:
    return next(
        (
            target.marker_id
            for document in state.documents
            for target in document.targets
            if target.action_id == action_id
        ),
        None,
    )


def _skip_popup_shell_state(states: list[ReplicaState]) -> None:
    """Point popup entry past a genuinely incomplete loading shell."""
    if len(states) < 3:
        return
    entry, shell, ready = states[:3]
    transition = next((item for item in entry.transitions if item.mode == "popup"), None)
    if transition is None or transition.to_state_id != shell.state_id:
        return
    shell_frames = [doc for doc in shell.documents if doc.parent_document_id is not None]
    ready_frames = [doc for doc in ready.documents if doc.parent_document_id is not None]
    if len(shell_frames) != 1 or len(ready_frames) != 1:
        return
    shell_bytes = shell_frames[0].screenshot_size_bytes
    ready_bytes = ready_frames[0].screenshot_size_bytes
    if shell_bytes < 50_000 and ready_bytes >= max(50_000, shell_bytes * 4):
        transition.to_state_id = ready.state_id


def _refresh_delayed_state(
    state: ReplicaState,
    pages: list[ReplicaPage],
    documents: list[ReplicaDocument],
) -> None:
    """Use the next action's before-snapshot when a popup finished loading late."""
    current_frames = [doc for doc in state.documents if doc.parent_document_id is not None]
    ready_frames = [doc for doc in documents if doc.parent_document_id is not None]
    if len(current_frames) != 1 or len(ready_frames) != 1:
        return
    current_bytes = current_frames[0].screenshot_size_bytes
    ready_bytes = ready_frames[0].screenshot_size_bytes
    if current_bytes >= 50_000 or ready_bytes < max(50_000, current_bytes * 4):
        return
    state.pages = pages
    state.documents = documents


def build_flow_from_snapshots(script_path: str | Path, capture_root: str | Path, marker_annotations: object | None = None) -> ReplicaFlow:
    """Build a sequential ReplicaFlow from marked-action snapshot pairs.

    ``marker_annotations`` is the list of validated GUI marker records (each
    ``{"marker_id", "line", "label"}``); when supplied it is threaded into
    ``parse_action_plan`` (the Task 2 annotation extension) so group/action
    ``marker_id`` carries the stable GUI UUID instead of the regenerated
    ``m_{index}`` id. For backward compatibility a full annotations payload dict
    (containing a ``markers`` list) is also accepted and its list is extracted.
    Without annotations behavior is unchanged.
    """
    script_path = Path(script_path)
    capture_root = Path(capture_root)
    plan = parse_action_plan(script_path.read_text(encoding="utf-8"), _coerce_marker_annotations(marker_annotations))
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
    # 步骤 1：显式追踪「有快照对但 target.json 缺失」的 action —— target 本可捕获却
    # 缺失（多匹配/无匹配/捕获异常被降级），离线转场失去可点载体，必须在
    # pipeline_report 里显式可见（build_flow_from_snapshots 把 warnings 序列化进 manifest）。
    missing_target_actions: list[str] = []
    for index, action in enumerate(actions, start=1):
        before_pages, before_documents = _load_snapshot_state(capture_root, action.action_id, "before")
        _refresh_delayed_state(states[-1], before_pages, before_documents)
        target = _load_target_snapshot(capture_root, action.action_id)
        # target 本可捕获（有完整快照对）却缺失 target.json → 该 action 的离线转场无载体
        if target is None and _has_snapshot_pair(capture_root, action.action_id):
            missing_target_actions.append(action.action_id)
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
    _skip_popup_shell_state(states)
    warnings = [f"action_capture_failed:{action_id}" for action_id in skipped_actions]
    warnings.extend(f"missing_target_evidence:{action_id}" for action_id in missing_target_actions)

    # Phase 6: merge any Phase-5 series-branch snapshots into the flow as
    # ordinary ReplicaState / ReplicaDocument (schema v2). Each successful /
    # partial branch gets a unique viewer state; each metadata-successful branch
    # gets a unique metadata state whose close transition returns explicitly to
    # the same branch's viewer state.
    series_branches, expansion_evidence = _build_branches_into_flow(states, capture_root, plan, warnings)

    return ReplicaFlow(
        2,
        script_path.stem,
        script_path.name,
        sha256_file(script_path),
        datetime.now(timezone.utc).isoformat(),
        viewport,
        plan.bootstrap,
        plan.popup_expectations,
        CaptureTimingProfile(),
        "s_000",
        states,
        warnings,
        series_branches=series_branches,
        series_expansion=expansion_evidence,
    )


def _build_branches_into_flow(
    states: list[ReplicaState],
    capture_root: Path,
    plan: ActionPlan,
    warnings: list[str],
) -> tuple[list[SeriesBranch], SeriesExpansionEvidence | None]:
    """Merge captured series branches into ``states`` (in place) and return branches/evidence.

    Uses ``_load_series_branch_snapshots`` (which reuses ``ReplicaDocument.from_dict``
    — the module's one topology decoder). Synthetic action IDs follow the stable
    ``series:{branch_id}:activate/meta_open/meta_close`` scheme.
    """
    template = classify_recording_template(plan)
    branch_selector = template.series_action.locator if template.series_action is not None else None
    meta_open_recipe = template.metadata_open.locator if template.metadata_open is not None else None

    snapshots, branch_warnings, expansion_payload = _load_series_branch_snapshots(Path(capture_root))
    warnings.extend(branch_warnings)
    if not snapshots:
        return [], None

    series_branches: list[SeriesBranch] = []
    next_ordinal = len(states)
    active_page_var = branch_selector.page_var if branch_selector is not None else "page"

    for snapshot in snapshots:
        branch_id = snapshot.branch_id
        viewer_state_id: str | None = None
        metadata_state_id: str | None = None
        return_state_id: str | None = None
        selector = branch_selector

        # Unique viewer state for captured/partial branches that have a valid
        # viewer snapshot.
        if snapshot.capture_status in ("captured", "partial") and snapshot.viewer_documents:
            viewer_state_id = f"bviewer_{branch_id}"
            viewer_ordinal = next_ordinal
            next_ordinal += 1
            # P0#2: atomically remap pages + documents (ids, parent ids, page
            # ids, entry_document_id and in-document region/target references) so
            # the builder can resolve each branch entry document via
            # ``target_page.entry_document_id``.
            viewer_pages, viewer_documents = _branch_topology(snapshot.viewer_pages, snapshot.viewer_documents, branch_id)
            evidence = StateEvidence(False, False, False, False, 0, 0, 0, 0, "series_branch_viewer")
            viewer_transitions: list[ReplicaTransition] = []
            # Metadata trigger lives on the viewer state (a real clickable DOM
            # target, not a dom=None placeholder).
            if snapshot.capture_status in ("captured", "partial") and snapshot.metadata_documents:
                # (closure #3) Mount the trigger on exactly ONE viewer document —
                # the one the meta-open recipe's frame chain resolves to — so a
                # nested-frame viewer never duplicates / misplaces it.
                # ``document_id`` is taken from that owning document; when the
                # frame chain is ambiguous or unresolvable we do NOT guess
                # ``documents[-1]`` — the trigger (and its metadata state) is
                # simply omitted, which is strictly safer than a wrong mount.
                owning_doc_id = _document_id_for_recipe(meta_open_recipe, viewer_pages, viewer_documents)
                if owning_doc_id is None:
                    page_docs = [doc for doc in viewer_documents if doc.page_var == active_page_var]
                    if len(page_docs) == 1:
                        owning_doc_id = page_docs[0].document_id
                if owning_doc_id:
                    metadata_state_id = f"bmeta_{branch_id}"
                    meta_action = _synthetic_meta_open_target(
                        branch_id, meta_open_recipe, owning_doc_id, dom=snapshot.meta_open_dom
                    )
                    for doc in viewer_documents:
                        if doc.document_id == owning_doc_id:
                            doc.targets.append(meta_action)
                    viewer_transitions.append(ReplicaTransition(
                        f"series:{branch_id}:meta_open", f"series:{branch_id}:meta_open",
                        viewer_state_id, metadata_state_id, active_page_var, active_page_var, "same_page",
                    ))
            states.append(ReplicaState(
                viewer_state_id, viewer_ordinal, "", active_page_var,
                viewer_pages, viewer_documents, viewer_transitions, evidence,
            ))
            return_state_id = viewer_state_id

            # Unique metadata state for metadata-successful branches; its close
            # returns explicitly to the same branch's viewer state.
            if metadata_state_id is not None:
                meta_ordinal = next_ordinal
                next_ordinal += 1
                metadata_transitions = [ReplicaTransition(
                    f"series:{branch_id}:meta_close", f"series:{branch_id}:meta_close",
                    metadata_state_id, viewer_state_id, active_page_var, active_page_var, "same_page",
                )]
                meta_pages, meta_documents = _branch_topology(snapshot.metadata_pages, snapshot.metadata_documents, branch_id)
                states.append(ReplicaState(
                    metadata_state_id, meta_ordinal, "", active_page_var,
                    meta_pages, meta_documents,
                    metadata_transitions,
                    StateEvidence(False, False, False, False, 0, 0, 0, 0, "series_branch_metadata"),
                ))

        branch_warning = snapshot.warning
        if snapshot.capture_status == "failed" and not branch_warning:
            branch_warning = snapshot.warning or "series_capture_failed"
        series_branches.append(SeriesBranch(
            branch_id=branch_id,
            series_key=snapshot.series_key,
            label=snapshot.label,
            ordinal=snapshot.ordinal,
            document_id=snapshot.document_id,
            source_member_id=snapshot.source_member_id,
            selector=selector,
            activation=snapshot.activation,
            viewer_state_id=viewer_state_id,
            metadata_state_id=metadata_state_id,
            return_state_id=return_state_id,
            capture_status=snapshot.capture_status,
            warning=branch_warning,
        ))

    expansion = _expansion_evidence(snapshots, expansion_payload)
    return series_branches, expansion


def _branch_topology(
    pages: list[ReplicaPage],
    documents: list[ReplicaDocument],
    branch_id: str,
) -> tuple[list[ReplicaPage], list[ReplicaDocument]]:
    """(P0#2) Atomically remap a branch's page/document graph to branch-unique IDs.

    Each branch snapshot lives in its own asset namespace, yet all flow states
    share one document-id pool and one asset directory (relative to the capture
    root). This single atomic transform rebases:

    - ``ReplicaDocument.document_id`` and ``parent_document_id``
    - ``ReplicaPage.page_id`` and ``ReplicaPage.entry_document_id``
    - in-document ``region.document_id`` and ``target.document_id`` references

    so ``target_page.entry_document_id`` reliably resolves to a remapped
    document (no dangling/StopIteration in the builder's route resolution).
    """
    prefix = f"{branch_id}__"
    remap_doc: dict[str, str] = {}
    for document in documents:
        remap_doc.setdefault(document.document_id, f"{prefix}{document.document_id}")
    remap_page: dict[str, str] = {page.page_id: f"{prefix}{page.page_id}" for page in pages}

    new_pages = [
        replace(
            page,
            page_id=remap_page.get(page.page_id, page.page_id),
            # (closure suggestion #1) ``opener_page_id`` references another page
            # in the *same* branch snapshot; remap it through the page map so a
            # multi-page / popup branch never keeps a dangling pre-remap opener
            # id after the graph is rebased.
            opener_page_id=(
                remap_page.get(page.opener_page_id, page.opener_page_id)
                if page.opener_page_id is not None else None
            ),
            entry_document_id=remap_doc.get(page.entry_document_id, page.entry_document_id),
        )
        for page in pages
    ]
    new_documents = []
    for document in documents:
        new_id = remap_doc[document.document_id]
        parent = document.parent_document_id
        if parent is not None and parent in remap_doc:
            parent = remap_doc[parent]
        new_regions = [replace(region, document_id=remap_doc.get(region.document_id, region.document_id)) for region in document.regions]
        new_targets = [replace(target, document_id=remap_doc.get(target.document_id, target.document_id)) for target in document.targets]
        new_documents.append(replace(
            document,
            document_id=new_id,
            parent_document_id=parent,
            page_id=remap_page.get(document.page_id, document.page_id),
            regions=new_regions,
            targets=new_targets,
        ))
    return new_pages, new_documents


def _frame_hop_matches_document(hop: FrameHop, document: ReplicaDocument) -> bool:
    """True when a recipe ``frame_chain`` hop points at ``document``.

    A hop carries both the selector (``#id`` / ``iframe[name=...]``) and the
    frame's own id/name captured at record time; a document records the same
    ``frame_selector``/``frame_id``/``frame_name`` that ``capture_page_topology``
    captured. Any one of them matching is sufficient, since a resolved hop must
    already be the correct parent in the chain.
    """
    if hop.frame_id and hop.frame_id == document.frame_id:
        return True
    if hop.frame_name and hop.frame_name == document.frame_name:
        return True
    if hop.selector and document.frame_selector:
        if hop.selector == document.frame_selector:
            return True
        # ``iframe#id`` / ``iframe[name=foo]`` (hop) vs ``#id`` (document) — the
        # recorded ``.locator(x).content_frame`` chain carries a wrapper selector
        # while the captured document records its bare frame selector.
        raw_id = re.search(r"#([A-Za-z_][\w-]*)", hop.selector)
        doc_id = re.search(r"#([A-Za-z_][\w-]*)", document.frame_selector)
        if raw_id and doc_id and raw_id.group(1) == doc_id.group(1):
            return True
        # ``iframe[name=foo]`` (document) vs ``foo`` (hop.name) — compare the
        # bare name when the selectors differ only in wrapper syntax.
        if "name=" in hop.selector and "name=" in document.frame_selector:
            try:
                if re.search(r"name=['\"]([^'\"]+)", hop.selector).group(1) == re.search(r"name=['\"]([^'\"]+)", document.frame_selector).group(1):
                    return True
            except (AttributeError, IndexError):
                pass
        if "=" not in hop.selector and "=" not in document.frame_selector and hop.selector == document.frame_selector.lstrip("#").strip():
            return True
    return False


def _document_id_for_recipe(
    recipe: LocatorRecipe | None,
    pages: list[ReplicaPage],
    documents: list[ReplicaDocument],
) -> str | None:
    """Resolve the owning document id for a recorded locator recipe (closure #3).

    Starts at ``recipe.page_var``'s entry document, then walks each ``frame_chain``
    hop down the captured document hierarchy (matching ``frame_selector`` /
    ``frame_id`` / ``frame_name``). Every hop must resolve to exactly one direct
    child document; an ambiguous or unresolvable chain returns ``None`` rather
    than guessing. This is the single owning-document rule used to mount the
    meta-open target so nested-frame viewers never duplicate/misplace the trigger.
    """
    if recipe is None:
        return None
    page = next((page for page in pages if page.page_var == recipe.page_var), None)
    if page is None:
        return None
    current = page.entry_document_id
    for hop in recipe.frame_chain or []:
        candidates = [
            doc.document_id
            for doc in documents
            if doc.parent_document_id == current and _frame_hop_matches_document(hop, doc)
        ]
        if len(candidates) != 1:
            return None
        current = candidates[0]
    return current


def _synthetic_meta_open_target(
    branch_id: str,
    meta_open_recipe: LocatorRecipe | None,
    document_id: str,
    dom: DomNodeSnapshot | None = None,
) -> ActionTarget:
    return ActionTarget(
        action_id=f"series:{branch_id}:meta_open",
        marker_id="",
        action_type="click",
        action_source_kind="locator",
        action_args={},
        locator=meta_open_recipe,
        dom=dom,
        selector_closure=None,
        point=None,
        key=None,
        replay_policy="execute",
        skip_reason=None,
        document_id=document_id,
        transition_id=f"series:{branch_id}:meta_open",
    )


def _expansion_evidence(snapshots: list[SeriesBranchCapture], expansion_payload: dict[str, object] | None) -> SeriesExpansionEvidence:
    """(P1#6) Aggregate flow-level expansion evidence using the discovery count.

    ``discovered`` is frozen to the discovery's absolute count (from the capture
    manifest when present) so skipped_budget / skipped_duplicate branches can
    never be silently dropped from the denominator. The v2 schema has no
    ``skipped_count`` field, so skipped entries are conservatively mapped to
    ``partial`` (retaining the flow warning) — never discarded.
    """
    manifest = expansion_payload if isinstance(expansion_payload, dict) else {}
    has_counts = any(key in manifest for key in ("discovered_count", "captured_count"))
    if has_counts:
        captured = int(manifest.get("captured_count") or 0)
        partial = int(manifest.get("partial_count") or 0) + int(manifest.get("skipped_count") or 0)
        failed = int(manifest.get("failed_count") or 0)
        discovered = int(manifest.get("discovered_count") or (captured + partial + failed))
    else:
        # No manifest (e.g. unit-level fixtures): derive from the branch dirs,
        # mapping any skipped terminal status to partial.
        captured = sum(1 for s in snapshots if s.capture_status == "captured")
        partial = sum(1 for s in snapshots if s.capture_status in ("partial", "skipped_budget", "skipped_duplicate"))
        failed = sum(1 for s in snapshots if s.capture_status == "failed")
        discovered = captured + partial + failed
    reached_end = bool(manifest.get("reached_end")) if has_counts else bool((expansion_payload or {}).get("reached_end")) if expansion_payload else False
    warning = manifest.get("warning") if has_counts else ((expansion_payload or {}).get("warning") if expansion_payload else None)
    total_ms = int(manifest.get("total_duration_ms") or 0) if has_counts else (int((expansion_payload or {}).get("total_duration_ms") or 0) if expansion_payload else 0)
    return SeriesExpansionEvidence(
        discovered_count=discovered,
        captured_count=captured,
        partial_count=partial,
        failed_count=failed,
        reached_end=reached_end,
        total_duration_ms=total_ms,
        warning=warning,
    )


def _capture_to_manifest_core(
    script_path: str | Path,
    capture_root: str | Path,
    marker_annotations: object | None,
    notify: Callable[[dict[str, str]], None],
    storage_state: str | Path | None,
    interactive_auth: bool,
    capture_timeout_s: int,
    expansion_config: dict[str, object] | None = None,
) -> Path:
    """Shared capture-only body: run one instrumented replay and persist its manifest.

    This is the single capture implementation. Both the public ``capture_to_manifest``
    (annotation-backed) and the backward-compatible no-annotations branch of
    ``capture_and_build`` route through here so the capture path is not duplicated.
    """
    notify({"event": "capture_started", "stage": "live_capture"})
    capture_root = Path(capture_root)
    capture_root.mkdir(parents=True, exist_ok=True)
    result = run_live_capture(
        script_path,
        capture_root,
        timeout_s=capture_timeout_s,
        storage_state=storage_state,
        interactive_auth=interactive_auth,
        emit=notify,
        marker_annotations=marker_annotations,
        expansion_config=expansion_config,
    )
    if result.returncode:
        notify({"event": "failed", "stage": "capture"})
        raise RuntimeError(
            f"instrumented replay failed with exit {result.returncode}: "
            f"{result.stderr[-1000:]}"
        )
    notify({"event": "capture_finished", "stage": "live_capture"})
    flow = build_flow_from_snapshots(script_path, capture_root, marker_annotations=marker_annotations)
    flow.timing_profile.capture_timeout_s = capture_timeout_s
    # Persist a byte-identical copy of the executed source alongside the
    # manifest so ``flow.source_script_relpath`` (the script's basename)
    # resolves inside the capture tree and ``validate_manifest`` can hash-verify
    # provenance without warning ``source_script_missing``.
    source_name = Path(script_path).name
    (capture_root / source_name).write_bytes(Path(script_path).read_bytes())
    manifest_path = Path(capture_root) / "manifest.json"
    write_manifest(manifest_path, flow)
    return manifest_path


def capture_to_manifest(
    script_path: str | Path,
    annotations_path: str | Path,
    capture_root: str | Path,
    emit: Callable[[dict[str, str]], None] | None = None,
    storage_state: str | Path | None = None,
    interactive_auth: bool = False,
    capture_timeout_s: int = 900,
    expansion_config: dict[str, object] | None = None,
) -> Path:
    """Public capture-only boundary: validate annotations, replay, and persist a manifest.

    Does not build any replica. GUI UUIDs from the annotations are threaded through
    the instrumented replay and the resulting manifest so ``ActionTarget.marker_id``
    carries the stable GUI UUID.
    """
    notify = emit or (lambda event: None)
    annotation_payload = validate_annotations(script_path, annotations_path)
    marker_annotations = annotation_payload["markers"]
    return _capture_to_manifest_core(
        script_path, capture_root, marker_annotations, notify,
        storage_state, interactive_auth, capture_timeout_s,
        expansion_config=expansion_config,
    )


def _build_emitter(notify: Callable[[dict[str, str]], None]) -> Callable[[dict[str, str]], None]:
    """Adapt ``build_from_manifest`` progress events to the capture-and-build contract.

    Translates the build stage terminal ``completed`` into ``build_finished`` while
    preserving the ``build_started`` boundary. This keeps ``capture_and_build``'s
    event stream (capture_started/capture_finished/build_started/build_finished)
    compatible with its established callers.
    """

    def wrapped(event: dict[str, str]) -> None:
        name = event.get("event")
        if name == "completed":
            notify({"event": "build_finished", "stage": "replica_build", "entrypoint": event.get("entrypoint")})
        elif name == "build_started":
            notify({"event": "build_started", "stage": "replica_build"})
        else:
            notify(event)

    return wrapped


def capture_and_build(
    script_path: str | Path,
    output_root: str | Path,
    emit: Callable[[dict[str, str]], None] | None = None,
    storage_state: str | Path | None = None,
    interactive_auth: bool = False,
    capture_timeout_s: int = 900,
    annotations_path: str | Path | None = None,
    expansion_config: dict[str, object] | None = None,
) -> Path:
    """Backward-compatible wrapper: capture to a manifest, then build the replica.

    When ``annotations_path`` is supplied, capture runs through the annotation-aware
    ``capture_to_manifest`` boundary. When ``annotations_path=None`` (compat), the
    same capture core runs with temporary ``m_{index:03d}`` marker IDs. The build
    always delegates to ``build_from_manifest`` with an explicit source-path hash
    gate. The capture implementation is never duplicated here.
    """
    notify = emit or (lambda event: None)
    output_root = Path(output_root)
    capture_root = output_root / "capture"
    if annotations_path is not None:
        manifest_path = capture_to_manifest(
            script_path, annotations_path, capture_root, emit=notify,
            storage_state=storage_state, interactive_auth=interactive_auth,
            capture_timeout_s=capture_timeout_s,
            expansion_config=expansion_config,
        )
    else:
        manifest_path = _capture_to_manifest_core(
            script_path, capture_root, None, notify,
            storage_state, interactive_auth, capture_timeout_s,
            expansion_config=expansion_config,
        )
    entrypoint = build_from_manifest(
        manifest_path, capture_root, output_root / "replica",
        emit=_build_emitter(notify), source_path=script_path,
    )
    return entrypoint


def validate_annotations(script_path: str | Path, annotations_path: str | Path) -> dict[str, object]:
    """Reject stale GUI marker metadata before a live capture starts."""
    payload = json.loads(Path(annotations_path).read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise ValueError("unsupported replica annotations schema version")
    if payload.get("source_script_sha256") != sha256_file(script_path):
        raise ValueError("replica annotations do not match the processed script")
    return payload


def _coerce_marker_annotations(value: object | None) -> list[dict[str, object]] | None:
    """Accept either a GUI markers list or a full annotations payload dict for compat.

    The public functions accept both shapes because the 08-05 plumbing used a full
    payload dict while Task 5 threads the validated ``markers`` list. Both reduce to
    the same ``[{"marker_id", "line", "label"}]`` list before ``parse_action_plan``.
    """
    if value is None:
        return None
    if isinstance(value, dict):
        markers = value.get("markers")
        if markers is None:
            raise ValueError("annotations payload missing 'markers' list")
        return markers
    if isinstance(value, list):
        return value
    raise TypeError(f"unexpected marker_annotations type: {type(value).__name__}")


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
    source_path: str | Path | None = None,
) -> Path:
    """Build local assets from an already-captured flow without live network access.

    When ``source_path`` is supplied, the run's source script is hash-verified
    against ``flow.source_script_sha256`` before rendering (a provenance gate). The
    orchestrator must always supply the run's source script; do not copy a
    credential-bearing processed script into the asset directory merely to pass it.
    """
    notify = emit or (lambda event: print(json.dumps(event, ensure_ascii=False), flush=True))
    notify({"event": "build_started", "stage": "replica_build"})
    flow = read_manifest(manifest_path, flow_root)
    if source_path is not None and sha256_file(source_path) != flow.source_script_sha256:
        raise ValueError("source script hash does not match manifest")
    entrypoint = build_replica(flow, Path(flow_root), Path(output_root))
    notify({"event": "completed", "entrypoint": str(entrypoint)})
    return entrypoint


def main() -> None:
    parser = argparse.ArgumentParser(description="Capture/build a local interactive replica")
    parser.add_argument("--mode", choices=["capture-only", "offline-build", "live"], required=True)
    parser.add_argument("--manifest")
    parser.add_argument("--flow-root")
    parser.add_argument("--script")
    parser.add_argument("--annotations")
    parser.add_argument("--auth-mode", choices=["scripted", "interactive", "storage-state"], default="scripted")
    parser.add_argument("--storage-state")
    parser.add_argument("--capture-timeout", type=int, default=900)
    parser.add_argument("--output", required=True)
    parser.add_argument("--expand-all-series", action="store_true")
    parser.add_argument("--max-series", type=int, default=40)
    parser.add_argument("--per-series-timeout", type=int, default=20)
    parser.add_argument("--total-series-timeout", type=int, default=900)
    parser.add_argument("--viewer-capture-mode", default="first_stable_frame")
    args = parser.parse_args()
    emit = lambda event: print(json.dumps(event, ensure_ascii=False), flush=True)
    storage_state = args.storage_state if args.auth_mode == "storage-state" else None
    interactive_auth = args.auth_mode == "interactive"
    expansion_config = None
    if args.expand_all_series:
        expansion_config = {
            "expand_all_series": True,
            "max_series": args.max_series,
            "per_series_timeout_s": args.per_series_timeout,
            "total_series_timeout_s": args.total_series_timeout,
            "viewer_capture_mode": args.viewer_capture_mode,
        }
    if args.mode == "offline-build":
        if not args.manifest or not args.flow_root:
            parser.error("offline-build requires --manifest and --flow-root")
        entrypoint = build_from_manifest(
            args.manifest, args.flow_root, args.output, emit=emit,
            source_path=args.script,
        )
        print(json.dumps({"event": "completed", "entrypoint": str(entrypoint)}, ensure_ascii=False), flush=True)
        return
    if args.mode == "capture-only":
        if not args.script or not args.annotations:
            parser.error("capture-only requires --script and --annotations")
        try:
            # ``--output`` is already the run's capture root (the orchestrator
            # passes ``layout.capture_dir`` directly). Treating it as the root
            # (not ``output/capture``) keeps manifest + snapshot relpaths
            # anchored at ``layout.capture_dir`` where validation expects them.
            manifest_path = capture_to_manifest(
                args.script, args.annotations, Path(args.output),
                emit=emit, storage_state=storage_state,
                interactive_auth=interactive_auth,
                capture_timeout_s=args.capture_timeout,
                expansion_config=expansion_config,
            )
        except Exception as error:
            emit({"event": "failed", "category": classify_capture_error(error)})
            raise
        print(json.dumps({"event": "completed", "entrypoint": str(manifest_path)}, ensure_ascii=False), flush=True)
        return
    # live mode
    if not args.script:
        parser.error("live requires --script")
    if args.auth_mode == "storage-state" and not args.storage_state:
        parser.error("storage-state auth mode requires --storage-state")
    if args.auth_mode == "storage-state" and not Path(args.storage_state).is_file():
        parser.error("storage-state path does not exist")
    try:
        entrypoint = capture_and_build(
            args.script,
            args.output,
            emit=emit,
            storage_state=storage_state,
            interactive_auth=interactive_auth,
            capture_timeout_s=args.capture_timeout,
            annotations_path=args.annotations,
            expansion_config=expansion_config,
        )
    except Exception as error:
        emit({"event": "failed", "category": classify_capture_error(error)})
        raise
    print(json.dumps({"event": "completed", "entrypoint": str(entrypoint)}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
