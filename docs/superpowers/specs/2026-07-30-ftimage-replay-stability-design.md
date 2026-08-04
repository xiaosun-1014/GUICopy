# FTImage Replay Stability Design

## Goal

Make FTImage recordings replay through series selection, Metadata open/close, and WL/WW adjustment without manual script edits. Pre-fill the GUI with the supplied FTImage URL and its standard output path.

## Root Causes

- The report marker is a comment, so replay selects a series before the asynchronous report request has finished. The late report response reopens `#reportContainer` over the toolbar.
- FTImage contains two `a.tool.tool-tags` elements. The unscoped locator violates Playwright strict mode.
- The Metadata close action is `#tagsBox a.close`, not a second Tags click.
- The GUI still uses legacy Uicloud defaults.

## Design

- Before a recorded series-selection action, wait for the FTImage report footer to be attached when `#reportContainer` exists.
- After series selection, wait for the report overlay to be hidden and the More tool to be enabled.
- Normalize the recorded Tags open action to `#moreBox a.tool.tool-tags`.
- Normalize the anonymous Metadata close action to `#tagsBox a.close`.
- Use the supplied FTImage link and `out/ftimage/processed_script_ftimage.py` as GUI defaults.

## Verification

- Unit tests cover both FTImage selector rewrites and GUI defaults.
- A Playwright test covers report readiness before series selection.
- Existing capture, parser, replay, GUI, and end-to-end tests remain green.
- A real-page diagnostic confirms the Metadata panel opens through `#moreBox` and exposes `#tagsBox a.close`.
