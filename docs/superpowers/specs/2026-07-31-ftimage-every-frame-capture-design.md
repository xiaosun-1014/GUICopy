# FTImage Every-Frame Canvas Capture Design

## Goal

For the selected diagnostic series, write exactly one JPEG file for every
requested frame index. A series with `N` frames must produce `N` numbered image
files, even when the viewer cannot confirm that a navigation action changed the
rendered image.

## Current Problem

The generated canvas implementation treats matching JPEG file sizes as proof
that two frames are duplicates. It deletes the later file and omits it from the
result. In the verified FTImage run, the script visited 278 frame indexes but
kept only 243 files.

Equal file size does not prove equal image content. More importantly, deleting
any requested index conflicts with the required one-file-per-frame contract.
The current relative `canvas_frames` path can also place output in the process
working directory instead of beside the generated adapter.

## Selected Architecture

Canvas capture becomes deterministic shared runtime behavior:

- Add `skills/_shared/canvas_capture.py` as the single implementation used by
  generated adapters.
- Make `agent.py` generate a fixed call to that shared module for the
  `影像画布交互` marker, as it already does for deterministic Meta extraction.
- Keep `skills/marker-canvas-capture/SKILL.md` as documentation for the marker
  contract, but do not ask the LLM to generate the capture pipeline.
- Do not hand-edit `completed_ftimage*.py`; regenerate it from
  `processed_script_ftimage.py`.

## Capture Contract

Given `total_frames=N`:

1. Create a unique run directory under
   `SCRIPT_DIR / "canvas_frames" / "YYYYMMDD_HHMMSS_ffffff"`.
2. Iterate requested indexes from 1 through `N`.
3. Attempt to navigate to each index and wait for rendering.
4. Save `canvas_frame_XXXX.jpeg` for every index.
5. Never delete or skip a file because its size or render hash matches another
   frame.
6. Assert that the run directory contains exactly `N` numbered JPEG files.
7. Return all `N` paths in frame-index order.

The run directory timestamp always includes microsecond precision so two runs
started in the same second do not collide. Existing run directories are never
deleted or overwritten.

## Navigation and Rendering

The shared runtime retains the existing viewer-independent fallbacks:

1. Viewer/cornerstone JavaScript API.
2. Focused canvas keyboard navigation.
3. Mouse wheel over the canvas.
4. Slider navigation when available.

The canvas thumbnail hash remains a best-effort rendering readiness signal. It
records whether a change was confirmed, but it never controls whether the
requested frame is saved.

For frame 1, the current image is captured without navigation. For frames
2 through `N`, navigation failure or an unchanged hash produces a warning and a
manifest flag, then capture continues.

## Screenshot Fallbacks

Each requested frame uses these methods in order:

1. Copy the largest visible canvas to a 2D scratch canvas and encode JPEG at
   quality 0.95.
2. Use Playwright `canvas.screenshot()` as JPEG.
3. Use a current-page JPEG screenshot as the last resort.

If a higher-priority method fails, the next method must write the same target
filename. The third method fulfills the user's selected policy of producing
`N` files even when an individual canvas capture cannot be obtained.

## Manifest

Each run directory contains `capture_manifest.json` with:

- selected series name, when available;
- requested frame count;
- saved frame count;
- run start and completion timestamps;
- one entry per frame containing the requested index, relative image filename,
  capture method, navigation method, whether frame change was confirmed, file
  size, and warning text.

The manifest is diagnostic only. A possible duplicate remains a valid saved
frame under the selected policy.

## Generated Adapter Interface

The generated marker block imports the shared entry point and calls it with:

- the detected Playwright page (`page`, `page1`, or another recorded page
  variable);
- `total_frames=locals().get("seq_frames")`;
- `series_name=locals().get("seq_name")`;
- `output_root=SCRIPT_DIR / "canvas_frames"`;
- the recorded canvas click coordinates when available.

If `seq_frames` is unavailable, the runtime parses the viewer's displayed frame
counter. If neither source yields a positive count, it captures one frame and
records the fallback in the manifest.

## Error Handling

- Failure to confirm navigation: warn, mark the manifest entry, and save.
- Canvas JavaScript capture failure: fall back to locator screenshot.
- Locator screenshot failure: fall back to page screenshot.
- Failure of all three screenshot methods: raise immediately because the
  required `N`-file contract cannot be fulfilled.
- Final file count mismatch: raise with the expected count, actual count, and
  run directory.

## Testing

- Unit test that equal-sized frame files are all retained.
- Unit test that `N` requested indexes produce `N` ordered paths and manifest
  entries.
- Unit test that an unconfirmed frame change still saves the requested index
  and records a warning.
- Unit test that output is rooted under `SCRIPT_DIR` and each run gets an
  isolated directory.
- Unit test that the page screenshot fallback preserves the one-file-per-index
  contract.
- Generator test that canvas markers use the deterministic shared runtime and
  do not call the LLM.
- Syntax-check the regenerated FTImage adapter.
- Real FTImage verification: select the 278-frame series and confirm 278
  numbered JPEG files plus a 278-entry manifest.

## Non-Goals

- Detecting whether two medical images are semantically identical.
- Removing repeated frames.
- Running a VL model on every captured image.
- Deleting historical capture runs.
