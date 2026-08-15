"""Smoke test for test/fixtures/multi_series/series_list.html.

Opens the fixture with Playwright chromium and runs the discovery algorithm from
capture_snapshot.discover_series_candidates, asserting:
  - >= 3 series descriptors are enumerated
  - the two same-name "Coronal MIP" series get DIFFERENT series_keys (uid-1992 / uid-2047)
  - the scroll-revealed Sagittal series (uid-5209) is also enumerated

Run with:
  D:/Anaconda/envs/codegen-marker/python.exe test/fixtures/multi_series/_smoke.py
"""
from pathlib import Path

FIXTURE_DIR = Path(__file__).resolve().parent

# Keep this script runnable from both the repo root and this directory.
import sys

_REPO_ROOT = FIXTURE_DIR.parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from playwright.sync_api import sync_playwright  # noqa: E402
from capture_snapshot import discover_series_candidates  # noqa: E402


def main() -> int:
    html = (FIXTURE_DIR / "series_list.html").as_uri()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 800, "height": 600})
        page.goto(html)
        descriptors, members, evidence = discover_series_candidates(
            page.locator("#series-list"), "msl", max_scroll_steps=20, max_duration_s=5.0
        )
        browser.close()

    print("descriptors:", len(descriptors))
    for d in descriptors:
        print(
            f"  key={d.series_key!r} ordinal={d.ordinal} label={d.label!r} "
            f"frames(text)={d.inferred_frame_count} selected={d.selected}"
        )
    print("evidence:", evidence)

    assert len(descriptors) >= 3, f"expected >=3 series, got {len(descriptors)}"
    keys = [d.series_key for d in descriptors]
    assert len(keys) == len(set(keys)), f"duplicate series_keys: {keys}"

    # Same-name grouping: label is "Coronal MIP\n1.0 362幅" (name + frame text), so
    # group by the "Coronal MIP" prefix (before the newline).
    coronal = [d for d in descriptors if d.label.split("\n")[0].strip().lower() == "coronal mip"]
    assert len(coronal) == 2, f"expected 2 same-name Coronal MIP, got {len(coronal)}"
    ckeys = {d.series_key for d in coronal}
    assert ckeys == {"uid-1992", "uid-2047"}, f"same-name keys wrong: {ckeys}"

    assert "uid-5209" in keys, "scroll-revealed Sagittal (uid-5209) not enumerated"
    print("\nSMOKE OK: 5 series enumerated, same-name Coronal MIP distinct keys, "
          "scroll-revealed uid-5209 present.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
