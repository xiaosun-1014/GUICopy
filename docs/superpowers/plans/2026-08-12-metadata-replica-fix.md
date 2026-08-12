# Metadata Replica Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make complete captured metadata panels scrollable in offline replicas and correct their nested-frame, popup, readiness, and close-navigation behavior.

**Architecture:** Preserve metadata markup in the existing `InteractionRegion.root` snapshot and add a metadata-only renderer. Resolve panel candidates from the action locator's owning document, wait for panel stability before after-capture, and derive close-button placement from the active page and the presence of metadata evidence.

**Tech Stack:** Python 3, Playwright sync API, dataclasses, unittest, generated HTML/JavaScript.

---

### Task 1: Render Complete Metadata Regions

**Files:**
- Modify: `test/test_build_replica.py`
- Modify: `build_replica.py:68-128`

- [x] **Step 1: Write the failing renderer test**

Create a `DomNodeSnapshot` for a short scroll container containing a final row below its visible height, attach it as an `InteractionRegion("r_meta", "metadata", ...)`, build the replica, and assert the generated state HTML contains `data-replica-panel-region="r_meta"`, the final row, and `overflow-y:auto`.

- [x] **Step 2: Run the renderer test and verify RED**

Run:

```powershell
D:/Anaconda/envs/codegen-marker/python.exe -m unittest test.test_build_replica.BuildReplicaTests.test_metadata_region_renders_complete_scrollable_root -v
```

Expected: FAIL because `_render_document` only emits `region.members` and metadata has none.

- [x] **Step 3: Implement metadata-only rendering**

After ordinary region members are rendered, iterate metadata regions, deduplicate by region id, and append the sanitized `region.root.outer_html` in a positioned wrapper with `overflow-y:auto`.

- [x] **Step 4: Run the renderer test and verify GREEN**

Run the command from Step 2. Expected: PASS.

### Task 2: Capture a Sibling Panel Inside the Target Frame

**Files:**
- Modify: `test/test_batch_capture_replicate.py`
- Modify: `batch_capture_replicate.py:129-150`

- [x] **Step 1: Write the failing nested-frame capture test**

Create a page with an iframe containing a tags trigger and sibling `#tagsBox`, call `LiveCaptureSession._capture` using the iframe locator, load `topology.json`, and assert the child document's metadata region root contains `id="tagsBox"`.

- [x] **Step 2: Run the capture test and verify RED**

Run:

```powershell
D:/Anaconda/envs/codegen-marker/python.exe -m unittest test.test_batch_capture_replicate.BatchCaptureReplicateTests.test_metadata_panel_is_captured_from_target_frame -v
```

Expected: FAIL because the nested-document branch captures the trigger parent rather than searching the owning document.

- [x] **Step 3: Route nested metadata through the specialized capture path**

For metadata, construct an owning-document scope with `target_locator.locator("xpath=/html")` and call `capture_marker_interaction_region(scope, marker_label, target_document.document_id, target_locator)`. Preserve existing behavior for other nested marker types.

- [x] **Step 4: Run the capture test and verify GREEN**

Run the command from Step 2. Expected: PASS.

### Task 3: Wait for Metadata Content Stability

**Files:**
- Modify: `test/test_batch_capture_replicate.py`
- Modify: `batch_capture_replicate.py:41-99`

- [x] **Step 1: Write the failing asynchronous-panel test**

Use a panel that becomes visible and receives a late metadata row through `setTimeout`. Invoke `ensure_post_action_state` for `Meta 信息工具`, then assert the late row is present when the function returns.

- [x] **Step 2: Run the readiness test and verify RED**

Run:

```powershell
D:/Anaconda/envs/codegen-marker/python.exe -m unittest test.test_batch_capture_replicate.BatchCaptureReplicateTests.test_metadata_post_action_waits_for_stable_panel_content -v
```

Expected: FAIL because metadata currently returns immediately.

- [x] **Step 3: Implement bounded condition polling**

For metadata, derive the target document root with `locator_factory().locator("xpath=/html")`, find the first visible metadata candidate, and poll a signature of text content plus `scrollHeight`. Return after the same signature is observed for `stable_s`; return `False` on timeout. Do not retry-click metadata.

- [x] **Step 4: Run the readiness test and verify GREEN**

Run the command from Step 2. Expected: PASS.

### Task 4: Scope Close Navigation to Active Metadata States

**Files:**
- Modify: `test/test_build_replica.py`
- Modify: `build_replica.py:228-277`

- [x] **Step 1: Write failing state-placement tests**

Add one test whose non-entry state has no metadata region and assert no close button is rendered. Add a popup-state test with `active_page_var="page1"`, metadata on the popup entry document, and assert the close button appears only in the popup document and targets the preceding state.

- [x] **Step 2: Run the state-placement tests and verify RED**

Run:

```powershell
D:/Anaconda/envs/codegen-marker/python.exe -m unittest test.test_build_replica.BuildReplicaTests.test_non_metadata_state_has_no_close_button test.test_build_replica.BuildReplicaTests.test_metadata_popup_places_close_on_active_popup -v
```

Expected: FAIL because every non-entry state's main document currently gets the button.

- [x] **Step 3: Implement active metadata placement**

Select the active page using `state.active_page_var`, resolve its entry document, and compute `has_metadata` from that document's regions. Pass `back_target` only when the current document is the active entry document and `has_metadata` is true. Preserve the preceding-state relative URL calculation.

- [x] **Step 4: Run the state-placement tests and verify GREEN**

Run the command from Step 2. Expected: PASS.

### Task 5: Regression Verification

**Files:**
- Verify: `capture_snapshot.py`
- Verify: `replica_models.py`
- Verify: `build_replica.py`
- Verify: `batch_capture_replicate.py`
- Verify: `test/test_capture_snapshot.py`
- Verify: `test/test_build_replica.py`
- Verify: `test/test_batch_capture_replicate.py`
- Verify: `test/test_replica_runtime.py`

- [x] **Step 1: Run focused metadata tests**

```powershell
D:/Anaconda/envs/codegen-marker/python.exe -m unittest test.test_capture_snapshot test.test_build_replica test.test_batch_capture_replicate test.test_replica_runtime -v
```

Expected: all tests PASS. If the combined command exceeds the runner timeout, run each module separately and report each result.

- [x] **Step 2: Run syntax and whitespace checks**

```powershell
D:/Anaconda/envs/codegen-marker/python.exe -m py_compile capture_snapshot.py replica_models.py build_replica.py batch_capture_replicate.py
git diff --check
```

Expected: exit code 0 with no syntax errors or whitespace errors.

- [x] **Step 3: Review the final diff**

Confirm every production change traces to metadata rendering, target-frame capture, readiness, or close navigation, and confirm pre-existing user edits remain present.
