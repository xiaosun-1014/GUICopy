"""Parse marked Playwright codegen scripts without executing their source."""

from __future__ import annotations

import ast
import re
import tokenize
from dataclasses import dataclass, field
from io import StringIO
from typing import Any

from replica_models import ActionTarget, BootstrapPlan, FrameHop, LocatorRecipe, Point, PopupExpectation


MARKER_RE = re.compile(r"\[MARKER:\s*(?P<label>[^@\]]+?)(?:\s*@[^\]]+)?\]")
ACTION_METHODS = {"click", "dblclick", "fill", "press", "select_option", "hover"}


@dataclass
class MarkerGroup:
    marker_id: str
    marker_label: str
    source_line: int
    actions: list[ActionTarget] = field(default_factory=list)


@dataclass
class ActionPlan:
    bootstrap: BootstrapPlan
    marker_groups: list[MarkerGroup]
    popup_expectations: list[PopupExpectation]
    instrumented_source: str


def _literal_arguments(argument_text: str) -> tuple[list[Any], dict[str, Any]]:
    try:
        call = ast.parse(f"f({argument_text})", mode="eval").body
        return [ast.literal_eval(item) for item in call.args], {
            keyword.arg: ast.literal_eval(keyword.value) for keyword in call.keywords if keyword.arg
        }
    except (SyntaxError, ValueError):
        return [], {}


def _locator_from_expression(expression: ast.AST, page_var: str) -> LocatorRecipe | None:
    source = ast.unparse(expression)
    frame_chain = []
    for match in re.finditer(r"\.locator\((?P<args>[^)]*)\)\.content_frame", source):
        args, _ = _literal_arguments(match.group("args"))
        if args and isinstance(args[0], str):
            frame_chain.append(FrameHop(args[0], None, None))

    ordinal_op = None
    ordinal_value = None
    ordinal_match = re.search(r"\.(first|last|nth\((\d+)\))$", source)
    if ordinal_match:
        ordinal_op = ordinal_match.group(1).split("(")[0]
        ordinal_value = int(ordinal_match.group(2)) if ordinal_match.group(2) else None
        source = source[: ordinal_match.start()]

    matches = list(re.finditer(r"\.(locator|get_by_role|get_by_text|get_by_test_id|get_by_label|get_by_title)\(", source))
    if not matches:
        return None
    match = matches[-1]
    method = match.group(1)
    argument_text = source[match.end() : -1]
    args, kwargs = _literal_arguments(argument_text)
    kind = {
        "locator": "css",
        "get_by_role": "role",
        "get_by_text": "text",
        "get_by_test_id": "test_id",
        "get_by_label": "label",
        "get_by_title": "title",
    }[method]
    locator_args: dict[str, object] = {"args": args, **kwargs}
    return LocatorRecipe(source, page_var, frame_chain, kind, locator_args, ordinal_op, ordinal_value)


def _page_var(expression: ast.AST) -> str:
    source = ast.unparse(expression)
    match = re.match(r"(page\d*|page)\b", source)
    return match.group(1) if match else "page"


def _literal_value(expression: ast.AST) -> object:
    try:
        return ast.literal_eval(expression)
    except (ValueError, TypeError):
        return ast.unparse(expression)


def _action_target(call: ast.Call, marker_id: str, action_id: str) -> ActionTarget | None:
    if not isinstance(call.func, ast.Attribute):
        return None
    action_type = call.func.attr
    receiver = call.func.value
    page_var = _page_var(receiver)
    if action_type == "press" and isinstance(receiver, ast.Attribute) and receiver.attr == "keyboard":
        key = ast.literal_eval(call.args[0]) if call.args else None
        return ActionTarget(action_id, marker_id, "press", "keyboard", {}, None, None, None, None, key, "execute", None, "", f"t_{action_id}")
    if action_type in {"move", "click", "dblclick", "wheel"} and isinstance(receiver, ast.Attribute) and receiver.attr == "mouse":
        values = [ast.literal_eval(arg) for arg in call.args]
        point = Point(float(values[0]), float(values[1]), "page_viewport_css") if len(values) >= 2 else None
        return ActionTarget(action_id, marker_id, action_type, "mouse_xy", {"args": values}, None, None, None, point, None, "execute", None, "", f"t_{action_id}")
    if action_type in ACTION_METHODS:
        locator = _locator_from_expression(receiver, page_var)
        if locator is None:
            return None
        args = [_literal_value(arg) for arg in call.args]
        kwargs = {kw.arg: _literal_value(kw.value) for kw in call.keywords if kw.arg}
        return ActionTarget(action_id, marker_id, action_type, "locator", {"args": args, **kwargs}, locator, None, None, None, None, "execute", None, "", f"t_{action_id}")
    return None


def _marker_comments(source: str) -> list[tuple[int, str]]:
    found = []
    for token in tokenize.generate_tokens(StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            match = MARKER_RE.search(token.string)
            if match:
                found.append((token.start[0], match.group("label").strip()))
    return found


def parse_action_plan(source: str) -> ActionPlan:
    """Return a static plan; source is parsed but never imported or executed."""
    tree = ast.parse(source)
    marker_comments = _marker_comments(source)
    groups = [MarkerGroup(f"m_{index:03d}", label, line) for index, (line, label) in enumerate(marker_comments)]
    first_marker_line = marker_comments[0][0] if marker_comments else len(source.splitlines()) + 1
    bootstrap = BootstrapPlan(1, max(0, first_marker_line - 1), True, {"page": "main"})

    calls = sorted((node for node in ast.walk(tree) if isinstance(node, ast.Call)), key=lambda node: (node.lineno, node.col_offset))
    action_counts = [0] * len(groups)
    actions_by_line: dict[int, str] = {}
    for call in calls:
        candidates = [index for index, group in enumerate(groups) if group.source_line < call.lineno]
        if not candidates:
            continue
        group_index = candidates[-1]
        action_id = f"a_{group_index:03d}_{action_counts[group_index] + 1:03d}"
        action = _action_target(call, groups[group_index].marker_id, action_id)
        if action:
            action_counts[group_index] += 1
            action.action_args["_source_line"] = call.lineno
            groups[group_index].actions.append(action)
            actions_by_line[call.lineno] = action.action_id

    expectations: list[PopupExpectation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.With):
            continue
        popup_call = next((item.context_expr for item in node.items if isinstance(item.context_expr, ast.Call) and isinstance(item.context_expr.func, ast.Attribute) and item.context_expr.func.attr == "expect_popup"), None)
        if popup_call is None:
            continue
        item = next(item for item in node.items if item.context_expr is popup_call)
        if not isinstance(item.optional_vars, ast.Name):
            continue
        info_var = item.optional_vars.id
        result_var = next(
            (
                statement.targets[0].id
                for statement in ast.walk(tree)
                if isinstance(statement, ast.Assign)
                and isinstance(statement.targets[0], ast.Name)
                and ast.unparse(statement.value) == f"{info_var}.value"
            ),
            "",
        )
        body_ids = [actions_by_line[call.lineno] for call in ast.walk(node) if isinstance(call, ast.Call) and call.lineno in actions_by_line]
        expectations.append(PopupExpectation(node.lineno, _page_var(popup_call.func.value), info_var, result_var, body_ids))
    return ActionPlan(bootstrap, groups, expectations, source)


def _replay_locator_expression(step: dict[str, Any]) -> str:
    """Return a local Playwright locator expression from a serialized recipe."""
    locator = step.get("locator")
    if not locator:
        return "None"
    source_expression = locator.get("source_expression")
    page_var = locator["page_var"]
    if source_expression and source_expression.startswith(page_var):
        return f"pages[{page_var!r}]" + source_expression[len(page_var) :]
    expression = f"pages[{locator['page_var']!r}]"
    for hop in locator.get("frame_chain", []):
        expression += f".frame_locator({hop['selector']!r})"
    args = locator.get("locator_args", {})
    positional = ", ".join(repr(value) for value in args.get("args", []))
    keywords = ", ".join(f"{key}={value!r}" for key, value in args.items() if key != "args")
    joined = ", ".join(value for value in (positional, keywords) if value)
    methods = {"css": "locator", "role": "get_by_role", "text": "get_by_text", "test_id": "get_by_test_id", "label": "get_by_label", "title": "get_by_title"}
    expression += f".{methods[locator['locator_kind']]}({joined})"
    if locator.get("ordinal_op") == "first":
        expression += ".first"
    elif locator.get("ordinal_op") == "last":
        expression += ".last"
    elif locator.get("ordinal_op") == "nth":
        expression += f".nth({locator.get('ordinal_value', 0)})"
    return expression


def _replay_action_lines(step: dict[str, Any], indent: str = "        ") -> list[str]:
    """Generate one marked local action without evaluating recorded source code."""
    action_type = step["action_type"]
    page_var = step.get("page_var", "page")
    action_args = step.get("action_args", {})
    args = [value for value in action_args.get("args", [])]
    kwargs = {key: value for key, value in action_args.items() if key not in {"args", "_source_line"}}
    if step.get("action_source_kind") == "keyboard":
        return [f"{indent}pages[{page_var!r}].keyboard.press({step.get('key')!r})"]
    if step.get("action_source_kind") == "mouse_xy":
        values = ", ".join(repr(value) for value in args)
        return [f"{indent}pages[{page_var!r}].mouse.{action_type}({values})"]
    locator = _replay_locator_expression(step)
    values = ", ".join([*(repr(value) for value in args), *(f"{key}={value!r}" for key, value in kwargs.items())])
    return [f"{indent}{locator}.{action_type}({values})"]


def generate_replay_script(
    replica_directory: str,
    entry_page_bindings: dict[str, str],
    replay_steps: list[dict[str, Any]] | None = None,
) -> str:
    """Generate a syntax-safe offline replay entrypoint with no live bootstrap calls."""
    bindings = "\n".join(
        f"        # {page_var} is restored from local entry binding: {binding!r}"
        for page_var, binding in entry_page_bindings.items()
    )
    replay_lines: list[str] = []
    for step in replay_steps or []:
        transition = step.get("transition") or {}
        if transition.get("mode") == "popup":
            source_page = step.get("page_var", "page")
            target_page = transition.get("target_page_var", "page1")
            replay_lines.append(f"        with pages[{source_page!r}].expect_popup() as popup_info:")
            replay_lines.extend(_replay_action_lines(step, "            "))
            replay_lines.append(f"        pages[{target_page!r}] = popup_info.value")
        else:
            replay_lines.extend(_replay_action_lines(step))
    replay_body = "\n".join(replay_lines) or "        # No marked actions were captured."
    return f'''from pathlib import Path
from playwright.sync_api import sync_playwright
from serve_replica import ReplicaServer


def run() -> None:
    replica_root = Path(__file__).resolve().parent / {replica_directory!r}
    with ReplicaServer(replica_root) as server, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context()
        page = context.new_page()
        pages = {{"page": page}}
        page.goto(server.url)
{bindings}
{replay_body}
        browser.close()


if __name__ == "__main__":
    run()
'''


def generate_serve_script() -> str:
    """Generate a standalone loopback server launcher for a built replica tree."""
    return '''from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading


class ReplicaServer:
    def __init__(self, root):
        self.root = root

    def __enter__(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=str(self.root)))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}/index.html"

    def __exit__(self, *_):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)


def run() -> None:
    replica_root = Path(__file__).resolve().parent
    with ReplicaServer(replica_root) as server:
        print(server.url, flush=True)
        try:
            input("Press Enter to stop the replica server... ")
        except (EOFError, KeyboardInterrupt):
            pass


if __name__ == "__main__":
    run()
'''


def locator_risk_report(plan: ActionPlan) -> dict[str, int]:
    """Classify parsed actions by offline locator compatibility risk."""
    counts = {"simple": 0, "aria": 0, "ordinal": 0, "structural": 0, "non_locator": 0}
    for group in plan.marker_groups:
        for action in group.actions:
            locator = action.locator
            if locator is None:
                counts["non_locator"] += 1
            elif locator.ordinal_op:
                counts["ordinal"] += 1
            elif locator.locator_kind in {"role", "text", "test_id", "label", "title"}:
                counts["aria"] += 1
            elif any(token in str(locator.locator_args.get("args", [""])[0]) for token in (">", ":nth-", "[")):
                counts["structural"] += 1
            else:
                counts["simple"] += 1
    return counts
