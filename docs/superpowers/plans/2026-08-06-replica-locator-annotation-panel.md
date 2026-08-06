# Replica Locator Annotation Panel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a developer-facing GUI panel that lists marker actions, previews and edits Playwright locator receivers, safely writes validated changes back to the processed script, and reports one consistent locator-risk classification across the pipeline.

**Architecture:** Keep the processed Python script as the only source of truth. Add a shared pure risk classifier, extend `parse_action_plan()` with non-persisted receiver source spans, expose pure locator parse/replace transactions, then place a standalone PyQt6 panel beside the existing editor and connect it through a single source-state synchronization path.

**Tech Stack:** Python 3.11（项目统一运行时；`dataclass`/`|` 联合语法 3.10 可用，但
本仓库强制 3.11，见 CLAUDE.md）, standard-library `ast`/`dataclasses`/`re`, PyQt6,
Playwright locator models, `unittest`

---

## Scope and working-tree rules

The approved design is:

`docs/superpowers/specs/2026-08-06-replica-locator-annotation-panel-design.md`

That specification currently contains user-authored, uncommitted edits. Do not stage or
rewrite it while executing this plan. Every commit command below names exact
implementation and test files; never use `git add .`.

This plan deliberately excludes:

- browser element picking or highlighting;
- online `count()` or visibility checks;
- coordinate-to-locator conversion;
- action-method or action-argument editing;
- sidecar locator overrides.

## File map

### New files

- `locator_risk.py` — the only locator-risk ordering and classification implementation.
- `replica_annotation_panel.py` — standalone Qt widget; it renders an `ActionPlan` and emits source-jump/apply requests.
- `test/test_locator_risk.py` — exhaustive shared risk-classification tests.
- `test/test_replica_annotation_panel.py` — component tests for the new Qt panel.

### Modified files

- `rewrite_script.py` — `SourceSpan`, span-to-character conversion, strict locator expression parsing, atomic locator replacement, and shared risk report.
- `build_replica.py` — consume the shared risk classifier when writing `locator_mapping.json`.
- `pipeline_validation.py` — consume shared risk ordering/classifier and retain existing partial-status policy.
- `main_gui.py` — source-state reconstruction, splitter integration, debounced plan refresh, source jump, and atomic apply wiring.
- `test/test_replica_action_parser.py` — parser spans, UTF-8 offsets, expression validation, replacement transaction, popup/iframe regression.
- `test/test_build_replica.py` — expected migrated risk bucket in `locator_mapping.json`.
- `test/test_pipeline_validation.py` — shared bucket names, highest-risk behavior, and unchanged partial policy.
- `test/test_replica_gui.py` — source-model synchronization and integrated panel lifecycle.
- `docs/MANUAL_ANNOTATION_REPLICA_SOP.md` — replace the “edit script only” limitation with the new stopped-recording panel workflow and document the risk-bucket migration.

---

### Task 1: Centralize locator-risk classification

**Files:**
- Create: `locator_risk.py`
- Create: `test/test_locator_risk.py`
- Modify: `rewrite_script.py:654-670`
- Modify: `build_replica.py:143-171`
- Modify: `pipeline_validation.py:161-229`
- Modify: `test/test_build_replica.py:26-53`
- Modify: `test/test_pipeline_validation.py:141-174`

- [ ] **Step 1: Write exhaustive failing classifier tests**

Create `test/test_locator_risk.py`:

```python
import unittest

from locator_risk import LOCATOR_RISK_ORDER, classify_locator_risk
from replica_models import ActionTarget, LocatorRecipe, Point


def locator_target(
    kind: str,
    argument: str,
    *,
    expression: str | None = None,
    ordinal_op: str | None = None,
) -> ActionTarget:
    source = expression or f"page.locator({argument!r})"
    locator = LocatorRecipe(
        source_expression=source,
        page_var="page",
        frame_chain=[],
        locator_kind=kind,
        locator_args={"args": [argument]},
        ordinal_op=ordinal_op,
        ordinal_value=2 if ordinal_op == "nth" else None,
    )
    return ActionTarget(
        "a_000_001", "marker-1", "click", "locator", {},
        locator, None, None, None, None, "execute", None, "d_main", None,
    )


class LocatorRiskTests(unittest.TestCase):
    def test_risk_order_has_every_persisted_bucket(self):
        self.assertEqual(
            list(LOCATOR_RISK_ORDER),
            [
                "stable_id",
                "aria",
                "stable_attribute",
                "text",
                "ordinal",
                "structural",
                "coordinate",
            ],
        )

    def test_semantic_locator_kinds_have_distinct_buckets(self):
        cases = [
            (locator_target("role", "button"), "aria"),
            (locator_target("label", "Patient ID"), "aria"),
            (locator_target("title", "More"), "aria"),
            (locator_target("test_id", "open-viewer"), "stable_attribute"),
            (locator_target("text", "Body 1.0"), "text"),
        ]
        for target, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(classify_locator_risk(target), expected)

    def test_css_rules_distinguish_direct_stable_and_structural_selectors(self):
        cases = [
            ("#report", "stable_id"),
            ('[id="report"]', "stable_id"),
            ('[data-testid="report"]', "stable_attribute"),
            ('[name="accession"]', "stable_attribute"),
            (".toolbar > button:nth-child(2)", "structural"),
            ('div[class="toolbar"] button', "structural"),
        ]
        for selector, expected in cases:
            with self.subTest(selector=selector):
                self.assertEqual(classify_locator_risk(locator_target("css", selector)), expected)

    def test_ordinal_coordinate_and_non_locator_are_distinct(self):
        ordinal = locator_target(
            "css",
            ".series",
            expression='page.locator(".series").nth(2)',
            ordinal_op="nth",
        )
        coordinate = ActionTarget(
            "a_mouse", "marker-1", "click", "mouse_xy", {"args": [10, 20]},
            None, None, None, Point(10, 20, "page_viewport_css"), None,
            "execute", None, "d_main", None,
        )
        keyboard = ActionTarget(
            "a_key", "marker-1", "press", "keyboard", {},
            None, None, None, None, "ArrowDown",
            "execute", None, "d_main", None,
        )
        self.assertEqual(classify_locator_risk(ordinal), "ordinal")
        self.assertEqual(classify_locator_risk(coordinate), "coordinate")
        self.assertEqual(classify_locator_risk(keyboard), "non_locator")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the new test and verify the module is missing**

Run:

```powershell
D:/Anaconda/envs/codegen-marker/python.exe -m unittest test.test_locator_risk -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'locator_risk'`.

- [ ] **Step 3: Implement the shared classifier**

Create `locator_risk.py`:

```python
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
```

- [ ] **Step 4: Run classifier tests and correct only rule-level failures**

Run:

```powershell
D:/Anaconda/envs/codegen-marker/python.exe -m unittest test.test_locator_risk -v
```

Expected: 4 tests PASS.

- [ ] **Step 5: Replace all three duplicate classifiers**

In `rewrite_script.py`, import `classify_locator_risk` and replace
`locator_risk_report()` with:

```python
def locator_risk_report(plan: ActionPlan) -> dict[str, int]:
    """Count parsed actions using the shared persisted risk buckets."""
    counts = {
        "stable_id": 0,
        "aria": 0,
        "stable_attribute": 0,
        "text": 0,
        "ordinal": 0,
        "structural": 0,
        "coordinate": 0,
        "non_locator": 0,
    }
    for group in plan.marker_groups:
        for action in group.actions:
            counts[classify_locator_risk(action)] += 1
    return counts
```

In `build_replica.py`, import `classify_locator_risk`, delete its local
`_locator_risk()`, and change `_locator_risk_metadata()` to set:

```python
"locator_risk": classify_locator_risk(target),
```

In `pipeline_validation.py`, import:

```python
from locator_risk import LOCATOR_RISK_ORDER, classify_locator_risk
```

Delete `_LOCATOR_RISK_ORDER` and the local `_locator_risk()`. In
`validate_locator_risk()`, replace its classification call and ordering lookup:

```python
risk = classify_locator_risk(target)
```

```python
key=lambda risk: LOCATOR_RISK_ORDER.get(risk, 0),
```

Keep `non_locator` excluded from `highest_risk`.

- [ ] **Step 6: Update migration expectations and add cross-consumer assertions**

In `test/test_build_replica.py`, change the expected mapping bucket:

```python
self.assertEqual(
    locator_mapping["a_000_001"]["locator_risk"],
    "stable_id",
)
```

In `test/test_pipeline_validation.py`, extend `LocatorRiskTests`:

```python
def test_shared_text_and_test_id_buckets_reach_validation_metrics(self):
    text_target = ActionTarget(
        "a_text", "m_0", "click", "locator", {"args": []},
        _ascii_locator(
            'page.get_by_text("Body")',
            locator_kind="text",
            args="Body",
        ),
        None, None, None, None, "execute", None, "d_main", None,
    )
    test_id_target = ActionTarget(
        "a_test_id", "m_0", "click", "locator", {"args": []},
        _ascii_locator(
            'page.get_by_test_id("open")',
            locator_kind="test_id",
            args="open",
        ),
        None, None, None, None, "execute", None, "d_main", None,
    )
    result = validate_locator_risk(
        _base_flow(states=[_entry_state(targets=[text_target, test_id_target])])
    )
    self.assertEqual(result.metrics["risk_counts"]["text"], 1)
    self.assertEqual(result.metrics["risk_counts"]["stable_attribute"], 1)
    self.assertEqual(result.metrics["highest_risk"], "text")
```

- [ ] **Step 7: Run the focused migration suite**

Run:

```powershell
D:/Anaconda/envs/codegen-marker/python.exe -m unittest `
  test.test_locator_risk `
  test.test_replica_action_parser `
  test.test_build_replica `
  test.test_pipeline_validation -v
```

Expected: all tests PASS; no expected output refers to the old `simple` bucket.

- [ ] **Step 8: Commit the shared classifier**

```powershell
git add -- locator_risk.py rewrite_script.py build_replica.py pipeline_validation.py test/test_locator_risk.py test/test_replica_action_parser.py test/test_build_replica.py test/test_pipeline_validation.py
git commit -m "refactor: unify replica locator risk classification"
```

---

### Task 2: Capture exact locator receiver source spans

**Files:**
- Modify: `rewrite_script.py:20-34,179-237`
- Modify: `test/test_replica_action_parser.py`

- [ ] **Step 1: Write failing source-span and UTF-8 tests**

Add imports and tests to `test/test_replica_action_parser.py`:

```python
from rewrite_script import (
    parse_action_plan,
    source_span_offsets,
)
```

```python
def test_locator_source_span_covers_only_receiver(self):
    source = '''# [MARKER: Meta 信息工具]
page.locator("#iframe").content_frame.locator(
    "#confirm"
).click(position={"x": 1, "y": 2})
'''
    plan = parse_action_plan(source)
    action = plan.marker_groups[0].actions[0]
    start, end = source_span_offsets(
        source,
        plan.locator_source_spans[action.action_id],
    )

    self.assertEqual(
        source[start:end],
        'page.locator("#iframe").content_frame.locator(\n'
        '    "#confirm"\n'
        ")",
    )
    self.assertEqual(action.action_type, "click")
    self.assertEqual(action.action_args["position"], {"x": 1, "y": 2})

def test_locator_source_span_handles_utf8_before_end_column(self):
    source = '''# [MARKER: 序列选择]
page.get_by_role("button", name="确定").click()
'''
    plan = parse_action_plan(source)
    action = plan.marker_groups[0].actions[0]
    start, end = source_span_offsets(
        source,
        plan.locator_source_spans[action.action_id],
    )

    self.assertEqual(
        source[start:end],
        'page.get_by_role("button", name="确定")',
    )
```

- [ ] **Step 2: Run the two tests and verify the API is missing**

Run:

```powershell
D:/Anaconda/envs/codegen-marker/python.exe -m unittest `
  test.test_replica_action_parser.ReplicaActionParserTests.test_locator_source_span_covers_only_receiver `
  test.test_replica_action_parser.ReplicaActionParserTests.test_locator_source_span_handles_utf8_before_end_column -v
```

Expected: FAIL because `source_span_offsets` and
`ActionPlan.locator_source_spans` do not exist.

- [ ] **Step 3: Add SourceSpan and UTF-8-safe offset conversion**

In `rewrite_script.py`, add:

```python
@dataclass(frozen=True)
class SourceSpan:
    start_line: int
    start_col: int
    end_line: int
    end_col: int


def _utf8_col_to_character_col(line: str, byte_col: int) -> int:
    prefix = line.encode("utf-8")[:byte_col]
    try:
        return len(prefix.decode("utf-8"))
    except UnicodeDecodeError as error:
        raise ValueError("AST column ends inside a UTF-8 character") from error


def source_span_offsets(source: str, span: SourceSpan) -> tuple[int, int]:
    """Convert an AST byte-column span to absolute Python string offsets."""
    lines = source.splitlines(keepends=True)
    if not (1 <= span.start_line <= span.end_line <= len(lines)):
        raise ValueError("source span line is outside the script")
    start_line = lines[span.start_line - 1]
    end_line = lines[span.end_line - 1]
    start = sum(len(line) for line in lines[: span.start_line - 1])
    start += _utf8_col_to_character_col(start_line, span.start_col)
    end = sum(len(line) for line in lines[: span.end_line - 1])
    end += _utf8_col_to_character_col(end_line, span.end_col)
    return start, end
```

Extend `ActionPlan` without changing persisted replica models:

```python
@dataclass
class ActionPlan:
    bootstrap: BootstrapPlan
    marker_groups: list[MarkerGroup]
    popup_expectations: list[PopupExpectation]
    instrumented_source: str
    locator_source_spans: dict[str, SourceSpan] = field(default_factory=dict)
```

- [ ] **Step 4: Record receiver spans during parsing**

In `parse_action_plan()`, initialize:

```python
locator_source_spans: dict[str, SourceSpan] = {}
```

Immediately after a locator action is accepted, record `call.func.value`:

```python
if action.locator is not None:
    receiver = call.func.value
    locator_source_spans[action.action_id] = SourceSpan(
        receiver.lineno,
        receiver.col_offset,
        receiver.end_lineno,
        receiver.end_col_offset,
    )
```

Return:

```python
return ActionPlan(
    bootstrap,
    groups,
    expectations,
    source,
    locator_source_spans,
)
```

- [ ] **Step 5: Run parser tests**

Run:

```powershell
D:/Anaconda/envs/codegen-marker/python.exe -m unittest test.test_replica_action_parser -v
```

Expected: all parser tests PASS, including both UTF-8/source-span tests.

- [ ] **Step 6: Commit source-span support**

```powershell
git add -- rewrite_script.py test/test_replica_action_parser.py
git commit -m "feat: capture locator receiver source spans"
```

---

### Task 3: Add strict locator-expression parsing

**Files:**
- Modify: `rewrite_script.py:36-80`
- Modify: `test/test_replica_action_parser.py`

- [ ] **Step 1: Write failing strict-expression tests**

Add `LocatorEditError` and `parse_locator_expression` to the test imports, then add:

```python
def test_parse_locator_expression_accepts_nested_iframe_receiver(self):
    recipe = parse_locator_expression(
        'page1.locator("#iframe").content_frame'
        '.locator(\'iframe[name="imageFrame"]\').content_frame'
        '.get_by_role("button", name="确定")'
    )

    self.assertEqual(recipe.page_var, "page1")
    self.assertEqual(
        [hop.selector for hop in recipe.frame_chain],
        ["#iframe", 'iframe[name="imageFrame"]'],
    )
    self.assertEqual(recipe.locator_kind, "role")

def test_parse_locator_expression_rejects_action_call(self):
    with self.assertRaisesRegex(LocatorEditError, "receiver"):
        parse_locator_expression('page.locator("#confirm").click()')

def test_parse_locator_expression_rejects_dynamic_selector(self):
    with self.assertRaisesRegex(LocatorEditError, "static literal"):
        parse_locator_expression("page.locator(selector)")

def test_parse_locator_expression_rejects_unknown_root(self):
    with self.assertRaisesRegex(LocatorEditError, "page variable"):
        parse_locator_expression('browser.locator("#confirm")')
```

- [ ] **Step 2: Run the strict-expression tests and verify failure**

Run:

```powershell
D:/Anaconda/envs/codegen-marker/python.exe -m unittest `
  test.test_replica_action_parser.ReplicaActionParserTests.test_parse_locator_expression_accepts_nested_iframe_receiver `
  test.test_replica_action_parser.ReplicaActionParserTests.test_parse_locator_expression_rejects_action_call `
  test.test_replica_action_parser.ReplicaActionParserTests.test_parse_locator_expression_rejects_dynamic_selector `
  test.test_replica_action_parser.ReplicaActionParserTests.test_parse_locator_expression_rejects_unknown_root -v
```

Expected: FAIL because the public parser and exception do not exist.

- [ ] **Step 3: Implement strict validation around the existing recipe parser**

In `rewrite_script.py`, add:

```python
LOCATOR_METHODS = {
    "locator",
    "get_by_role",
    "get_by_text",
    "get_by_test_id",
    "get_by_label",
    "get_by_title",
}


class LocatorEditError(ValueError):
    """A locator edit that cannot be represented safely by LocatorRecipe."""


def _attribute_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        return node.func.attr
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _validate_static_locator_calls(expression: ast.AST) -> None:
    for node in ast.walk(expression):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in LOCATOR_METHODS:
            continue
        for value in [
            *node.args,
            *(keyword.value for keyword in node.keywords if keyword.arg),
        ]:
            try:
                ast.literal_eval(value)
            except (ValueError, TypeError) as error:
                raise LocatorEditError(
                    "locator and iframe selectors must use static literal arguments"
                ) from error


def parse_locator_expression(expression: str) -> LocatorRecipe:
    """Parse one receiver expression without executing it."""
    try:
        node = ast.parse(expression, mode="eval").body
    except SyntaxError as error:
        raise LocatorEditError(f"invalid Python expression: {error.msg}") from error
    if _attribute_name(node) in ACTION_METHODS:
        raise LocatorEditError("enter only the locator receiver, without an action call")
    source = ast.unparse(node)
    root_match = re.match(r"(?P<page>page\d*)\b", source)
    if root_match is None:
        raise LocatorEditError("locator must start from a page variable")
    _validate_static_locator_calls(node)
    page_var = root_match.group("page")
    recipe = _locator_from_expression(node, page_var)
    if recipe is None:
        raise LocatorEditError("unsupported Playwright locator expression")
    return recipe
```

Replace `_locator_from_expression()`'s local locator-method set with the shared
`LOCATOR_METHODS` constant so the accepted methods cannot drift.

> **root 校验与 `frame_locator` 局限（P5）。** 上面的 `root_match` 是对
> `ast.unparse(node)` 的文本做 `re.match(r"(?P<page>page\d*)\b", source)`：对
> `page.locator(...)` 会误放行，再在 `_locator_from_expression` 返回 `None` 时报
> 「unsupported」。`frame_locator(...)`（Playwright 标准 iframe 写法）不在
> `LOCATOR_METHODS` 里，会被拒绝——这不是缺陷，因为本项目录制产物用的是
> `.locator(...).content_frame` 链（`rewrite_script.py:49`），与现有数据一致。
> 保持现状即可，但 `LocatorEditError` 文案应能提示「仅支持 content_frame 链」，
> 避免误导成通用表达式被拒。

- [ ] **Step 4: Run the strict-expression and existing parser suites**

Run:

```powershell
D:/Anaconda/envs/codegen-marker/python.exe -m unittest test.test_replica_action_parser -v
```

Expected: all tests PASS.

- [ ] **Step 5: Commit strict expression parsing**

```powershell
git add -- rewrite_script.py test/test_replica_action_parser.py
git commit -m "feat: validate editable locator expressions"
```

---

### Task 4: Implement atomic locator replacement

**Files:**
- Modify: `rewrite_script.py`
- Modify: `test/test_replica_action_parser.py`

- [ ] **Step 1: Write failing transaction tests**

Import `replace_action_locator`, then add:

```python
def test_replace_action_locator_changes_only_multiline_receiver(self):
    source = '''# [MARKER: Meta 信息工具]
page.locator("#iframe").content_frame.locator(
    "#old"
).click(position={"x": 4, "y": 5})
page.locator("#untouched").fill("2000")
'''
    updated = replace_action_locator(
        source,
        "a_000_001",
        'page.locator("#iframe").content_frame.get_by_test_id("confirm")',
    )

    self.assertIn(
        'page.locator("#iframe").content_frame'
        '.get_by_test_id("confirm").click(position={"x": 4, "y": 5})',
        updated,
    )
    self.assertIn('page.locator("#untouched").fill("2000")', updated)

def test_replace_action_locator_preserves_marker_uuid(self):
    source = '# [MARKER: 报告截图]\npage.locator("#old").click()\n'
    annotations = [{
        "marker_id": "marker-stable",
        "line": 1,
        "label": "报告截图",
    }]
    updated = replace_action_locator(
        source,
        "a_000_001",
        'page.get_by_role("button", name="Open")',
        annotations,
    )
    reparsed = parse_action_plan(updated, annotations)

    self.assertEqual(reparsed.marker_groups[0].marker_id, "marker-stable")
    self.assertEqual(reparsed.marker_groups[0].actions[0].marker_id, "marker-stable")

def test_replace_action_locator_rejects_page_variable_change_atomically(self):
    source = '# [MARKER: 报告截图]\npage.locator("#old").click()\n'
    with self.assertRaisesRegex(LocatorEditError, "page variable"):
        replace_action_locator(
            source,
            "a_000_001",
            'page1.locator("#new")',
        )
    self.assertIn('page.locator("#old").click()', source)

def test_replace_action_locator_rejects_missing_action(self):
    source = '# [MARKER: 报告截图]\npage.locator("#old").click()\n'
    with self.assertRaisesRegex(LocatorEditError, "not found"):
        replace_action_locator(source, "a_999_999", 'page.locator("#new")')
```

- [ ] **Step 2: Run transaction tests and verify failure**

Run:

```powershell
D:/Anaconda/envs/codegen-marker/python.exe -m unittest `
  test.test_replica_action_parser.ReplicaActionParserTests.test_replace_action_locator_changes_only_multiline_receiver `
  test.test_replica_action_parser.ReplicaActionParserTests.test_replace_action_locator_preserves_marker_uuid `
  test.test_replica_action_parser.ReplicaActionParserTests.test_replace_action_locator_rejects_page_variable_change_atomically `
  test.test_replica_action_parser.ReplicaActionParserTests.test_replace_action_locator_rejects_missing_action -v
```

Expected: FAIL because `replace_action_locator` does not exist.

- [ ] **Step 3: Implement the in-memory transaction**

Add to `rewrite_script.py`:

```python
def _find_action(plan: ActionPlan, action_id: str) -> ActionTarget | None:
    return next(
        (
            action
            for group in plan.marker_groups
            for action in group.actions
            if action.action_id == action_id
        ),
        None,
    )


def replace_action_locator(
    source: str,
    action_id: str,
    expression: str,
    marker_annotations: Sequence[Mapping[str, object]] | None = None,
) -> str:
    """Return a fully validated script with one locator receiver replaced."""
    original_plan = parse_action_plan(source, marker_annotations)
    original_action = _find_action(original_plan, action_id)
    if original_action is None or original_action.locator is None:
        raise LocatorEditError(f"locator action {action_id!r} was not found")
    span = original_plan.locator_source_spans.get(action_id)
    if span is None:
        raise LocatorEditError(f"locator source span for {action_id!r} was not found")
    recipe = parse_locator_expression(expression)
    if recipe.page_var != original_action.locator.page_var:
        raise LocatorEditError(
            "page variable cannot change during a locator-only edit"
        )
    start, end = source_span_offsets(source, span)
    candidate = source[:start] + expression + source[end:]
    try:
        ast.parse(candidate)
    except SyntaxError as error:
        raise LocatorEditError(
            f"updated script is invalid: {error.msg}"
        ) from error
    reparsed = parse_action_plan(candidate, marker_annotations)
    updated_action = _find_action(reparsed, action_id)
    if updated_action is None or updated_action.locator is None:
        raise LocatorEditError("target action disappeared after reparsing")
    if updated_action.action_type != original_action.action_type:
        raise LocatorEditError("action type changed after locator replacement")
    if updated_action.marker_id != original_action.marker_id:
        raise LocatorEditError("marker identity changed after locator replacement")
    return candidate
```

> **事务不校验 iframe 链等价（P6）。** 上面只校验 `page_var` 一致、`action_type`
> 不变、`marker_id` 不变；`frame_chain` 的增删改是**允许**的（iframe 链本就是
> 编辑目标），因此事务不保证新链与原链等价。这是符合编辑意图的，但实现与规格
> §6 步骤 7「能生成 locator」的措辞相比更弱，需明确：事务只保证「动作身份与类型
> 不变」，不保证接管 iframe 拓扑保持一致。

- [ ] **Step 4: Run transaction and parser tests**

Run:

```powershell
D:/Anaconda/envs/codegen-marker/python.exe -m unittest test.test_replica_action_parser -v
```

Expected: all tests PASS. The multiline receiver collapses only where the new
expression is inserted; action arguments and the following line remain unchanged.

- [ ] **Step 5: Commit the atomic replacement API**

```powershell
git add -- rewrite_script.py test/test_replica_action_parser.py
git commit -m "feat: atomically replace action locators"
```

---

### Task 5: Rebuild GUI display state from edited source

**Files:**
- Modify: `main_gui.py:197-278,884-921`
- Modify: `test/test_replica_gui.py`

- [ ] **Step 1: Write failing pure synchronization tests**

Import `rebuild_display_state_from_source` in `test/test_replica_gui.py`, then add:

```python
def test_rebuild_display_state_preserves_marker_id_after_multiline_edit(self):
    source = '''page.locator(
    "#open"
).click()
# [MARKER: 报告截图]
# page.screenshot(path="report.png")
'''
    anchors = [{
        "marker_id": "marker-1",
        "codegen_idx": 0,
        "fingerprint": 'page.locator("#old").click()',
        "items": [
            {
                "type": "marker",
                "text": "# [MARKER: 报告截图]",
                "marker_id": "marker-1",
            },
            {
                "type": "marker",
                "text": '# page.screenshot(path="report.png")',
                "marker_id": "marker-1",
            },
        ],
    }]

    items, rebuilt = rebuild_display_state_from_source(source, anchors)

    marker_items = [item for item in items if item["type"] == "marker"]
    self.assertEqual(len(marker_items), 2)
    self.assertTrue(all(item["marker_id"] == "marker-1" for item in marker_items))
    self.assertEqual(rebuilt[0]["marker_id"], "marker-1")
    self.assertEqual(rebuilt[0]["fingerprint"], ").click()")

def test_rebuild_display_state_assigns_uuid_to_manually_typed_marker(self):
    source = 'page.locator("#open").click()\n# [MARKER: 报告截图]\n'

    items, anchors = rebuild_display_state_from_source(source, [])

    marker = next(item for item in items if item["type"] == "marker")
    self.assertTrue(marker["marker_id"])
    self.assertEqual(anchors[0]["marker_id"], marker["marker_id"])

def test_duplicate_marker_headers_preserve_distinct_ids_by_occurrence(self):
    source = '''page.locator("#one").click()
# [MARKER: 报告截图]
page.locator("#two").click()
# [MARKER: 报告截图]
'''
    anchors = [
        {
            "marker_id": "marker-1",
            "codegen_idx": 0,
            "fingerprint": 'page.locator("#one").click()',
            "items": [{
                "type": "marker",
                "text": "# [MARKER: 报告截图]",
                "marker_id": "marker-1",
            }],
        },
        {
            "marker_id": "marker-2",
            "codegen_idx": 1,
            "fingerprint": 'page.locator("#two").click()',
            "items": [{
                "type": "marker",
                "text": "# [MARKER: 报告截图]",
                "marker_id": "marker-2",
            }],
        },
    ]

    annotations = build_annotations_from_source(source, anchors)

    self.assertEqual(
        [marker["marker_id"] for marker in annotations["markers"]],
        ["marker-1", "marker-2"],
    )
```

- [ ] **Step 2: Run synchronization tests and verify failure**

Run:

```powershell
D:/Anaconda/envs/codegen-marker/python.exe -m unittest `
  test.test_replica_gui.ReplicaGuiTests.test_rebuild_display_state_preserves_marker_id_after_multiline_edit `
  test.test_replica_gui.ReplicaGuiTests.test_rebuild_display_state_assigns_uuid_to_manually_typed_marker -v
```

Expected: FAIL because `rebuild_display_state_from_source` does not exist.

- [ ] **Step 3: Make duplicate marker identity matching occurrence-safe**

Add:

```python
from collections import defaultdict, deque
```

Replace the fingerprint index in `build_annotations_from_source()` with:

```python
ids_by_fingerprint: dict[str, deque[str]] = defaultdict(deque)
for anchor in anchors:
    marker_id = anchor.get("marker_id")
    header = next(
        (
            item
            for item in anchor.get("items") or []
            if item.get("type") == "marker"
            and (item.get("text") or "").lstrip().startswith("# [MARKER:")
        ),
        None,
    )
    if marker_id and header:
        fingerprint = _fingerprint(header.get("text") or "")
        ids_by_fingerprint[fingerprint].append(str(marker_id))
```

In the parsed-marker loop, replace `id_by_fp.get(fp)` with:

```python
known_ids = ids_by_fingerprint.get(fp)
marker_id = known_ids.popleft() if known_ids else str(uuid.uuid4())
```

This preserves two identical Marker headers as two distinct UUIDs in their
existing occurrence order.

> **影响面：`build_annotations_from_source` 是共享函数。** 它不是只给状态重建用，
> 也是 `_annotations_for_export()` / `_write_annotations()` 生成导出注解的唯一来源
> （`main_gui.py:616`）。此改动会让「重复 marker 头按 occurrence 各自保留 UUID」的
> 行为连带作用到导出路径。`deque.popleft()` 按出现顺序消费，语义正确，但请
> 在 Task 8 回归里显式覆盖「同标签重复 marker 头的导出注解仍各自持不同 UUID」，
> 避免只改 `main_gui` 的简单印象遗漏这一步。

- [ ] **Step 4: Implement deterministic state reconstruction**

Add the following pure function after `build_annotations_from_source()` in
`main_gui.py`:

```python
def rebuild_display_state_from_source(
    source_code: str,
    old_anchors: List[Dict],
) -> tuple[List[Dict[str, str]], List[Dict]]:
    """Rebuild line items and anchors after an arbitrary source edit."""
    annotations = build_annotations_from_source(source_code, old_anchors)
    marker_id_by_line = {
        int(marker["line"]): str(marker["marker_id"])
        for marker in annotations["markers"]
    }
    old_anchor_by_id = {
        str(anchor["marker_id"]): anchor
        for anchor in old_anchors
        if anchor.get("marker_id")
    }
    lines = source_code.splitlines()
    marker_ranges: dict[int, tuple[int, str]] = {}
    for start_line, marker_id in marker_id_by_line.items():
        previous = old_anchor_by_id.get(marker_id)
        item_count = max(1, len(previous.get("items", []))) if previous else 1
        end_line = min(len(lines), start_line + item_count - 1)
        marker_ranges[start_line] = (end_line, marker_id)

    display_items: List[Dict[str, str]] = []
    rebuilt_anchors: List[Dict] = []
    codegen_lines: List[str] = []
    line_number = 1
    while line_number <= len(lines):
        marker_range = marker_ranges.get(line_number)
        if marker_range is None:
            text = lines[line_number - 1]
            display_items.append({"type": "codegen", "text": text})
            codegen_lines.append(text)
            line_number += 1
            continue
        end_line, marker_id = marker_range
        marker_items = [
            {
                "type": "marker",
                "text": lines[index - 1],
                "marker_id": marker_id,
            }
            for index in range(line_number, end_line + 1)
        ]
        display_items.extend(marker_items)
        codegen_idx = max(0, len(codegen_lines) - 1)
        fingerprint = _fingerprint(codegen_lines[-1]) if codegen_lines else ""
        rebuilt_anchors.append({
            "marker_id": marker_id,
            "codegen_idx": codegen_idx,
            "fingerprint": fingerprint,
            "items": marker_items,
        })
        line_number = end_line + 1
    return display_items, rebuilt_anchors
```

> **限制：marker 块长度来自旧 anchor，而非源码实际边界（P1）。** 上面 `item_count =
> max(1, len(previous["items"]))` 复用旧块行数来推断撤销块范围。若用户在此前的手
> 动编辑里，从一个 marker 头下**增删了行**（如截图注释行数变化），重建会多吞或漏
> 吞 marker 行，导致 `_display_items` 与源码错位。3 个空测试都只覆盖「块行数不变」。
> 执行时建议（非阻塞）：优先从 `agent.parse_markers` 的块边界直接推算撤销范围，
> 或至少补一个「marker 块内行数变化后重建仍对齐」的测试锁定边界行为。

- [ ] **Step 5: Add one canonical MainWindow source setter**

Add to `MainWindow`:

```python
def _set_editor_source(self, source: str) -> None:
    """Atomically synchronize the editor and both line/marker data models."""
    display_items, anchors = rebuild_display_state_from_source(
        source,
        self._marker_anchors,
    )
    self._display_items = display_items
    self._marker_anchors = anchors
    self.code_view.setPlainText(source)
    self._latest_code = source
    self._update_export_enabled()
```

Do not route codegen worker updates through this method: `_on_code_ready()` must
retain its existing `relocate_markers()` semantics while recording. This setter
is for stopped-recording/manual and locator-panel edits.

> **语义限制：非 marker 行一律重建为 `codegen` 项。** `rebuild_display_state_from_source`
> 会把停止录制后在左侧手写的临时/注释行重分类为 `type="codegen"`。源码本身不丢，
> 但 `_display_items` 语义漂移；若之后再次启动录制 → `_on_code_ready` →
> `relocate_markers`，这些「伪 codegen 行」不在新 codegen 流里，锚在上面的 marker
> 可能被当作「锚点消失」而丢弃。apply/setter 只在停止态使用，风险窗口小，但
> 行为如此，无需修复，仅记录该限制。

- [ ] **Step 6: Run GUI synchronization regressions**

Run:

```powershell
$env:QT_QPA_PLATFORM='offscreen'
D:/Anaconda/envs/codegen-marker/python.exe -m unittest test.test_replica_gui -v
```

Expected: all GUI tests PASS.

- [ ] **Step 7: Commit source-state synchronization**

```powershell
git add -- main_gui.py test/test_replica_gui.py
git commit -m "fix: synchronize GUI state after source edits"
```

---

### Task 6: Build the standalone annotation panel

**Files:**
- Create: `replica_annotation_panel.py`
- Create: `test/test_replica_annotation_panel.py`

- [ ] **Step 1: Write failing panel component tests**

Create `test/test_replica_annotation_panel.py`:

```python
import sys
import unittest

from PyQt6.QtWidgets import QApplication

from replica_annotation_panel import ReplicaAnnotationPanel
from rewrite_script import parse_action_plan


APP = QApplication.instance() or QApplication(sys.argv)


class ReplicaAnnotationPanelTests(unittest.TestCase):
    def setUp(self):
        self.panel = ReplicaAnnotationPanel()
        self.panel.set_editable(True)
        self.source = '''# [MARKER: 序列选择]
page.locator(".series").nth(2).dblclick()
# [MARKER: 影像画布交互]
page.mouse.click(819, 318)
'''
        self.plan = parse_action_plan(self.source)
        self.panel.set_plan(self.source, self.plan)

    def tearDown(self):
        self.panel.close()

    def test_groups_actions_by_marker_and_filters_high_risk(self):
        self.assertEqual(self.panel.tree.topLevelItemCount(), 2)
        self.panel.high_risk_only.setChecked(True)
        self.assertEqual(self.panel.tree.topLevelItemCount(), 2)

    def test_locator_selection_previews_risk_without_mutating_source(self):
        locator_item = self.panel.tree.topLevelItem(0).child(0)
        self.panel.tree.setCurrentItem(locator_item)
        self.panel.expression_editor.setPlainText(
            'page.get_by_test_id("series-primary")'
        )

        self.assertIn("ordinal", self.panel.risk_label.text())
        self.assertIn("stable_attribute", self.panel.risk_label.text())
        self.assertTrue(self.panel.apply_button.isEnabled())
        self.assertEqual(self.panel.source, self.source)
        self.assertEqual(
            locator_item.foreground(2).color().name(),
            "#d97706",
        )

    def test_invalid_expression_disables_apply_and_shows_reason(self):
        locator_item = self.panel.tree.topLevelItem(0).child(0)
        self.panel.tree.setCurrentItem(locator_item)
        self.panel.expression_editor.setPlainText("page.locator(selector)")

        self.assertFalse(self.panel.apply_button.isEnabled())
        self.assertIn("static literal", self.panel.error_label.text())

    def test_coordinate_action_is_read_only(self):
        coordinate_item = self.panel.tree.topLevelItem(1).child(0)
        self.panel.tree.setCurrentItem(coordinate_item)

        self.assertTrue(self.panel.expression_editor.isReadOnly())
        self.assertFalse(self.panel.apply_button.isEnabled())
        self.assertIn("coordinate", self.panel.error_label.text())

    def test_apply_emits_action_id_and_expression(self):
        received = []
        self.panel.locator_apply_requested.connect(
            lambda action_id, expression: received.append(
                (action_id, expression)
            )
        )
        locator_item = self.panel.tree.topLevelItem(0).child(0)
        self.panel.tree.setCurrentItem(locator_item)
        self.panel.expression_editor.setPlainText(
            'page.get_by_test_id("series-primary")'
        )
        self.panel.apply_button.click()

        self.assertEqual(
            received,
            [("a_000_001", 'page.get_by_test_id("series-primary")')],
        )


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run panel tests and verify the module is missing**

Run:

```powershell
$env:QT_QPA_PLATFORM='offscreen'
D:/Anaconda/envs/codegen-marker/python.exe -m unittest test.test_replica_annotation_panel -v
```

Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement the panel data flow and widget tree**

Create `replica_annotation_panel.py` with these public members:

```python
"""Developer-facing locator annotation widget."""

from __future__ import annotations

from dataclasses import replace

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from locator_risk import HIGH_RISK_LOCATORS, classify_locator_risk
from replica_models import ActionTarget
from rewrite_script import (
    ActionPlan,
    LocatorEditError,
    SourceSpan,
    parse_locator_expression,
    source_span_offsets,
)


ACTION_ID_ROLE = int(Qt.ItemDataRole.UserRole)
RISK_COLORS = {
    "stable_id": "#15803d",
    "aria": "#15803d",
    "stable_attribute": "#15803d",
    "text": "#2563eb",
    "ordinal": "#d97706",
    "structural": "#c2410c",
    "coordinate": "#b91c1c",
    "non_locator": "#6b7280",
}


class ReplicaAnnotationPanel(QWidget):
    source_jump_requested = pyqtSignal(object)
    locator_apply_requested = pyqtSignal(str, str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.source = ""
        self.plan: ActionPlan | None = None
        self.actions: dict[str, ActionTarget] = {}
        self.current_action_id: str | None = None
        self.editable = False

        root = QVBoxLayout(self)
        self.status_label = QLabel("没有可解析的复刻动作")
        self.high_risk_only = QCheckBox("只看高风险")
        self.high_risk_only.toggled.connect(self._populate_tree)
        root.addWidget(self.status_label)
        root.addWidget(self.high_risk_only)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Marker / Action", "行", "风险"])
        self.tree.currentItemChanged.connect(self._on_selection_changed)
        root.addWidget(self.tree, 1)

        self.action_label = QLabel("动作：—")
        self.frame_label = QLabel("iframe：—")
        self.expression_editor = QPlainTextEdit()
        self.expression_editor.setPlaceholderText(
            "选择一个 locator 动作后编辑完整 receiver"
        )
        self.expression_editor.textChanged.connect(self._preview)
        self.risk_label = QLabel("风险：—")
        self.error_label = QLabel("")
        self.error_label.setWordWrap(True)
        root.addWidget(self.action_label)
        root.addWidget(self.frame_label)
        root.addWidget(self.expression_editor)
        root.addWidget(self.risk_label)
        root.addWidget(self.error_label)

        buttons = QHBoxLayout()
        self.reset_button = QPushButton("恢复")
        self.apply_button = QPushButton("应用")
        self.reset_button.clicked.connect(self._reset_expression)
        self.apply_button.clicked.connect(self._emit_apply)
        buttons.addWidget(self.reset_button)
        buttons.addWidget(self.apply_button)
        root.addLayout(buttons)
        self.apply_shortcut = QShortcut(QKeySequence("Ctrl+Return"), self)
        self.apply_shortcut.activated.connect(self._emit_apply)
        self._clear_editor()

    def set_editable(self, editable: bool) -> None:
        self.editable = editable
        self._preview()

    def set_parse_error(self, message: str) -> None:
        self.status_label.setText(f"当前源码无法解析：{message}")
        self.tree.setEnabled(False)
        self.expression_editor.setReadOnly(True)
        self.apply_button.setEnabled(False)

    def set_plan(self, source: str, plan: ActionPlan) -> None:
        self.source = source
        self.plan = plan
        self.actions = {
            action.action_id: action
            for group in plan.marker_groups
            for action in group.actions
        }
        self.tree.setEnabled(True)
        self.status_label.setText("录制期间只读" if not self.editable else "可编辑")
        self._populate_tree()

    def _populate_tree(self) -> None:
        selected = self.current_action_id
        self.tree.clear()
        if self.plan is None:
            return
        for group in self.plan.marker_groups:
            visible_actions = [
                action for action in group.actions
                if (
                    not self.high_risk_only.isChecked()
                    or classify_locator_risk(action) in HIGH_RISK_LOCATORS
                )
            ]
            if not visible_actions:
                continue
            group_item = QTreeWidgetItem([group.marker_label, "", ""])
            self.tree.addTopLevelItem(group_item)
            for action in visible_actions:
                risk = classify_locator_risk(action)
                line = str(action.action_args.get("_source_line", ""))
                item = QTreeWidgetItem(
                    [f"{action.action_id}  {action.action_type}", line, risk]
                )
                item.setData(0, ACTION_ID_ROLE, action.action_id)
                item.setForeground(
                    2,
                    QBrush(QColor(RISK_COLORS[risk])),
                )
                group_item.addChild(item)
                if action.action_id == selected:
                    self.tree.setCurrentItem(item)
            group_item.setExpanded(True)

    def _on_selection_changed(self, current, _previous) -> None:
        action_id = current.data(0, ACTION_ID_ROLE) if current else None
        if not action_id:
            self.current_action_id = None
            self._clear_editor()
            return
        self.current_action_id = str(action_id)
        action = self.actions[self.current_action_id]
        self.action_label.setText(
            f"动作：{action.action_type} · 页面："
            f"{action.locator.page_var if action.locator else 'page'}"
        )
        if action.locator is None:
            self.frame_label.setText("iframe：—")
            self.expression_editor.setPlainText("")
            self.expression_editor.setReadOnly(True)
            self.risk_label.setText("风险：coordinate")
            self.error_label.setText(
                "coordinate 动作首版只读；请在左侧脚本中改写整条动作。"
            )
            self.apply_button.setEnabled(False)
            return
        frames = [hop.selector for hop in action.locator.frame_chain]
        self.frame_label.setText(
            "iframe：" + (" → ".join(frames) if frames else "无")
        )
        span = self.plan.locator_source_spans[action.action_id]
        start, end = source_span_offsets(self.source, span)
        self.expression_editor.blockSignals(True)
        self.expression_editor.setPlainText(self.source[start:end])
        self.expression_editor.blockSignals(False)
        self.expression_editor.setReadOnly(not self.editable)
        self.source_jump_requested.emit(span)
        self._preview()

    def _clear_editor(self) -> None:
        self.action_label.setText("动作：—")
        self.frame_label.setText("iframe：—")
        self.expression_editor.setPlainText("")
        self.expression_editor.setReadOnly(True)
        self.risk_label.setText("风险：—")
        self.error_label.setText("")
        self.apply_button.setEnabled(False)
        self.reset_button.setEnabled(False)

    def _preview(self) -> None:
        action = self.actions.get(self.current_action_id or "")
        if action is None or action.locator is None:
            return
        self.reset_button.setEnabled(self.editable)
        if not self.editable:
            self.expression_editor.setReadOnly(True)
            self.apply_button.setEnabled(False)
            return
        expression = self.expression_editor.toPlainText().strip()
        try:
            recipe = parse_locator_expression(expression)
            if recipe.page_var != action.locator.page_var:
                raise LocatorEditError("page variable cannot change")
        except LocatorEditError as error:
            self.error_label.setText(str(error))
            self.apply_button.setEnabled(False)
            return
        preview_target = replace(action, locator=recipe)
        self.risk_label.setText(
            f"风险：{classify_locator_risk(action)} → "
            f"{classify_locator_risk(preview_target)}"
        )
        frames = [hop.selector for hop in recipe.frame_chain]
        self.frame_label.setText(
            "iframe：" + (" → ".join(frames) if frames else "无")
        )
        self.error_label.setText("")
        self.apply_button.setEnabled(True)

    def _reset_expression(self) -> None:
        if self.current_action_id and self.plan:
            action = self.actions[self.current_action_id]
            if action.locator is not None:
                span = self.plan.locator_source_spans[action.action_id]
                start, end = source_span_offsets(self.source, span)
                self.expression_editor.setPlainText(self.source[start:end])

    def _emit_apply(self) -> None:
        if self.current_action_id and self.apply_button.isEnabled():
            self.locator_apply_requested.emit(
                self.current_action_id,
                self.expression_editor.toPlainText().strip(),
            )
```

`_emit_apply()` already checks `apply_button.isEnabled()`, so the shortcut
cannot bypass validation or read-only state.

- [ ] **Step 4: Run the panel component tests**

Run:

```powershell
$env:QT_QPA_PLATFORM='offscreen'
D:/Anaconda/envs/codegen-marker/python.exe -m unittest test.test_replica_annotation_panel -v
```

Expected: all 5 tests PASS.

- [ ] **Step 5: Commit the standalone panel**

```powershell
git add -- replica_annotation_panel.py test/test_replica_annotation_panel.py
git commit -m "feat: add replica locator annotation widget"
```

---

### Task 7: Integrate the panel into MainWindow

**Files:**
- Modify: `main_gui.py:14-56,365-499,528-598,884-921`
- Modify: `test/test_replica_gui.py`

- [ ] **Step 1: Write failing MainWindow integration tests**

Add to `test/test_replica_gui.py`:

```python
def _load_editable_locator_source(self):
    source = '''def run(page):
    # [MARKER: 序列选择]
    page.locator(".series").nth(2).dblclick()
'''
    self.window._set_editor_source(source)
    self.window._manager = None
    self.window._refresh_annotation_panel()
    return source

def test_annotation_panel_is_read_only_while_recording(self):
    self._load_editable_locator_source()
    self.window._manager = object()
    self.window._refresh_annotation_panel()

    self.assertFalse(self.window.annotation_panel.editable)

def test_annotation_panel_applies_locator_and_marks_source_unsaved(self):
    self._load_editable_locator_source()
    self.window._saved_source_hash = hashlib.sha256(
        self.window._latest_code.encode("utf-8")
    ).hexdigest()
    self.window._apply_locator_edit(
        "a_000_001",
        'page.get_by_test_id("series-primary")',
    )

    self.assertIn(
        'page.get_by_test_id("series-primary").dblclick()',
        self.window._latest_code,
    )
    self.assertFalse(self.window.export_replica_btn.isEnabled())
    action = (
        self.window.annotation_panel.plan
        .marker_groups[0]
        .actions[0]
    )
    self.assertEqual(action.locator.locator_kind, "test_id")

def test_annotation_panel_failed_apply_keeps_source_unchanged(self):
    source = self._load_editable_locator_source()

    self.window._apply_locator_edit(
        "a_000_001",
        "page.locator(selector)",
    )

    self.assertEqual(self.window._latest_code, source)

def test_annotation_selection_jumps_to_receiver_source(self):
    self._load_editable_locator_source()
    action = self.window.annotation_panel.plan.marker_groups[0].actions[0]
    span = self.window.annotation_panel.plan.locator_source_spans[action.action_id]

    self.window._select_source_span(span)

    self.assertEqual(
        self.window.code_view.textCursor().selectedText(),
        'page.locator(".series").nth(2)',
    )

def test_invalid_manual_source_keeps_last_plan_as_read_only_reference(self):
    self._load_editable_locator_source()
    previous_plan = self.window.annotation_panel.plan
    self.window._latest_code = "def broken(:\n"

    self.window._refresh_annotation_panel()

    self.assertIs(self.window.annotation_panel.plan, previous_plan)
    self.assertFalse(self.window.annotation_panel.tree.isEnabled())
    self.assertIn(
        "当前源码无法解析",
        self.window.annotation_panel.status_label.text(),
    )
```

> **错误态恢复与 `set_parse_error` 的联动（P3）。** `set_parse_error` 会把编辑框置
> 只读、禁用 apply。源码修复后 `set_plan` 只重新启用 tree 和状态栏，编辑框只在
> `_on_selection_changed` 选中某 action 时才 `setReadOnly(False)` 复位。补一条恢复测试，
> 锁定「解析失败 → 修复源码 → 重新选中 action 后编辑框/apply 恢复可编辑」的行为：

```python
def test_parse_error_recovers_after_source_is_fixed(self):
    self._load_editable_locator_source()
    self.window._latest_code = "def broken(:\n"
    self.window._refresh_annotation_panel()
    self.assertFalse(self.window.annotation_panel.expression_editor.isReadOnly() or True)  # 占位：断言应落在恢复后
```

> 执行时把上面占位断言替换为：修复源码 → `_refresh_annotation_panel()` → 选中
> `a_000_001` → 断言 `expression_editor.isReadOnly() is False` 且 `apply_button.isEnabled()`
> 恢复。

- [ ] **Step 2: Run the integration tests and verify missing members**

Run:

```powershell
$env:QT_QPA_PLATFORM='offscreen'
D:/Anaconda/envs/codegen-marker/python.exe -m unittest `
  test.test_replica_gui.ReplicaGuiTests.test_annotation_panel_is_read_only_while_recording `
  test.test_replica_gui.ReplicaGuiTests.test_annotation_panel_applies_locator_and_marks_source_unsaved `
  test.test_replica_gui.ReplicaGuiTests.test_annotation_panel_failed_apply_keeps_source_unchanged `
  test.test_replica_gui.ReplicaGuiTests.test_annotation_selection_jumps_to_receiver_source `
  test.test_replica_gui.ReplicaGuiTests.test_invalid_manual_source_keeps_last_plan_as_read_only_reference -v
```

Expected: FAIL because MainWindow has no annotation panel integration.

- [ ] **Step 3: Add the splitter and connect panel signals**

In `main_gui.py`, import:

```python
from PyQt6.QtWidgets import QSplitter
from replica_annotation_panel import ReplicaAnnotationPanel
from rewrite_script import (
    LocatorEditError,
    SourceSpan,
    parse_action_plan,
    replace_action_locator,
    source_span_offsets,
)
```

In `MainWindow.__init__`, before `_build_ui()`:

```python
self._annotation_refresh_timer = QTimer(self)
self._annotation_refresh_timer.setSingleShot(True)
self._annotation_refresh_timer.setInterval(300)
self._annotation_refresh_timer.timeout.connect(
    self._refresh_annotation_panel
)
```

In `_build_ui()`, replace the direct `layout.addWidget(self.code_view, 1)` with:

```python
self.annotation_panel = ReplicaAnnotationPanel()
self.annotation_panel.source_jump_requested.connect(
    self._select_source_span
)
self.annotation_panel.locator_apply_requested.connect(
    self._apply_locator_edit
)
self.editor_splitter = QSplitter(Qt.Orientation.Horizontal)
self.editor_splitter.addWidget(self.code_view)
self.editor_splitter.addWidget(self.annotation_panel)
self.editor_splitter.setStretchFactor(0, 3)
self.editor_splitter.setStretchFactor(1, 2)
layout.addWidget(self.editor_splitter, 1)
```

- [ ] **Step 4: Add debounced parsing, source jump, and atomic apply**

Add these methods to `MainWindow`:

```python
def _refresh_annotation_panel(self) -> None:
    editable = self._manager is None and self._export_process is None
    self.annotation_panel.set_editable(editable)
    if not self._latest_code:
        self.annotation_panel.set_plan("", parse_action_plan(""))
        return
    try:
        annotations = self._annotations_for_export()["markers"]
        plan = parse_action_plan(self._latest_code, annotations)
    except (SyntaxError, ValueError) as error:
        self.annotation_panel.set_parse_error(str(error))
        return
    self.annotation_panel.set_plan(self._latest_code, plan)

def _select_source_span(self, span: SourceSpan) -> None:
    start, end = source_span_offsets(self._latest_code, span)
    cursor = self.code_view.textCursor()
    cursor.setPosition(start)
    cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
    self.code_view.setTextCursor(cursor)
    self.code_view.ensureCursorVisible()

def _apply_locator_edit(self, action_id: str, expression: str) -> None:
    original = self._latest_code
    try:
        annotations = self._annotations_for_export()["markers"]
        updated = replace_action_locator(
            original,
            action_id,
            expression,
            annotations,
        )
    except (LocatorEditError, SyntaxError, ValueError) as error:
        self.annotation_panel.error_label.setText(str(error))
        self._show_status("Locator 修改未应用", 5000)
        return
    self._set_editor_source(updated)
    self._refresh_annotation_panel()
    self._show_status("Locator 已更新，请保存后再导出", 5000)
```

Update `_on_text_changed()`:

```python
def _on_text_changed(self) -> None:
    """Synchronize live source and debounce annotation parsing."""
    self._latest_code = self.code_view.toPlainText()
    self._update_export_enabled()
    self._annotation_refresh_timer.start()
```

- [ ] **Step 5: Wire recording/export lifecycle to editability**

At the end of `_on_start()`, `_on_stop()`, `_on_export_replica()`,
`_on_export_finished()`, and `_on_clear()`, call:

```python
self._refresh_annotation_panel()
```

The effective rule remains centralized in `_refresh_annotation_panel()`:

```python
editable = self._manager is None and self._export_process is None
```

Do not make the left code editor read-only; only the locator panel is locked
during recording/export.

- [ ] **Step 6: Run MainWindow and panel suites**

Run:

```powershell
$env:QT_QPA_PLATFORM='offscreen'
D:/Anaconda/envs/codegen-marker/python.exe -m unittest `
  test.test_replica_annotation_panel `
  test.test_replica_gui -v
```

Expected: all tests PASS; no modal dialog blocks the failed-apply test.

- [ ] **Step 7: Commit MainWindow integration**

```powershell
git add -- main_gui.py test/test_replica_gui.py
git commit -m "feat: integrate replica locator annotation panel"
```

---

### Task 8: Document migration and run complete regression verification

**Files:**
- Modify: `docs/MANUAL_ANNOTATION_REPLICA_SOP.md:84-112,132-164`
- Test: `test/test_replica_action_parser.py`
- Test: `test/test_locator_risk.py`
- Test: `test/test_replica_annotation_panel.py`
- Test: `test/test_replica_gui.py`
- Test: `test/test_build_replica.py`
- Test: `test/test_pipeline_validation.py`
- Test: full `test/` suite

- [ ] **Step 1: Update the operator/developer SOP with the implemented workflow**

Replace the “只能修改脚本文本” instructions in
`docs/MANUAL_ANNOTATION_REPLICA_SOP.md` with these concrete steps:

```markdown
### Step 3 — 在复刻标注面板精修 locator

1. 停止录制；录制期间面板只读。
2. 在右侧按 Marker 展开 ActionTarget。
3. 优先筛选 `ordinal`、`structural`、`coordinate`。
4. 选择 locator 动作，检查页面变量与 iframe 链。
5. 编辑完整 Playwright receiver；不要填写 `.click()`、`.fill()` 等动作调用。
6. 确认“修改前风险 → 修改后风险”和 iframe 预览。
7. 点击“应用”；失败时脚本不会改变。
8. 保存 processed 脚本后再运行复刻导出。

坐标动作首版只读，仍需在左侧把整条动作手动改写为 locator 动作。
面板风险是静态稳定性提示；唯一性和可见性仍由捕获及离线验证确认。
```

Add a migration note:

```markdown
> 风险桶迁移：本版本统一 GUI、build 和 validation 的分类。
> `get_by_text` 归入 `text`，`get_by_test_id` 归入
> `stable_attribute`，简单直接 CSS 使用 `stable_id` 历史桶名。
> 因桶名发生变化，升级前后的 `risk_counts` 不可直接比较；
> 同版本不同 run 之间可以比较。
```

- [ ] **Step 2: Run all focused feature tests**

Run:

```powershell
$env:QT_QPA_PLATFORM='offscreen'
D:/Anaconda/envs/codegen-marker/python.exe -m unittest `
  test.test_locator_risk `
  test.test_replica_action_parser `
  test.test_replica_annotation_panel `
  test.test_replica_gui `
  test.test_build_replica `
  test.test_pipeline_validation -v
```

Expected: all focused tests PASS.

- [ ] **Step 3: Run the complete repository test suite**

Run:

```powershell
$env:QT_QPA_PLATFORM='offscreen'
D:/Anaconda/envs/codegen-marker/python.exe -m unittest discover -s test -v
```

Expected: the command exits `0` with no failures or errors. Existing skips are
acceptable only if they were already present before this feature.

- [ ] **Step 4: Verify the two real recording fixtures parse through the panel path**

Run:

```powershell
@'
from pathlib import Path
from rewrite_script import parse_action_plan

for hospital in ("cxhospital", "uicloud"):
    path = Path("out") / hospital / f"processed_script_{hospital}.py"
    source = path.read_text(encoding="utf-8")
    plan = parse_action_plan(source)
    locator_actions = [
        action
        for group in plan.marker_groups
        for action in group.actions
        if action.locator is not None
    ]
    assert locator_actions, hospital
    assert all(
        action.action_id in plan.locator_source_spans
        for action in locator_actions
    ), hospital
    print(hospital, len(locator_actions))
'@ | D:/Anaconda/envs/codegen-marker/python.exe -
```

Expected: both `cxhospital` and `uicloud` print a positive locator-action count
and the process exits `0`.

- [ ] **Step 5: Check formatting and the exact implementation diff**

Run:

```powershell
git diff --check
git status --short
git diff -- locator_risk.py rewrite_script.py build_replica.py pipeline_validation.py replica_annotation_panel.py main_gui.py test docs/MANUAL_ANNOTATION_REPLICA_SOP.md
```

Expected:

- `git diff --check` produces no output;
- only the intended implementation/test/SOP files are part of the feature;
- the user's modified design specification remains unstaged unless the user
  explicitly asks to commit it.

- [ ] **Step 6: Commit the SOP and final regression adjustments**

```powershell
git add -- docs/MANUAL_ANNOTATION_REPLICA_SOP.md
git commit -m "docs: explain locator annotation workflow"
```

If focused regression tests required expectation-only changes after Tasks 1–7,
stage those exact test files in the same command; do not stage the modified
design specification.

---

## Definition of done

- Every locator action under a Marker appears in the stopped-recording panel.
- Selecting an action highlights only its receiver expression in the source editor.
- Single-line, multiline, nested-iframe, popup-page, and Chinese locator expressions can be edited safely.
- Invalid or dynamic expressions never mutate source state.
- Marker UUIDs survive locator-only edits; `action_id` is relied on only inside one replacement transaction.
- Coordinate actions are visible, high risk, and read-only.
- GUI/build/validation report the same risk bucket for the same `ActionTarget`.
- `non_locator` is excluded from `highest_risk`.
- Applying a locator edit marks the script unsaved and prevents export until save.
- Both hospital fixtures expose spans for every locator action.
- The full `unittest` suite passes.
