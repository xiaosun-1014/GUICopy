# FTImage Every-Frame Canvas Capture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every generated adapter save exactly one numbered JPEG for every requested viewer frame and write a diagnostic manifest without deleting possible duplicates.

**Architecture:** Move canvas traversal and capture out of LLM-generated code into `skills/_shared/canvas_capture.py`. Make `agent.py` replace the canvas marker with a deterministic import/call block, just like deterministic Meta extraction. The runtime creates an isolated run directory, navigates best-effort, captures through three fallbacks, retains all indexes, writes a manifest, and asserts the final count.

**Tech Stack:** Python 3.10+, Playwright synchronous API, standard-library `base64`, `dataclasses`, `datetime`, `json`, `pathlib`, `re`, and `time`; `unittest` for tests.

---

## File Structure

- Create `skills/_shared/canvas_capture.py`: viewer frame discovery, frame-count parsing, navigation, render waiting, three-level screenshot fallback, one-file-per-index loop, manifest writing, and final count assertion.
- Create `test/test_canvas_capture.py`: focused unit tests for retention, manifest semantics, output isolation, fallback behavior, and count enforcement.
- Modify `agent.py`: add deterministic canvas marker generation and bypass the LLM for this marker.
- Modify `test/test_agent_marker_boundaries.py`: prove canvas marker generation is deterministic, preserves recorded coordinates, uses `SCRIPT_DIR`, and does not invoke the LLM.
- Modify `skills/marker-canvas-capture/SKILL.md`: replace the file-size-dedup contract with the exact-N contract and document the manifest/run directory.
- Generate `out/ftimage/completed_ftimage_fixed_v4.py`: fresh adapter generated from the recorded source; do not hand-edit it.

This checkout has no `.git` directory. The commit steps normally required by the skill cannot run here. After each task, record changed files and test evidence instead of committing.

### Task 1: Add the Exact-N Capture Loop and Manifest Contract

**Files:**
- Create: `skills/_shared/canvas_capture.py`
- Create: `test/test_canvas_capture.py`

- [ ] **Step 1: Write failing tests for retaining every requested index**

Create `test/test_canvas_capture.py` with a temporary output root and patch only browser-bound helpers:

```python
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from skills._shared.canvas_capture import (
    CaptureResult,
    NavigationResult,
    _capture_frame,
    capture_canvas_interaction,
)


class CanvasCaptureTests(unittest.TestCase):
    def test_equal_sized_frames_are_all_retained(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_root = Path(temp_dir) / "canvas_frames"

            def fake_capture(page, scope, path):
                path.write_bytes(b"x" * 2048)
                return CaptureResult(path=path, method="test")

            with (
                patch(
                    "skills._shared.canvas_capture._find_viewer_frame",
                    return_value=object(),
                ),
                patch(
                    "skills._shared.canvas_capture._parse_total_frames",
                    return_value=None,
                ),
                patch(
                    "skills._shared.canvas_capture._navigate_to_frame",
                    return_value=NavigationResult(
                        method="test", changed=False, warning="unconfirmed"
                    ),
                ),
                patch(
                    "skills._shared.canvas_capture._capture_frame",
                    side_effect=fake_capture,
                ),
            ):
                paths = capture_canvas_interaction(
                    object(),
                    click_x=0,
                    click_y=0,
                    total_frames=4,
                    output_root=output_root,
                    series_name="1.5_lung",
                )

            self.assertEqual(len(paths), 4)
            self.assertEqual(
                [Path(path).name for path in paths],
                [f"canvas_frame_{index:04d}.jpeg" for index in range(1, 5)],
            )
            self.assertTrue(all(Path(path).stat().st_size == 2048 for path in paths))

            manifest = json.loads(
                (Path(paths[0]).parent / "capture_manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(manifest["requested_frame_count"], 4)
            self.assertEqual(manifest["saved_frame_count"], 4)
            self.assertEqual(len(manifest["frames"]), 4)
            self.assertFalse(manifest["frames"][1]["change_confirmed"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```powershell
D:/Anaconda/envs/codegen-marker/python.exe -m unittest `
  test.test_canvas_capture.CanvasCaptureTests.test_equal_sized_frames_are_all_retained -v
```

Expected: import failure because `skills._shared.canvas_capture` does not exist.

- [ ] **Step 3: Implement the result types, isolated run directory, exact-N loop, and manifest**

Create `skills/_shared/canvas_capture.py` with these public types and entry point:

```python
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class NavigationResult:
    method: str
    changed: bool
    warning: str = ""


@dataclass(frozen=True)
class CaptureResult:
    path: Path
    method: str


def _new_run_dir(output_root: Path) -> Path:
    run_dir = output_root / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def capture_canvas_interaction(
    viewer_page,
    click_x: float,
    click_y: float,
    total_frames: int | None = None,
    output_root: str | Path = "canvas_frames",
    series_name: str | None = None,
) -> list[str]:
    scope = _find_viewer_frame(viewer_page)
    parsed_total = _parse_total_frames(scope)
    requested_total = int(total_frames or parsed_total or 1)
    if requested_total < 1:
        requested_total = 1

    run_dir = _new_run_dir(Path(output_root))
    started_at = datetime.now().isoformat(timespec="milliseconds")
    paths: list[str] = []
    frame_entries: list[dict] = []

    for frame_index in range(1, requested_total + 1):
        navigation = (
            NavigationResult(method="initial", changed=True)
            if frame_index == 1
            else _navigate_to_frame(
                viewer_page, scope, frame_index, frame_index - 1, requested_total
            )
        )
        target = run_dir / f"canvas_frame_{frame_index:04d}.jpeg"
        capture = _capture_frame(viewer_page, scope, target)
        if not target.is_file() or target.stat().st_size < 1024:
            raise RuntimeError(f"第 {frame_index} 帧截图无效: {target}")

        paths.append(str(target))
        frame_entries.append(
            {
                "frame_index": frame_index,
                "filename": target.name,
                "capture_method": capture.method,
                "navigation_method": navigation.method,
                "change_confirmed": navigation.changed,
                "file_size": target.stat().st_size,
                "warning": navigation.warning,
            }
        )

    numbered_files = sorted(run_dir.glob("canvas_frame_*.jpeg"))
    if len(numbered_files) != requested_total or len(paths) != requested_total:
        raise RuntimeError(
            f"逐帧截图数量不匹配: 期望 {requested_total}, "
            f"实际 {len(numbered_files)}, 目录 {run_dir}"
        )

    manifest = {
        "series_name": series_name,
        "requested_frame_count": requested_total,
        "saved_frame_count": len(paths),
        "started_at": started_at,
        "completed_at": datetime.now().isoformat(timespec="milliseconds"),
        "frames": frame_entries,
    }
    (run_dir / "capture_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return paths
```

In the same file, initially define `_find_viewer_frame`,
`_parse_total_frames`, `_navigate_to_frame`, and `_capture_frame` with the exact
signatures above. `_find_viewer_frame` returns `page.main_frame` when it has a
canvas, otherwise the visible child frame with the largest canvas.
`_parse_total_frames` searches body text for `current/total`, `共 N 张`, or
`N frames`. The browser-bound implementations are completed in Task 2.

- [ ] **Step 4: Run the focused test and verify GREEN**

Run the command from Step 2.

Expected: one passing test; four files with equal sizes remain present and the
manifest contains four entries.

- [ ] **Step 5: Record checkpoint**

Record:

```text
Changed: skills/_shared/canvas_capture.py, test/test_canvas_capture.py
Evidence: exact-N equal-size retention test passes
```

### Task 2: Implement Navigation, Render Waiting, and Screenshot Fallbacks

**Files:**
- Modify: `skills/_shared/canvas_capture.py`
- Modify: `test/test_canvas_capture.py`

- [ ] **Step 1: Add failing tests for unconfirmed navigation and screenshot fallback**

Add these test cases:

```python
    def test_unconfirmed_navigation_still_saves_every_index(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_root = Path(temp_dir) / "canvas_frames"

            def fake_capture(page, scope, path):
                path.write_bytes(b"j" * 2048)
                return CaptureResult(path=path, method="page")

            with (
                patch(
                    "skills._shared.canvas_capture._find_viewer_frame",
                    return_value=object(),
                ),
                patch(
                    "skills._shared.canvas_capture._parse_total_frames",
                    return_value=None,
                ),
                patch(
                    "skills._shared.canvas_capture._navigate_to_frame",
                    return_value=NavigationResult(
                        method="keyboard",
                        changed=False,
                        warning="渲染变化未确认，仍按策略保存",
                    ),
                ),
                patch(
                    "skills._shared.canvas_capture._capture_frame",
                    side_effect=fake_capture,
                ),
            ):
                paths = capture_canvas_interaction(
                    object(), 0, 0, total_frames=3, output_root=output_root
                )

            manifest = json.loads(
                (Path(paths[0]).parent / "capture_manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(len(paths), 3)
            self.assertEqual(manifest["frames"][1]["navigation_method"], "keyboard")
            self.assertFalse(manifest["frames"][1]["change_confirmed"])
            self.assertIn("仍按策略保存", manifest["frames"][1]["warning"])

    def test_capture_uses_page_fallback_when_canvas_methods_fail(self):
        class FakePage:
            def screenshot(self, **kwargs):
                Path(kwargs["path"]).write_bytes(b"p" * 2048)

        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "canvas_frame_0001.jpeg"
            with (
                patch(
                    "skills._shared.canvas_capture._capture_canvas_js",
                    side_effect=RuntimeError("js failed"),
                ),
                patch(
                    "skills._shared.canvas_capture._capture_canvas_locator",
                    side_effect=RuntimeError("locator failed"),
                ),
            ):
                result = _capture_frame(FakePage(), object(), target)

            self.assertEqual(result.method, "page")
            self.assertGreaterEqual(target.stat().st_size, 1024)
```

- [ ] **Step 2: Run both tests and verify RED**

Run:

```powershell
D:/Anaconda/envs/codegen-marker/python.exe -m unittest `
  test.test_canvas_capture.CanvasCaptureTests.test_unconfirmed_navigation_still_saves_every_index `
  test.test_canvas_capture.CanvasCaptureTests.test_capture_uses_page_fallback_when_canvas_methods_fail -v
```

Expected: failure because the real fallback helpers and navigation result
behavior are not implemented.

- [ ] **Step 3: Implement browser-bound helpers**

Implement:

```python
def _capture_frame(viewer_page, scope, target: Path) -> CaptureResult:
    failures = []
    for method, operation in (
        ("canvas_js", lambda: _capture_canvas_js(scope, target)),
        ("canvas_locator", lambda: _capture_canvas_locator(scope, target)),
        (
            "page",
            lambda: viewer_page.screenshot(
                path=str(target), type="jpeg", quality=95, full_page=True
            ),
        ),
    ):
        try:
            operation()
            if target.is_file() and target.stat().st_size >= 1024:
                return CaptureResult(path=target, method=method)
        except Exception as exc:
            failures.append(f"{method}: {exc}")
    raise RuntimeError(
        f"所有截图策略均失败: {target}; " + "; ".join(failures)
    )
```

Implement `_capture_canvas_js` by selecting the largest visible canvas in the
target frame, drawing it into a same-size 2D scratch canvas, returning
`toDataURL("image/jpeg", 0.95)`, decoding the Base64 payload, and writing it to
`target`. Implement `_capture_canvas_locator` with the largest visible canvas
locator and `locator.screenshot(path=str(target), type="jpeg", quality=95)`.

Implement `_navigate_to_frame` with this sequence:

```python
def _navigate_to_frame(
    viewer_page, scope, target: int, current: int, total: int
) -> NavigationResult:
    previous_hash = _canvas_hash(scope)
    attempts = (
        ("api", lambda: _goto_frame_api(scope, target)),
        ("keyboard", lambda: _goto_frame_keyboard(viewer_page, scope, target, current)),
        ("wheel", lambda: _goto_frame_wheel(viewer_page, scope, target, current)),
        ("slider", lambda: _goto_frame_slider(scope, target, total)),
    )
    warnings = []
    for method, operation in attempts:
        try:
            if not operation():
                continue
            changed = _wait_for_frame_change(scope, previous_hash)
            if changed:
                return NavigationResult(method=method, changed=True)
            warnings.append(f"{method} 后未确认画面变化")
        except Exception as exc:
            warnings.append(f"{method}: {exc}")
    return NavigationResult(
        method="unconfirmed",
        changed=False,
        warning="; ".join(warnings) or "没有可用的翻页策略，仍按策略保存",
    )
```

Use the existing generated implementation in
`out/ftimage/completed_ftimage_fixed_v3.py` as behavioral reference for the
four `_goto_frame_*` helpers and the 80×60 scratch-canvas hash. Do not copy its
`seen_sizes`, file deletion, timestamped filename, or relative output path.

- [ ] **Step 4: Run all canvas tests**

Run:

```powershell
D:/Anaconda/envs/codegen-marker/python.exe -m unittest test.test_canvas_capture -v
```

Expected: all canvas tests pass.

- [ ] **Step 5: Record checkpoint**

Record:

```text
Changed: skills/_shared/canvas_capture.py, test/test_canvas_capture.py
Evidence: exact-N, unconfirmed navigation, and page fallback tests pass
```

### Task 3: Make Canvas Marker Generation Deterministic

**Files:**
- Modify: `agent.py`
- Modify: `test/test_agent_marker_boundaries.py`

- [ ] **Step 1: Write a failing generator test**

Add:

```python
    def test_canvas_marker_uses_deterministic_generator_without_llm(self):
        script = """from pathlib import Path
def run(page):
    # [MARKER: 影像画布交互]
    # recorded canvas action follows
    page.locator("canvas").click(position={"x":400,"y":383})
    context.close()
"""
        with patch.object(
            agent,
            "call_llm",
            side_effect=AssertionError("canvas marker must not invoke LLM"),
        ):
            completed = agent.process_script(script)

        self.assertIn(
            "from skills._shared.canvas_capture import capture_canvas_interaction",
            completed,
        )
        self.assertIn('output_root=SCRIPT_DIR / "canvas_frames"', completed)
        self.assertIn('total_frames=locals().get("seq_frames")', completed)
        self.assertIn('series_name=locals().get("seq_name")', completed)
        self.assertIn("click_x=400.0", completed)
        self.assertIn("click_y=383.0", completed)
        self.assertNotIn('page.locator("canvas").click', completed)
        self.assertIn("context.close()", completed)
```

- [ ] **Step 2: Run the generator test and verify RED**

Run:

```powershell
D:/Anaconda/envs/codegen-marker/python.exe -m unittest `
  test.test_agent_marker_boundaries.AgentMarkerBoundaryTests.test_canvas_marker_uses_deterministic_generator_without_llm -v
```

Expected: `AssertionError` from `call_llm` because canvas generation is still
LLM-driven.

- [ ] **Step 3: Implement `_generate_deterministic_canvas`**

Add a helper beside `_generate_deterministic_meta`:

```python
def _generate_deterministic_canvas(marker: Dict) -> str:
    context_text = "\n".join(
        marker["context_before"]
        + marker["raw"].splitlines()
        + marker["context_after"]
    )
    page_match = re.search(r"\b(page\d*)\.", context_text)
    page_var = page_match.group(1) if page_match else "page"
    position_match = re.search(
        r'position\s*=\s*\{\s*["\']x["\']\s*:\s*([0-9.]+)\s*,'
        r'\s*["\']y["\']\s*:\s*([0-9.]+)\s*\}',
        marker["raw"],
    )
    click_x = float(position_match.group(1)) if position_match else 0.0
    click_y = float(position_match.group(2)) if position_match else 0.0
    block = [
        "# [MARKER: 影像画布交互]",
        "import sys as _canvas_sys",
        "from pathlib import Path as _CanvasPath",
        "SCRIPT_DIR = _CanvasPath(__file__).resolve().parent",
        "_CANVAS_PROJECT = SCRIPT_DIR.parent.parent",
        "if str(_CANVAS_PROJECT) not in _canvas_sys.path:",
        "    _canvas_sys.path.insert(0, str(_CANVAS_PROJECT))",
        "from skills._shared.canvas_capture import capture_canvas_interaction",
        "frame_paths = capture_canvas_interaction(",
        f"    {page_var},",
        f"    click_x={click_x!r},",
        f"    click_y={click_y!r},",
        '    total_frames=locals().get("seq_frames"),',
        '    series_name=locals().get("seq_name"),',
        '    output_root=SCRIPT_DIR / "canvas_frames",',
        ")",
        'print(f"[画布] 已保存 {len(frame_paths)} 个逐帧截图")',
    ]
    indent = marker["indent"]
    return "\n".join(indent + line if line else line for line in block)
```

In `process_script`, place the canvas deterministic branch beside the Meta
branch, syntax-check the replacement against the whole script, replace the
marker-owned lines, print a deterministic status message, and `continue`
without loading the skill bundle or calling the LLM.

- [ ] **Step 4: Run generator tests and syntax checks**

Run:

```powershell
D:/Anaconda/envs/codegen-marker/python.exe -m unittest `
  test.test_agent_marker_boundaries -v
D:/Anaconda/envs/codegen-marker/python.exe -m py_compile agent.py `
  skills/_shared/canvas_capture.py
```

Expected: all marker-boundary tests pass and compilation exits 0.

- [ ] **Step 5: Record checkpoint**

Record:

```text
Changed: agent.py, test/test_agent_marker_boundaries.py
Evidence: canvas marker bypasses LLM and generated block compiles
```

### Task 4: Align the Canvas Skill Contract

**Files:**
- Modify: `skills/marker-canvas-capture/SKILL.md`
- Modify: `test/test_agent_marker_boundaries.py`

- [ ] **Step 1: Add a failing skill-contract test**

Add:

```python
    def test_canvas_skill_requires_exact_n_outputs_without_dedup(self):
        skill = (
            PROJECT_ROOT / "skills" / "marker-canvas-capture" / "SKILL.md"
        ).read_text(encoding="utf-8")

        self.assertIn("capture_manifest.json", skill)
        self.assertIn("每次运行一个独立子目录", skill)
        self.assertIn("不得按文件大小去重", skill)
        self.assertNotIn("seen_sizes", skill)
```

- [ ] **Step 2: Run the skill-contract test and verify RED**

Run:

```powershell
D:/Anaconda/envs/codegen-marker/python.exe -m unittest `
  test.test_agent_marker_boundaries.AgentMarkerBoundaryTests.test_canvas_skill_requires_exact_n_outputs_without_dedup -v
```

Expected: failure because the existing skill explicitly requires file-size
deduplication.

- [ ] **Step 3: Rewrite the affected skill sections**

Update the description, marker replacement overview, architecture diagram,
pitfall table, output section, and complete replacement example so they state:

```text
全量帧逐帧截图 → 每个请求索引无条件落盘 → capture_manifest.json
不得按文件大小或渲染 hash 删除任何帧。
输出目录：
SCRIPT_DIR/canvas_frames/YYYYMMDD_HHMMSS_ffffff/
文件：
canvas_frame_0001.jpeg ... canvas_frame_NNNN.jpeg
```

Remove every instruction that recommends `seen_sizes`, file-size
deduplication, or deletion of a possible duplicate.

- [ ] **Step 4: Run the skill-contract and canvas tests**

Run:

```powershell
D:/Anaconda/envs/codegen-marker/python.exe -m unittest `
  test.test_agent_marker_boundaries `
  test.test_canvas_capture -v
```

Expected: all tests pass.

- [ ] **Step 5: Record checkpoint**

Record:

```text
Changed: skills/marker-canvas-capture/SKILL.md
Evidence: documented marker contract matches deterministic runtime
```

### Task 5: Regenerate and Statically Verify the FTImage Adapter

**Files:**
- Generate: `out/ftimage/completed_ftimage_fixed_v4.py`

- [ ] **Step 1: Generate from the recorded source**

Run:

```powershell
D:/Anaconda/envs/codegen-marker/python.exe agent.py `
  out/ftimage/processed_script_ftimage.py `
  -o out/ftimage/completed_ftimage_fixed_v4.py
```

Expected: all five markers process successfully; Meta and canvas report
`deterministic`; the output file is written.

- [ ] **Step 2: Compile the generated adapter**

Run:

```powershell
D:/Anaconda/envs/codegen-marker/python.exe -m py_compile `
  out/ftimage/completed_ftimage_fixed_v4.py
```

Expected: exit code 0.

- [ ] **Step 3: Inspect the generated canvas block**

Run:

```powershell
Select-String `
  -Path out/ftimage/completed_ftimage_fixed_v4.py `
  -Pattern 'capture_canvas_interaction|output_root|seen_sizes|os.remove'
```

Expected:

- one shared `capture_canvas_interaction` import and call;
- `output_root=SCRIPT_DIR / "canvas_frames"`;
- no `seen_sizes`;
- no canvas-frame deletion logic.

- [ ] **Step 4: Run the complete focused regression suite**

Run:

```powershell
D:/Anaconda/envs/codegen-marker/python.exe -m unittest `
  test.test_canvas_capture `
  test.test_meta_extract `
  test.test_agent_marker_boundaries `
  test.test_viewer_state -v
```

Expected: all tests pass with zero failures and zero errors.

- [ ] **Step 5: Record checkpoint**

Record:

```text
Generated: out/ftimage/completed_ftimage_fixed_v4.py
Evidence: generated adapter compiles and contains no frame deduplication
```

### Task 6: Verify All 278 Frames on the Real FTImage Viewer

**Files produced at runtime:**
- `out/ftimage/report.jpeg`
- `out/ftimage/meta_validation/dicom_meta.json`
- `out/ftimage/canvas_frames/<run-id>/canvas_frame_0001.jpeg`
- `out/ftimage/canvas_frames/<run-id>/...`
- `out/ftimage/canvas_frames/<run-id>/canvas_frame_0278.jpeg`
- `out/ftimage/canvas_frames/<run-id>/capture_manifest.json`

- [ ] **Step 1: Run the generated adapter**

Run:

```powershell
$env:PYTHONUNBUFFERED = "1"
D:/Anaconda/envs/codegen-marker/python.exe `
  out/ftimage/completed_ftimage_fixed_v4.py
Remove-Item Env:PYTHONUNBUFFERED
```

Expected: the process exits 0 after selecting the 278-frame lung series and
capturing all requested indexes.

- [ ] **Step 2: Verify the newest run directory**

Run:

```powershell
$runDir = Get-ChildItem out/ftimage/canvas_frames -Directory |
  Sort-Object LastWriteTime -Descending |
  Select-Object -First 1
$frames = Get-ChildItem $runDir.FullName -Filter 'canvas_frame_*.jpeg'
$manifest = Get-Content -Raw ($runDir.FullName + '\capture_manifest.json') |
  ConvertFrom-Json
[pscustomobject]@{
  RunDirectory = $runDir.FullName
  FrameFiles = $frames.Count
  ManifestFrames = $manifest.frames.Count
  Requested = $manifest.requested_frame_count
  Saved = $manifest.saved_frame_count
}
```

Expected:

```text
FrameFiles    = 278
ManifestFrames = 278
Requested     = 278
Saved         = 278
```

- [ ] **Step 3: Verify numbering and file validity**

Run:

```powershell
$expected = 1..278 | ForEach-Object { 'canvas_frame_{0:D4}.jpeg' -f $_ }
$actual = Get-ChildItem $runDir.FullName -Filter 'canvas_frame_*.jpeg' |
  Sort-Object Name |
  Select-Object -ExpandProperty Name
Compare-Object $expected $actual
Get-ChildItem $runDir.FullName -Filter 'canvas_frame_*.jpeg' |
  Where-Object Length -lt 1024
```

Expected: both commands produce no mismatches or undersized files.

- [ ] **Step 4: Report confirmed and unconfirmed navigation counts**

Run:

```powershell
$manifest.frames |
  Group-Object change_confirmed |
  Select-Object Name,Count
```

Expected: counts total 278. Unconfirmed frames are allowed by the selected
policy and remain saved.

- [ ] **Step 5: Final verification**

Run once more:

```powershell
D:/Anaconda/envs/codegen-marker/python.exe -m unittest `
  test.test_canvas_capture `
  test.test_meta_extract `
  test.test_agent_marker_boundaries `
  test.test_viewer_state -v
D:/Anaconda/envs/codegen-marker/python.exe -m py_compile `
  agent.py `
  skills/_shared/canvas_capture.py `
  skills/_shared/meta_extract.py `
  skills/_shared/viewer_state.py `
  out/ftimage/completed_ftimage_fixed_v4.py
```

Expected: zero test failures/errors and compilation exit code 0.
