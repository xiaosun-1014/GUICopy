"""Viewer state waits shared by generated automation scripts."""

from __future__ import annotations

import re
import time


_FRAME_COUNT_RE = re.compile(
    r"(?<!\d)(\d{1,4})\s*(?:张|幅|层|帧|images?|slices?|frames?)",
    re.IGNORECASE,
)
_PREFERRED_SERIES_RE = re.compile(
    r"(lung|thin|hrct|mpr|coronal|sagittal|axial|bone|brain|"
    r"mediastinum|body|chest|abdomen|head|ce|肺|骨|脑)",
    re.IGNORECASE,
)
_LOCALIZER_RE = re.compile(r"(scout|localizer|定位|导引)", re.IGNORECASE)
_STRUCTURAL_SERIES_SELECTORS = (
    "#seriesList li[data-seriesid]",
    "li[data-seriesid]",
    "[data-seriesuuid][data-idx]",
    ".series-list li",
)


def _normalized_text(value: str) -> str:
    return " ".join((value or "").split())


def _series_frame_count(text: str) -> int | None:
    """Parse one concrete series count while rejecting report summaries."""
    matches = _FRAME_COUNT_RE.findall(text)
    if len(matches) != 1:
        return None
    if re.search(r"序列\s*[:：]", text) or re.search(r"影像\s*[:：]", text):
        return None
    return int(matches[0])


def select_structural_series(page: object) -> tuple[str, int] | None:
    """Double-click the best item from a real series-list container.

    This intentionally scans known structural containers before any generic
    body-text heuristic, so report totals and parent nodes cannot masquerade as
    individual series.
    """
    scopes = [page.main_frame]
    scopes.extend(frame for frame in page.frames if frame is not page.main_frame)

    for selector in _STRUCTURAL_SERIES_SELECTORS:
        candidates = []
        for scope_index, scope in enumerate(scopes):
            items = scope.locator(selector)
            try:
                item_count = items.count()
            except Exception:
                continue
            for item_index in range(item_count):
                item = items.nth(item_index)
                try:
                    if not item.is_visible():
                        continue
                    text = _normalized_text(item.inner_text())
                except Exception:
                    continue
                frames = _series_frame_count(text)
                if frames is None:
                    continue
                preference = len(_PREFERRED_SERIES_RE.findall(text))
                localizer_penalty = 1 if _LOCALIZER_RE.search(text) else 0
                score = (frames, preference, -localizer_penalty, -scope_index, -item_index)
                candidates.append((score, item, text, frames))

        if not candidates:
            continue

        _, item, text, frames = max(candidates, key=lambda candidate: candidate[0])
        target = item
        try:
            links = item.locator("a")
            if links.count() and links.first.is_visible():
                target = links.first
        except Exception:
            pass
        target.dblclick(timeout=10000)
        print(f"[序列选择] 结构化策略命中: {text} ({frames}张)")
        return text, frames

    return None


def wait_for_pre_action_state(page: object, marker_label: str) -> None:
    """Let an asynchronous FTImage report finish attaching before selection."""
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
    timeout_s: float = 15.0,
    stable_s: float = 1.0,
) -> bool:
    """Wait until the report overlay is hidden and the toolbar is usable."""
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
    stable_since = None
    while time.monotonic() < deadline:
        try:
            report_hidden = not has_report or not report.is_visible()
            more_ready = (
                not has_more_tool
                or (
                    more_tool.is_visible()
                    and "disabled"
                    not in (more_tool.get_attribute("class") or "").split()
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
