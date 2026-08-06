"""Shared locator-risk classification for GUI, build, and validation."""

from __future__ import annotations

import re

from replica_models import ActionTarget


LOCATOR_RISK_ORDER = {
    "stable_id": 1,
    "aria": 2,
    "stable_attribute": 3,
    "text": 4,
    "ordinal": 5,
    "structural": 6,
    "coordinate": 7,
}

HIGH_RISK_LOCATORS = frozenset({"ordinal", "structural", "coordinate"})

_EXACT_ID = re.compile(
    r"""^(?:#[A-Za-z_][\w:.-]*|\[id\s*=\s*(['"])[^'"]+\1\])$"""
)
_STABLE_ATTRIBUTE = re.compile(
    r"""^\[(?:data-testid|data-test|data-qa|name|aria-label)\s*=\s*(['"])[^'"]+\1\]$""",
    re.IGNORECASE,
)
_STRUCTURAL = re.compile(
    r"(?:\s[>+~]?\s|[>+~]|:nth-|:first-|:last-|:has\(|,|\[(?!"
    r"(?:id|data-testid|data-test|data-qa|name|aria-label)\s*=))",
    re.IGNORECASE,
)


def classify_locator_risk(target: ActionTarget) -> str:
    """Return the one canonical static risk bucket for an action target."""
    locator = target.locator
    if locator is None:
        if target.action_source_kind == "mouse_xy" or target.point is not None:
            return "coordinate"
        return "non_locator"
    if locator.ordinal_op:
        return "ordinal"
    if locator.locator_kind in {"role", "label", "title"}:
        return "aria"
    if locator.locator_kind == "test_id":
        return "stable_attribute"
    if locator.locator_kind == "text":
        return "text"
    arguments = locator.locator_args.get("args", [])
    selector = str(arguments[0]) if arguments else ""
    if _EXACT_ID.fullmatch(selector):
        return "stable_id"
    if _STABLE_ATTRIBUTE.fullmatch(selector):
        return "stable_attribute"
    if _STRUCTURAL.search(selector):
        return "structural"
    # Preserve the existing validator's direct/simple-CSS bucket name. The
    # name is historical; only complex/positional CSS is structural.
    return "stable_id"
