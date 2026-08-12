# Metadata Replica Fix Design

## Goal

Make a captured metadata panel usable in the offline replica, including content below the original viewport, while preserving nested-frame and popup behavior.

## Scope

The change is limited to metadata-region capture and rendering, metadata post-action readiness, and the close/back control associated with metadata states. It does not change marker completion, DICOM JSON extraction, unrelated replica regions, or the manifest schema.

## Design

### Capture

- Keep the complete sanitized panel markup in `InteractionRegion.root.outer_html`; do not restore the redundant `full_html` field.
- Resolve metadata candidates inside the locator's owning Playwright frame. This handles panels that are siblings of the trigger inside nested viewer iframes.
- Fall back to the existing generic interaction-region capture when no real panel candidate is visible.
- After a metadata action, poll until a candidate panel is visible and its text/scroll height remains unchanged for consecutive samples. Stop at a bounded timeout so unsupported viewers continue through the existing fallback path.

### Rendering

- Render metadata `region.root.outer_html` once as a positioned, internally scrollable overlay.
- Use the captured root rectangle and an explicit `overflow-y: auto`; retain the screenshot as the visual background.
- Continue rendering ordinary regions from `region.members`. Metadata receives a dedicated path because its member list is intentionally empty.

### Navigation

- Add the close/back control only when the rendered state contains a metadata region.
- Determine the active page from `ReplicaState.active_page_var`, then attach the control only to that page's entry document.
- Navigate to the immediately preceding captured state using the existing relative URL mechanism.
- Popup states therefore receive the control in the popup document rather than the background main page.

## Error Handling

- Missing, hidden, document-level, or implausibly oversized candidates fall back to generic region capture.
- Metadata readiness is timeout-bounded and must not introduce an unbounded replay wait.
- Existing screenshot-only rendering remains available if no usable metadata region is captured.

## Tests

- A build test proves metadata root HTML is emitted, scrollable, and exposes content below the captured panel height.
- A capture test proves a sibling metadata panel is found through the trigger's owning nested frame.
- A state-build test proves only metadata states receive a close control.
- A popup test proves the close control is placed in the active popup entry document and returns to the preceding state.
- Existing capture, build, runtime, and batch-capture tests remain green.

## Success Criteria

The offline replica can open a metadata state, scroll through the complete captured panel, and close back to the preceding state in both same-page and popup viewer layouts, without adding close controls to unrelated states.
