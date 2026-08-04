# -*- coding: utf-8 -*-
"""Deterministic, exact-N canvas frame capture with a per-run manifest."""

from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MIN_CAPTURE_BYTES = 1024


@dataclass(frozen=True)
class NavigationResult:
    method: str
    changed: bool
    warning: str = ""


@dataclass(frozen=True)
class CaptureResult:
    path: Path
    method: str


def _new_run_dir(output_root: str | Path) -> Path:
    """Create and return a unique timestamped directory for one capture run."""
    root = Path(output_root)
    root.mkdir(parents=True, exist_ok=True)
    while True:
        name = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        run_dir = root / name
        try:
            run_dir.mkdir()
        except FileExistsError:
            time.sleep(0.000001)
            continue
        return run_dir


def _frame_list(page: Any) -> list[Any]:
    try:
        frames = list(page.frames)
    except Exception:
        frames = []
    try:
        main_frame = page.main_frame
    except Exception:
        main_frame = None
    if main_frame is not None:
        return [main_frame] + [frame for frame in frames if frame != main_frame]
    return frames


def _find_viewer_frame(page: Any) -> Any | None:
    """Return the main or child frame containing a canvas, when available."""
    frames = _frame_list(page)
    for frame in frames:
        try:
            if frame.locator("canvas").count() > 0:
                return frame
        except Exception:
            continue
    return frames[0] if frames else None


def _scope_text(scope: Any) -> str:
    for target in (scope,):
        try:
            return target.locator("body").inner_text()
        except Exception:
            pass
    return ""


def _parse_total_frames(scope: Any) -> int | None:
    """Parse common viewer frame counters such as ``1/120``, ``共120张`` and ``120 frames``."""
    text = _scope_text(scope)
    if not text:
        return None

    patterns = (
        r"\b\d+\s*/\s*(\d+)\b",
        r"(?:共|total(?:\s+(?:of|frames?))?)\s*[:：]?\s*(\d+)\s*(?:张|幅|层|帧|frames?|images?|slices?)?\b",
        r"\b(\d+)\s+frames?\b",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            total = int(match.group(1))
            if total > 0:
                return total
    return None


def _canvas_js(scope: Any, expression: str, *args: Any) -> Any:
    try:
        return scope.evaluate(expression, *args)
    except Exception:
        return None


def _find_largest_canvas(scope: Any) -> dict[str, float] | None:
    return _canvas_js(
        scope,
        """
        () => {
          const canvases = Array.from(document.querySelectorAll('canvas'));
          if (!canvases.length) return null;
          const canvas = canvases.reduce((best, current) =>
            current.width * current.height > best.width * best.height ? current : best
          );
          const rect = canvas.getBoundingClientRect();
          return {x: rect.x, y: rect.y, width: rect.width, height: rect.height};
        }
        """,
    )


def _activate_canvas(viewer_frame: Any, click_x: float, click_y: float) -> None:
    try:
        viewer_frame.locator("canvas").first.click(
            position={"x": click_x, "y": click_y}, timeout=3000
        )
    except Exception:
        pass


def _largest_visible_canvas_index(scope: Any) -> int:
    index = _canvas_js(
        scope,
        """
        () => {
          const visible = Array.from(document.querySelectorAll('canvas'))
            .map((canvas, index) => ({canvas, index, rect: canvas.getBoundingClientRect()}))
            .filter(({rect}) => rect.width > 0 && rect.height > 0);
          if (!visible.length) return -1;
          return visible.reduce((best, current) =>
            current.rect.width * current.rect.height > best.rect.width * best.rect.height
              ? current : best
          ).index;
        }
        """,
    )
    if not isinstance(index, int) or index < 0:
        raise RuntimeError("no visible canvas found")
    return index


def _largest_visible_canvas(scope: Any) -> Any:
    return scope.locator("canvas").nth(_largest_visible_canvas_index(scope))


def _canvas_hash(scope: Any) -> int | None:
    """Return a small JPEG-data-length fingerprint for the largest visible canvas."""
    value = _canvas_js(
        scope,
        """
        () => {
          const visible = Array.from(document.querySelectorAll('canvas'))
            .map(canvas => ({canvas, rect: canvas.getBoundingClientRect()}))
            .filter(({rect}) => rect.width > 0 && rect.height > 0);
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
    return value if isinstance(value, int) and value > 0 else None


def _wait_for_frame_change(
    scope: Any, previous_hash: int | None, timeout: float = 1.5
) -> bool:
    if previous_hash is None:
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(0.15)
        latest_hash = _canvas_hash(scope)
        if latest_hash is not None and latest_hash != previous_hash:
            return True
    return False


def _goto_frame_api(scope: Any, target_index: int) -> bool:
    return bool(
        _canvas_js(
            scope,
            """
            (target) => {
              const index = Math.max(0, target - 1);
              const visible = Array.from(document.querySelectorAll('canvas'))
                .map(canvas => ({canvas, rect: canvas.getBoundingClientRect()}))
                .filter(({rect}) => rect.width > 0 && rect.height > 0);
              const canvas = visible.reduce((best, current) =>
                !best || current.rect.width * current.rect.height > best.rect.width * best.rect.height
                  ? current : best, null)?.canvas;
              const setters = [
                window.setImageIndex,
                window.setImageIdIndex,
                window.viewer?.setImageIndex,
                window.viewer?.setFrameIndex,
                window.viewer?.gotoFrame,
              ];
              for (const setter of setters) {
                if (typeof setter === 'function') {
                  try { setter.call(window.viewer || window, index); return true; } catch (error) {}
                }
              }
              try {
                if (canvas && typeof window.cornerstone?.scrollToIndex === 'function') {
                  window.cornerstone.scrollToIndex(canvas, index);
                  return true;
                }
              } catch (error) {}
              return false;
            }
            """,
            target_index,
        )
    )


def _goto_frame_keyboard(scope: Any, viewer_page: Any, _target_index: int) -> bool:
    try:
        _largest_visible_canvas(scope).click(
            position={"x": 1, "y": 1}, timeout=2000, force=True
        )
        viewer_page.keyboard.press("ArrowDown")
        return True
    except Exception:
        return False


def _goto_frame_wheel(scope: Any, viewer_page: Any, _target_index: int) -> bool:
    try:
        canvas = _largest_visible_canvas(scope)
        canvas.hover(timeout=2000, force=True)
        viewer_page.mouse.wheel(0, 120)
        return True
    except Exception:
        return False


def _goto_frame_slider(scope: Any, target_index: int, total_frames: int | None) -> bool:
    return bool(
        _canvas_js(
            scope,
            """
            ([target, total]) => {
              const sliders = Array.from(document.querySelectorAll('input[type="range"], [role="slider"]'))
                .filter(slider => {
                  const rect = slider.getBoundingClientRect();
                  return rect.width > 0 && rect.height > 0;
                });
              if (!sliders.length) return false;
              const slider = sliders.reduce((best, current) => {
                const bestRect = best.getBoundingClientRect();
                const currentRect = current.getBoundingClientRect();
                return currentRect.width * currentRect.height > bestRect.width * bestRect.height
                  ? current : best;
              });
              const minimum = Number(slider.min ?? slider.getAttribute('aria-valuemin') ?? 0);
              const maximum = Number(slider.max ?? slider.getAttribute('aria-valuemax') ?? Math.max(0, total - 1));
              const value = Math.min(maximum, Math.max(minimum, minimum === 0 ? target - 1 : target));
              const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
              if (slider instanceof HTMLInputElement && setter) setter.call(slider, String(value));
              else slider.setAttribute('aria-valuenow', String(value));
              slider.dispatchEvent(new Event('input', {bubbles: true}));
              slider.dispatchEvent(new Event('change', {bubbles: true}));
              return true;
            }
            """,
            [target_index, total_frames or target_index],
        )
    )


def _navigate_to_frame(
    viewer_page: Any, target_index: int, total_frames: int | None = None
) -> NavigationResult:
    """Navigate only when a canvas fingerprint confirms the resulting frame changed."""
    viewer_frame = _find_viewer_frame(viewer_page) or viewer_page
    previous_hash = _canvas_hash(viewer_frame)
    attempts = (
        ("api", lambda: _goto_frame_api(viewer_frame, target_index)),
        ("keyboard", lambda: _goto_frame_keyboard(viewer_frame, viewer_page, target_index)),
        ("wheel", lambda: _goto_frame_wheel(viewer_frame, viewer_page, target_index)),
        ("slider", lambda: _goto_frame_slider(viewer_frame, target_index, total_frames)),
    )
    failures: list[str] = []
    for method, goto in attempts:
        try:
            if not goto():
                failures.append(f"{method} unavailable")
                continue
            if _wait_for_frame_change(viewer_frame, previous_hash):
                return NavigationResult(method=method, changed=True)
            failures.append(f"{method} did not change canvas")
        except Exception as error:
            failures.append(f"{method}: {error}")
    return NavigationResult(
        method="unconfirmed",
        changed=False,
        warning=f"frame {target_index} navigation unconfirmed: {'; '.join(failures)}",
    )


def _capture_canvas_js(scope: Any, path: Path) -> None:
    data_url = _canvas_js(
        scope,
        """
        () => {
          const visible = Array.from(document.querySelectorAll('canvas'))
            .map(canvas => ({canvas, rect: canvas.getBoundingClientRect()}))
            .filter(({rect}) => rect.width > 0 && rect.height > 0);
          const source = visible.reduce((best, current) =>
            !best || current.rect.width * current.rect.height > best.rect.width * best.rect.height
              ? current : best, null)?.canvas;
          if (!source || !source.width || !source.height) return null;
          const scratch = document.createElement('canvas');
          scratch.width = source.width;
          scratch.height = source.height;
          const context = scratch.getContext('2d');
          if (!context) return null;
          context.drawImage(source, 0, 0, scratch.width, scratch.height);
          return scratch.toDataURL('image/jpeg', 0.95);
        }
        """,
    )
    if not isinstance(data_url, str) or not data_url.startswith("data:image/jpeg"):
        raise RuntimeError("no JPEG data URL")
    _, separator, encoded = data_url.partition(",")
    if not separator:
        raise RuntimeError("malformed canvas data URL")
    path.write_bytes(base64.b64decode(encoded, validate=True))


def _capture_canvas_locator(scope: Any, path: Path) -> None:
    _largest_visible_canvas(scope).screenshot(
        path=str(path), type="jpeg", quality=95
    )


def _capture_frame(viewer_frame: Any, path: Path) -> CaptureResult:
    """Capture one frame through canvas JS, locator, then a full-page screenshot."""
    failures: list[str] = []
    for method, capture in (
        ("canvas_js", lambda: _capture_canvas_js(viewer_frame, path)),
        ("canvas_locator", lambda: _capture_canvas_locator(viewer_frame, path)),
    ):
        try:
            capture()
            if path.is_file() and path.stat().st_size >= MIN_CAPTURE_BYTES:
                return CaptureResult(path=path, method=method)
            failures.append(f"{method}: output is too small ({path.stat().st_size if path.exists() else 0} bytes)")
        except Exception as error:
            failures.append(f"{method}: {error}")

    try:
        page = getattr(viewer_frame, "page", None)
        if page is None and hasattr(viewer_frame, "screenshot"):
            page = viewer_frame
        if page is None:
            raise RuntimeError("viewer page is unavailable")
        page.screenshot(path=str(path), type="jpeg", quality=95, full_page=True)
        if path.is_file() and path.stat().st_size >= MIN_CAPTURE_BYTES:
            return CaptureResult(path=path, method="page")
        failures.append(f"page: output is too small ({path.stat().st_size if path.exists() else 0} bytes)")
    except Exception as error:
        failures.append(f"page: {error}")

    raise RuntimeError(f"all capture strategies failed for {path}: {'; '.join(failures)}")


def _validate_capture(result: CaptureResult, expected_path: Path) -> CaptureResult:
    path = Path(result.path)
    if path != expected_path:
        raise RuntimeError(f"capture returned {path}, expected {expected_path}")
    if not path.exists():
        raise FileNotFoundError(f"capture file was not created: {path}")
    size = path.stat().st_size
    if size < MIN_CAPTURE_BYTES:
        raise ValueError(f"capture file is too small ({size} bytes): {path}")
    return CaptureResult(path=path, method=result.method)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def capture_canvas_interaction(
    viewer_page: Any,
    click_x: float,
    click_y: float,
    total_frames: int | None = None,
    output_root: str | Path = "canvas_frames",
    series_name: str | None = None,
) -> list[CaptureResult]:
    """Capture frames 1..N exactly once and write a manifest for the run."""
    started_at = _iso_now()
    viewer_frame = _find_viewer_frame(viewer_page) or viewer_page
    requested_total = total_frames or _parse_total_frames(viewer_frame)
    if requested_total is None:
        requested_total = _parse_total_frames(viewer_page)
    if requested_total is None or requested_total <= 0:
        raise ValueError("total frame count is required and must be positive")

    run_dir = _new_run_dir(output_root)
    _activate_canvas(viewer_frame, click_x, click_y)

    results: list[CaptureResult] = []
    manifest_frames: list[dict[str, Any]] = []
    for frame_index in range(1, requested_total + 1):
        if frame_index == 1:
            navigation = NavigationResult(method="initial", changed=True)
        else:
            navigation = _navigate_to_frame(viewer_page, frame_index, requested_total)
            if not isinstance(navigation, NavigationResult):
                navigation = NavigationResult(
                    method=getattr(navigation, "method", "unknown"),
                    changed=bool(getattr(navigation, "changed", False)),
                    warning=getattr(navigation, "warning", ""),
                )

        path = run_dir / f"canvas_frame_{frame_index:04d}.jpeg"
        captured = _capture_frame(viewer_frame, path)
        if not isinstance(captured, CaptureResult):
            captured = CaptureResult(
                path=Path(getattr(captured, "path", path)),
                method=getattr(captured, "method", "unknown"),
            )
        captured = _validate_capture(captured, path)
        results.append(captured)
        manifest_frames.append(
            {
                "frame_index": frame_index,
                "filename": path.name,
                "capture_method": captured.method,
                "navigation_method": navigation.method,
                "change_confirmed": navigation.changed,
                "file_size": path.stat().st_size,
                "warning": navigation.warning,
            }
        )

    expected_names = {
        f"canvas_frame_{frame_index:04d}.jpeg"
        for frame_index in range(1, requested_total + 1)
    }
    numbered_files = list(run_dir.glob("canvas_frame_*.jpeg"))
    if len(results) != requested_total:
        raise RuntimeError(
            f"saved path count mismatch: {len(results)} != {requested_total}"
        )
    if len(numbered_files) != requested_total or {
        path.name for path in numbered_files
    } != expected_names:
        raise RuntimeError(
            f"numbered file count mismatch in {run_dir}: "
            f"{len(numbered_files)} != {requested_total}"
        )

    manifest = {
        "series_name": series_name,
        "requested_frame_count": requested_total,
        "saved_frame_count": len(results),
        "started_at": started_at,
        "completed_at": _iso_now(),
        "frames": manifest_frames,
    }
    (run_dir / "capture_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return results
