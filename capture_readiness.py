"""Shared, dependency-light helpers for waiting on asynchronous UI readiness.

These helpers were promoted from module-private functions in
``batch_capture_replicate`` so that live replay capture and future per-series
(auto-expansion) capture in this repo can share a single source of truth when
waiting for a DICOM Metadata panel to finish loading.

This module is intentionally dependency-free toward the helpers themselves: it
only reads the Metadata candidate selector table from ``capture_snapshot``
(public, read-only) and otherwise stands alone so it can be imported by any
capture-stage module without pulling in the whole live-capture stack.
"""

from __future__ import annotations

import hashlib
import io
import re
import time

from PIL import Image, ImageStat

from capture_snapshot import _MARKER_REGION_CANDIDATES


_METADATA_TEXT_RE = re.compile(
    r"(?:series\s*(?:number|description|instance\s*uid)|"
    r"study\s*(?:description|instance\s*uid)|"
    r"\(\s*[0-9a-f]{4}\s*,\s*[0-9a-f]{4}\s*\)|序列(?:号|描述|实例))",
    re.IGNORECASE,
)


def _metadata_candidate_allowed(selector: str, text: str, tag: str) -> bool:
    """Return whether a visible candidate plausibly represents Metadata.

    Generic ``*info*`` selectors match patient banners, while strong ``*tags*``
    selectors can match permanent toolbar containers. Every accepted root must
    therefore carry Metadata-specific fields, not merely a suggestive class.
    """
    normalized_tag = (tag or "").lower()
    if normalized_tag in {
        "a", "button", "html", "body", "i", "input", "main", "select",
        "span", "svg", "textarea",
    }:
        return False
    _ = selector
    return bool(_METADATA_TEXT_RE.search(text or ""))


def metadata_panel_signature(locator_factory: object) -> str | None:
    """Compute a stable fingerprint of the visible Metadata panel content.

    Walks the Metadata candidate selectors (scoped to the highest ancestor html
    of the target locator), and returns ``"{text}\\0{scrollHeight}"`` for the
    first visible non-interactive element that matches. Returns ``None`` when the
    panel is not present, not yet visible, or on any locator failure.

    The caller passes a ``locator_factory`` callable (typically ``lambda:
    <locator>``) rather than a live Locator so that re-resolution happens on each
    poll, matching how recordings capture frame/panel targets.
    """
    try:
        target = locator_factory()
        scope = target.locator("xpath=ancestor::html")
        candidates = _MARKER_REGION_CANDIDATES["Meta 信息工具"][1]
        for selector in candidates:
            matches = scope.locator(selector)
            for index in range(matches.count()):
                candidate = matches.nth(index)
                if not candidate.is_visible():
                    continue
                payload = candidate.evaluate(
                    """element => ({
                        tag: element.tagName.toLowerCase(),
                        text: (element.innerText || element.textContent || '').trim(),
                        scrollHeight: element.scrollHeight,
                    })"""
                )
                if not _metadata_candidate_allowed(selector, payload["text"], payload["tag"]):
                    continue
                return f'{payload["text"]}\0{payload["scrollHeight"]}'
    except Exception:
        return None
    return None


def wait_for_metadata_panel_state(
    page: object,
    locator_factory: object,
    timeout_s: float,
    stable_s: float,
) -> bool:
    """Return True once the Metadata panel signature has been stable for ``stable_s``.

    Polls the panel signature until either the same non-``None`` signature has
    persisted for at least ``stable_s`` seconds (ready) or ``timeout_s`` elapses
    (returns False). A ``None`` signature resets the stability window, so a panel
    that is still filling in — or that fails to resolve — never counts as ready.
    """
    deadline = time.monotonic() + timeout_s
    previous: str | None = None
    stable_since: float | None = None
    while time.monotonic() < deadline:
        signature = metadata_panel_signature(locator_factory)
        now = time.monotonic()
        if signature is None:
            previous = None
            stable_since = None
        elif signature != previous:
            previous = signature
            stable_since = now
        elif stable_since is not None and now - stable_since >= stable_s:
            return True
        page.wait_for_timeout(100)
    return False


def canvas_hash(scope: object) -> int | None:
    """Return a small JPEG-data-length fingerprint for the largest visible canvas.

    Promoted from ``skills/_shared/canvas_capture._canvas_hash`` so the live
    per-series explorer and future stages share one public readiness signal. A
    ``None`` means no visible canvas (or the scope is not ready); callers rely on
    a *combination* of readiness evidence, never this value alone.
    """
    try:
        value = scope.evaluate(
            """() => {
              const visible = Array.from(document.querySelectorAll('canvas'))
                .map(canvas => ({canvas, rect: canvas.getBoundingClientRect()}))
                .filter(({rect}) => rect.width > 0 && rect.height > 0);
              if (!visible.length) return null;
              const source = visible.reduce((best, current) =>
                !best || current.rect.width * current.rect.height > best.rect.width * best.rect.height
                  ? current : best, null)?.canvas;
              if (!source || !source.width || !source.height) return null;
              const scratch = document.createElement('canvas');
              scratch.width = 80;
              scratch.height = 60;
              const context = scratch.getContext('2d');
              if (!context) return null;
              context.drawImage(source, 0, 0, source.width, source.height, 0, 0, 80, 60);
              return scratch.toDataURL('image/jpeg', 0.3).length;
            }
            """,
        )
    except Exception:
        return None
    return value if isinstance(value, int) and value > 0 else None


def wait_for_frame_change(scope: object, previous_hash: int | None, timeout: float = 1.5) -> bool:
    """Return True once the largest visible canvas differs from ``previous_hash``."""
    if previous_hash is None:
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(0.15)
        latest_hash = canvas_hash(scope)
        if latest_hash is not None and latest_hash != previous_hash:
            return True
    return False


def viewer_dom_fingerprint(frame: object, selector: str = "body") -> str | None:
    """Return a cheap, stable fingerprint of the viewer frame's semantic DOM.

    Used as one of several combined readiness signals: when two consecutive
    samples are equal the viewer is no longer churning its layout/content. It is
    deliberately coarse (text + scrollHeight + child count) so transient pixel
    noise does not cause false "still changing" results.
    """
    try:
        payload = frame.locator(selector).evaluate(
            """element => ({
                text: (element.innerText || element.textContent || '').trim().slice(0, 2000),
                scrollHeight: element.scrollHeight,
                childCount: element.children.length,
            })"""
        )
    except Exception:
        return None
    if not payload:
        return None
    return f'{payload.get("text", "")}\0{payload.get("scrollHeight", 0)}\0{payload.get("childCount", 0)}'


def screenshot_nonblank(png_bytes: bytes, min_stddev: float = 2.0, min_bytes: int = 1024) -> bool:
    """Return True when a PNG screenshot is non-empty and not uniformly black.

    A blank (black) viewer produces a near-zero standard deviation; a genuinely
    rendered viewer produces texture. This is deliberately a *coarse* readiness
    signal combined with selected-state / DOM-stability evidence.
    """
    if png_bytes is None or len(png_bytes) < min_bytes:
        return False
    try:
        with Image.open(io.BytesIO(png_bytes)) as image:
            grayscale = image.convert("L").resize((160, 90))
            return ImageStat.Stat(grayscale).stddev[0] >= min_stddev
    except Exception:
        return False


def metadata_uid_sha256_prefix(uid: str) -> str | None:
    """Return a short SHA-256 prefix of a SeriesInstanceUID for local identity audit.

    The *original* UID is never written into filenames, logs or reports; only this
    non-reversible prefix is used to confirm a Metadata panel matches a temporary
    descriptor.
    """
    if not uid:
        return None
    return hashlib.sha256(str(uid).encode("utf-8")).hexdigest()[:16]
