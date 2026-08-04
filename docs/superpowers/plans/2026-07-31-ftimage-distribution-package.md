# FTImage Distribution Package Implementation Plan

**Goal:** Create a self-contained folder that a colleague can set up once and run without the source project.

**Architecture:** Preserve the generated adapter's expected `ftimage/` and project-root-relative shared-module layout inside a new distribution folder. Provide batch scripts that create a local virtual environment, install Playwright, install Chromium, and run the adapter. Keep all runtime outputs under the packaged `ftimage/` directory.

**Tech Stack:** Python 3.10+, Playwright sync API, Windows batch files, standard-library filesystem operations.

### Task 1: Assemble the distribution tree

**Files:**
- Create: `out/ftimage_runner/ftimage/completed_ftimage_fixed_v4.py`
- Create: `out/ftimage_runner/skills/_shared/{__init__.py,canvas_capture.py,meta_extract.py,viewer_state.py}`

- [ ] Copy only the generated adapter and runtime shared modules into the new tree.
- [ ] Verify the adapter's relative project-root import resolves to `out/ftimage_runner/skills`.

### Task 2: Add setup and run entry points

**Files:**
- Create: `out/ftimage_runner/requirements.txt`
- Create: `out/ftimage_runner/setup.bat`
- Create: `out/ftimage_runner/run.bat`
- Create: `out/ftimage_runner/README.md`

- [ ] `setup.bat` creates `.venv`, installs `playwright>=1.40`, and installs Chromium.
- [ ] `run.bat` checks for `.venv`, then runs the adapter from the distribution root.
- [ ] README documents first-run setup, subsequent run, network/URL requirements, and output tree.

### Task 3: Verify the package

- [ ] Run `setup.bat` or equivalent commands in the package directory.
- [ ] Compile the packaged adapter and shared modules.
- [ ] Run the packaged adapter once against the recorded FTImage URL.
- [ ] Confirm the package creates `report.jpeg`, metadata outputs, and an isolated `canvas_frames/<run-id>` directory.
