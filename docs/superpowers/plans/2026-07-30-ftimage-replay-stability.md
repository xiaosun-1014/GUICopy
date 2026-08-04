# FTImage Replay Stability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stabilize FTImage replay through Metadata and WL/WW while pre-filling the GUI with FTImage inputs.

**Architecture:** Keep hospital-specific recording cleanup in `normalize_ftimage_codegen` and semantic replay waits in `batch_capture_replicate`. Tests exercise public helpers and real Playwright state transitions without changing the replica manifest format.

**Tech Stack:** Python 3.11, Playwright sync API, PyQt6, unittest.

---

### Task 1: Correct FTImage Selector Normalization

**Files:**
- Modify: `main_gui.py`
- Test: `test/test_replica_gui.py`

- [ ] Add a failing test requiring `#moreBox a.tool.tool-tags` for opening and `#tagsBox a.close` for closing.
- [ ] Run the focused GUI normalization test and confirm it fails.
- [ ] Update `normalize_ftimage_codegen` with the two unique selectors.
- [ ] Run the focused test and confirm it passes.

### Task 2: Stabilize Series Transition Timing

**Files:**
- Modify: `batch_capture_replicate.py`
- Test: `test/test_batch_capture_replicate.py`

- [ ] Add a failing Playwright test where report content attaches asynchronously before series selection.
- [ ] Run the focused timing test and confirm the readiness helper is missing.
- [ ] Add a pre-action readiness wait and call it from `LiveCaptureSession.before`.
- [ ] Extend the post-action wait to require the More tool to be enabled.
- [ ] Run focused capture tests and confirm they pass.

### Task 3: Set FTImage GUI Defaults

**Files:**
- Modify: `main_gui.py`
- Test: `test/test_replica_gui.py`

- [ ] Add a failing test for the supplied FTImage URL and standard output path.
- [ ] Run the focused GUI test and confirm it fails.
- [ ] Replace legacy defaults with FTImage constants.
- [ ] Run the focused GUI tests and confirm they pass.

### Task 4: Update Current Recording and Verify

**Files:**
- Modify: `out/ftimage/processed_script_ftimage_v3.py`

- [ ] Replace the ambiguous Tags actions in the current recording.
- [ ] Run parser, capture, replay, GUI, and end-to-end tests.
- [ ] Run the complete unittest suite.
- [ ] Re-export V3 in the restarted GUI for real-environment confirmation.
