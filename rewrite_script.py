"""Parse marked Playwright codegen scripts without executing their source."""

from __future__ import annotations

import ast
import re
import tokenize
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from io import StringIO
from typing import Any

from locator_risk import classify_locator_risk
from replica_models import ActionTarget, BootstrapPlan, FrameHop, LocatorRecipe, Point, PopupExpectation


MARKER_RE = re.compile(r"\[MARKER:\s*(?P<label>[^@\]]+?)(?:\s*@[^\]]+)?\]")
ACTION_METHODS = {"click", "dblclick", "fill", "press", "select_option", "hover"}
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
    locator_source_spans: dict[str, SourceSpan] = field(default_factory=dict)


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

    matches = list(re.finditer(r"\.({})\(".format("|".join(sorted(LOCATOR_METHODS))), source))
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


def _normalize_label(label: object) -> str:
    """Normalize a marker label for matching.

    Mirrors agent.MARKER_RE whitespace rules (collapse runs to a single space,
    strip leading/trailing) plus ASCII casefold, so annotation labels compare
    equal to script marker labels. Consistent with batch_capture's normalize_label.
    """
    return " ".join(str(label).split()).casefold()


def _build_groups_with_annotations(
    marker_comments: list[tuple[int, str]],
    annotations: Sequence[Mapping[str, object]],
) -> list[MarkerGroup]:
    """Build marker groups using GUI annotation UUIDs as the stable marker_id.

    Annotations are indexed by (source line, normalized label). Rules:
      - reject duplicate (line, label) keys and duplicate marker IDs;
      - every parsed marker comment must have exactly one matching annotation;
      - any annotation that matches no marker comment is rejected (unused).
    """
    by_key: dict[tuple[int, str], str] = {}
    seen_ids: set[str] = set()
    for annotation in annotations:
        marker_id = annotation["marker_id"]
        line = annotation["line"]
        key = (line, _normalize_label(annotation["label"]))
        if key in by_key:
            raise ValueError(f"duplicate annotation for source line {line}")
        if marker_id in seen_ids:
            raise ValueError(f"duplicate annotation marker_id {marker_id!r}")
        by_key[key] = marker_id
        seen_ids.add(marker_id)

    matched: set[tuple[int, str]] = set()
    groups: list[MarkerGroup] = []
    for line, label in marker_comments:
        key = (line, _normalize_label(label))
        if key not in by_key:
            raise ValueError(f"missing annotation for marker at source line {line}")
        matched.add(key)
        groups.append(MarkerGroup(by_key[key], label, line))

    unused = set(by_key) - matched
    if unused:
        raise ValueError(
            "unused annotations: "
            + ", ".join(f"L{line} {label!r}" for line, label in sorted(unused))
        )
    return groups


def parse_action_plan(
    source: str,
    marker_annotations: Sequence[Mapping[str, object]] | None = None,
) -> ActionPlan:
    """Return a static plan; source is parsed but never imported or executed.

    When ``marker_annotations`` is supplied (GUI replica annotations), each marker
    comment's stable GUI UUID is used as the group/action ``marker_id``. Without
    annotations, the historic ``m_{index:03d}`` IDs are preserved.
    """
    tree = ast.parse(source)
    marker_comments = _marker_comments(source)
    if marker_annotations is not None:
        groups = _build_groups_with_annotations(marker_comments, marker_annotations)
    else:
        groups = [MarkerGroup(f"m_{index:03d}", label, line) for index, (line, label) in enumerate(marker_comments)]
    first_marker_line = marker_comments[0][0] if marker_comments else len(source.splitlines()) + 1
    bootstrap = BootstrapPlan(1, max(0, first_marker_line - 1), True, {"page": "main"})

    calls = sorted((node for node in ast.walk(tree) if isinstance(node, ast.Call)), key=lambda node: (node.lineno, node.col_offset))
    action_counts = [0] * len(groups)
    actions_by_line: dict[int, str] = {}
    locator_source_spans: dict[str, SourceSpan] = {}
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
            if action.locator is not None:
                receiver = call.func.value
                locator_source_spans[action.action_id] = SourceSpan(
                    receiver.lineno,
                    receiver.col_offset,
                    receiver.end_lineno,
                    receiver.end_col_offset,
                )
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
    return ActionPlan(bootstrap, groups, expectations, source, locator_source_spans)


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


def _local_page_bootstrap_lines(indent: str, include_goto: bool = True) -> list[str]:
    """Render the shared local ReplicaServer-backed browser/context/page bootstrap lines.

    Used by both :func:`generate_replay_script` and
    :func:`generate_offline_adapter_script` so every offline entrypoint derives its
    ``browser``/``context``/``page``/``pages`` from the same local replica bootstrap
    (no live network auth). ``include_goto`` lets the offline adapter install its
    active-network-isolation route handler before navigating to ``server.url``.
    """
    lines = [
        f"{indent}browser = playwright.chromium.launch()",
        f"{indent}context = browser.new_context()",
        f"{indent}page = context.new_page()",
        f'{indent}pages = {{"page": page}}',
    ]
    if include_goto:
        lines.append(f"{indent}page.goto(server.url)")
    return lines


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
    body = [
        f"    replica_root = Path(__file__).resolve().parent / {replica_directory!r}",
        "    with ReplicaServer(replica_root) as server, sync_playwright() as playwright:",
        *_local_page_bootstrap_lines("        "),
    ]
    if bindings:
        body.append(bindings)
    body.append(replay_body)
    body.append("        browser.close()")
    body_text = "\n".join(body)
    return f'''from pathlib import Path
from playwright.sync_api import sync_playwright
from serve_replica import ReplicaServer


def run() -> None:
{body_text}


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


def _reindent_lines(lines: list[str], new_indent: str) -> list[str]:
    """Re-indent a source slice onto a new base indentation, preserving inner structure."""
    non_blank = [line for line in lines if line.strip()]
    base = min(len(line) - len(line.lstrip()) for line in non_blank) if non_blank else 0
    return [new_indent + line[base:] if line.strip() else "" for line in lines]


def _egress_guard_lines(indent: str) -> list[str]:
    """Render the Python-side outbound egress guard for the offline runner.

    The browser ``context.route`` only intercepts Playwright traffic; a marker
    block issuing a raw Python outbound call (``socket``, ``urllib.request``,
    ``requests``, ``http.client``) bypasses the route entirely. This guard
    patches ``socket.socket.connect``/``connect_ex`` — the funnel every Python
    network stack reaches — so any non-loopback TCP/UDP connection is recorded
    into the shared ``external_requests`` list and refused. Loopback
    (``127.0.0.1`` / ``localhost`` / ``::1`` / any loopback IP) and non-tuple
    (Unix-domain / local pipe) addresses — e.g. Playwright's own in-process
    driver socket — are always permitted so the local replica and the browser
    bridge keep working. The guard covers anything that resolves to a real
    ``(host, port)`` socket connect; it does not cover DNS lookups (resolved by
    the C resolver in-process before ``connect``) which by themselves open no
    network socket.
    """
    body = indent + "    "
    deep = indent + "        "
    return [
        f"{indent}def _is_loopback_egress(address):",
        f"{body}import ipaddress as _ip",
        # A non-tuple ``address`` is a Unix-domain / local pipe socket path
        # (Playwright's own in-process driver connection), which is inherently
        # local — always permit it. Only ``(host, port)`` tuples represent
        # network egress.
        f"{body}if not isinstance(address, tuple) or not address:",
        f"{deep}return True",
        f"{body}host = address[0]",
        f"{body}if host in (\"localhost\", \"127.0.0.1\", \"::1\"):",
        f"{deep}return True",
        f"{body}try:",
        f"{deep}return _ip.ip_address(host).is_loopback",
        f"{body}except ValueError:",
        f"{deep}return False",
        f"{indent}def _install_egress_guard(record):",
        f"{body}import socket as _sock",
        f"{body}def _wrap(method):",
        f"{deep}original = getattr(_sock.socket, method)",
        f"{deep}def guarded(self, address, *args, **kwargs):",
        f"{deep + indent}if not _is_loopback_egress(address):",
        f"{deep + body}record(address)",
        f'{deep + body}raise RuntimeError("offline_egress_blocked: non-loopback '
        f"Python outbound connection attempted: %r\" % (address,))",
        f"{deep + indent}return original(self, address, *args, **kwargs)",
        f"{deep}setattr(_sock.socket, method, guarded)",
        f"{body}_wrap(\"connect\")",
        f"{body}_wrap(\"connect_ex\")",
        f"{indent}_install_egress_guard(external_requests.append)",
    ]


def generate_offline_adapter_script(
    completed_source: str,
    replica_directory: str,
    validation_directory: str,
    capability_policy: Mapping[str, str] | None = None,
) -> str:
    """Rewrite a completed adapter into a portable, offline-capable runner.

    The live bootstrap (real URLs / network auth) is removed based on the parsed
    ``BootstrapPlan`` boundary; only statements after ``source_end_line`` are kept and
    grouped into per-marker blocks. Each block emits ``marker_started``/``marker_finished``
    events to ``validation/events.jsonl``. Active network isolation aborts and records
    every request outside the local replica origin, and the run fails with
    ``offline_external_request`` if any external request leaks through. Browser and the
    replica server are always closed in ``finally``.
    """
    tree = ast.parse(completed_source)
    plan = parse_action_plan(completed_source)
    bootstrap = plan.bootstrap
    if not bootstrap.skipped_in_offline_replay:
        raise ValueError("bootstrap.skipped_in_offline_replay must be True for offline rewrite")

    run_functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "run"]
    if len(run_functions) != 1:
        raise ValueError(f"expected exactly one top-level 'run' function, found {len(run_functions)}")

    source_end_line = bootstrap.source_end_line
    kept = [stmt for stmt in run_functions[0].body if stmt.lineno > source_end_line]

    groups = plan.marker_groups
    buckets: list[list[ast.stmt]] = [[] for _ in groups]
    for stmt in kept:
        group_index = -1
        for index, group in enumerate(groups):
            if group.source_line < stmt.lineno:
                group_index = index
            else:
                break
        if group_index >= 0:
            buckets[group_index].append(stmt)

    policy_by_label = {
        _normalize_label(label): mode for label, mode in (capability_policy or {}).items()
    }
    normalized_groups = [_normalize_label(group.marker_label) for group in groups]
    source_lines = completed_source.splitlines()

    indent = "    "
    inner = indent * 2
    block_indent = indent * 3

    block_lines: list[str] = []
    for index, group in enumerate(groups):
        bucket = buckets[index]
        if not bucket:
            continue
        label = group.marker_label
        mode = policy_by_label.get(normalized_groups[index], "supported")
        if mode not in {"supported", "degraded", "static-only"}:
            mode = "supported"
        marker_comment = (
            source_lines[group.source_line - 1]
            if 0 < group.source_line <= len(source_lines)
            else f"# [MARKER: {label}]"
        )
        if marker_comment.strip():
            block_lines.append(block_indent + marker_comment.lstrip())
        block_lines.append(f'{block_indent}emit({{"event": "marker_started", "marker": {label!r}}})')
        if mode == "static-only":
            block_lines.append(
                f'{block_indent}emit({{"event": "marker_degraded", "marker": {label!r}, '
                f'"capability": "canvas_dynamic_pixels"}})'
            )
            block_lines.append(
                f"{block_indent}# static-only: viewer-JS / dynamic-pixel execution skipped "
                f"(offline stage partial)"
            )
            block_lines.append(f"{block_indent}offline_stages.append(\"partial\")")
            block_lines.append(f'{block_indent}emit({{"event": "marker_finished", "marker": {label!r}, "status": "degraded"}})')
            continue
        if mode == "degraded":
            block_lines.append(f'{block_indent}emit({{"event": "marker_degraded", "marker": {label!r}}})')
        slice_lines: list[str] = []
        for stmt in bucket:
            slice_lines.extend(source_lines[stmt.lineno - 1 : stmt.end_lineno])
        reindented = _reindent_lines(slice_lines, block_indent)
        block_lines.extend(reindented if reindented else [f"{block_indent}pass"])
        block_lines.append(f'{block_indent}emit({{"event": "marker_finished", "marker": {label!r}, "status": {mode!r}}})')

    marker_body = "\n".join(block_lines) or f"{block_indent}pass"

    bindings_comments = "\n".join(
        f"{block_indent}# {page_var} is restored from local entry binding: {binding!r}"
        for page_var, binding in bootstrap.entry_page_bindings.items()
    )

    body_lines = [
        "def run() -> None:",
        # Marker blocks are copied verbatim and may issue their own
        # ``from pathlib import Path``, which would shadow ``Path`` as a
        # function-local across the WHOLE function (UnboundLocalError for the
        # reads below). Use the module-level ``_Path`` alias for the runner's
        # own path computations so marker-local shadowing cannot break them.
        f"{indent}replica_root = _Path(__file__).resolve().parent / {replica_directory!r}",
        f"{indent}validation_root = _Path(__file__).resolve().parent / {validation_directory!r}",
        f"{indent}validation_root.mkdir(parents=True, exist_ok=True)",
        f"{indent}external_requests: list = []",
        f"{indent}offline_stages: list = []",
        *_egress_guard_lines(indent),
        f"{indent}with ReplicaServer(replica_root) as server, sync_playwright() as playwright:",
        *_local_page_bootstrap_lines(inner, include_goto=False),
        f'{inner}allowed_origin = "/".join(server.url.split("/")[:3])',
        f"{inner}def _route(route):",
        f"{inner + indent}url = route.request.url",
        f"{inner + indent}parsed = _urlsplit(url)",
        f"{inner + indent}if parsed.scheme in (\"data\", \"blob\", \"about\") or url.startswith(allowed_origin):",
        f"{inner + indent + indent}route.continue_()",
        f"{inner + indent}else:",
        f"{inner + indent + indent}external_requests.append(url)",
        f"{inner + indent + indent}route.abort()",
        f'{inner}context.route("**/*", _route)',
        f"{inner}try:",
        f"{block_indent}page.goto(server.url)",
    ]
    if bindings_comments:
        body_lines.append(bindings_comments)
    body_lines.append(marker_body)
    body_lines.extend(
        [
            f"{inner}except BaseException:",
            # A marker that raised may have tripped the Python egress guard
            # (``offline_egress_blocked``), which records into
            # ``external_requests`` but does not itself write the file. Flush
            # the record and fail the run as ``offline_external_request`` so
            # the zero-external-request gate covers the Python channel too;
            # any other marker failure (external_requests empty) re-raises.
            f"{block_indent}if external_requests:",
            f"{block_indent + indent}validation_root.joinpath(\"external_requests.json\").write_text("
            f"json.dumps(external_requests, ensure_ascii=False), encoding=\"utf-8\")",
            f'{block_indent + indent}raise RuntimeError("offline_external_request")',
            f"{block_indent}raise",
            f"{inner}finally:",
            f"{block_indent}browser.close()",
            f'{indent}validation_root.joinpath("external_requests.json").write_text('
            f'json.dumps(external_requests, ensure_ascii=False), encoding="utf-8")',
            f'{indent}if "partial" in offline_stages:',
            f'{block_indent}print("offline stage: partial", flush=True)',
            f"{indent}if external_requests:",
            f'{block_indent}raise RuntimeError("offline_external_request")',
        ]
    )

    generated = "\n".join(
        [
            "from pathlib import Path as _Path",
            "import json",
            "import sys as _sys_path",
            "import urllib.parse",
            # Marker blocks may shadow ``urllib`` (e.g. ``import
            # urllib.request``) as a function-local, which would break the
            # route handler's ``urllib.parse`` lookup (same shadowing hazard as
            # the ``Path`` case). Bind a stable module-level alias up front.
            "_urlsplit = urllib.parse.urlsplit",
            "from playwright.sync_api import sync_playwright",
            # The offline runner lives in the adapter dir, but its two runtime
            # dependencies do not: ``serve_replica`` ships with the built replica
            # (``replica_directory``) and ``skills._shared`` lives at the project
            # root. Insert both onto ``sys.path`` before importing them.
            "_this_dir = _Path(__file__).resolve().parent",
            f"_replica_root_abs = _Path({replica_directory!r})",
            "_cursor = _this_dir",
            "while True:",
            "    if (_cursor / \"skills\" / \"_shared\").is_dir():",
            "        if str(_cursor) not in _sys_path.path:",
            "            _sys_path.path.insert(0, str(_cursor))",
            "        break",
            "    if _cursor.parent == _cursor:",
            "        break",
            "    _cursor = _cursor.parent",
            "if str(_replica_root_abs) not in _sys_path.path:",
            "    _sys_path.path.insert(0, str(_replica_root_abs))",
            "from serve_replica import ReplicaServer",
            "",
            "",
            "def emit(event):",
            f"{indent}events_path = _Path(__file__).resolve().parent / {validation_directory!r} / \"events.jsonl\"",
            f"{indent}events_path.parent.mkdir(parents=True, exist_ok=True)",
            f'{indent}with events_path.open("a", encoding="utf-8") as handle:',
            f"{indent + indent}handle.write(json.dumps(event, ensure_ascii=False) + \"\\n\")",
            "",
            "",
            *body_lines,
            "",
            "",
            'if __name__ == "__main__":',
            f"{indent}run()",
        ]
    )
    ast.parse(generated)
    return generated


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
