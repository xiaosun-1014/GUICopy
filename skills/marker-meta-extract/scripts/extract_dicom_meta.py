"""Meta 信息提取 marker 处理脚本。

读取 Playwright 录制脚本，找到 `# [MARKER: Meta 信息工具 @ {ts}]` 标记，
按 viewers.yaml 配置生成「viewer 适配 → DOM 多策略提取 → VL 回退 → 校验 → 落盘」的完整替换代码。

viewer-agnostic：所有 iframe/按钮/面板/正则从 viewers.yaml 读取。
不引入 PyYAML 依赖，使用自带的最小化 YAML 解析器（仅覆盖本项目用到的子集）。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ============================================================================
# 最小化 YAML 解析器（仅支持 viewers.yaml 需要的语法子集）
# ============================================================================
# 支持：
#   - 缩进表示嵌套（空格缩进）
#   - key: value
#   - 列表：- item
#   - 字符串：裸串 / 单引号 / 双引号
#   - 注释 # ...
#   - 空行
# 不支持：多文档、锚点、复杂 flow 语法、Unicode 转义
# ============================================================================

class MiniYaml:
    """最小化 YAML 解析器。"""

    @staticmethod
    def _strip_comment(line: str) -> str:
        """去掉行末 # 注释（保留字符串内的 #）。"""
        in_single = False
        in_double = False
        for i, ch in enumerate(line):
            if ch == "'" and not in_double:
                in_single = not in_single
            elif ch == '"' and not in_single:
                in_double = not in_double
            elif ch == "#" and not in_single and not in_double:
                return line[:i].rstrip()
        return line.rstrip()

    @staticmethod
    def _parse_value(raw: str) -> Any:
        raw = raw.strip()
        if not raw:
            return None
        if raw in ("true", "True", "yes"):
            return True
        if raw in ("false", "False", "no"):
            return False
        if raw in ("null", "Null", "~", ""):
            return None
        # 数字
        if re.match(r"^-?\d+$", raw):
            return int(raw)
        if re.match(r"^-?\d+\.\d+$", raw):
            return float(raw)
        # Inline flow lists used by the viewer registry (for example ``[]``
        # and ``["id"]``).  Split only at top-level commas so quoted values
        # and nested flow values remain intact.
        if raw.startswith("[") and raw.endswith("]"):
            inner = raw[1:-1].strip()
            if not inner:
                return []
            return [MiniYaml._parse_value(item) for item in MiniYaml._split_flow_items(inner)]
        # 引号字符串
        if (raw.startswith('"') and raw.endswith('"')) or \
           (raw.startswith("'") and raw.endswith("'")):
            return raw[1:-1]
        # 裸字符串
        return raw

    @staticmethod
    def _split_flow_items(raw: str) -> List[str]:
        """Split a small YAML flow sequence at top-level commas."""
        items: List[str] = []
        current: List[str] = []
        quote: Optional[str] = None
        escaped = False
        depth = 0
        for ch in raw:
            if quote is not None:
                current.append(ch)
                if escaped:
                    escaped = False
                elif ch == "\\" and quote == '"':
                    escaped = True
                elif ch == quote:
                    quote = None
                continue
            if ch in ('"', "'"):
                quote = ch
                current.append(ch)
            elif ch in "[{":
                depth += 1
                current.append(ch)
            elif ch in "]}":
                depth = max(0, depth - 1)
                current.append(ch)
            elif ch == "," and depth == 0:
                items.append("".join(current).strip())
                current = []
            else:
                current.append(ch)
        items.append("".join(current).strip())
        return items

    @staticmethod
    def parse(text: str) -> Dict[str, Any]:
        """解析 YAML 文本为 Python 字典。"""
        lines = []
        for line in text.splitlines():
            stripped = MiniYaml._strip_comment(line)
            if not stripped.strip():
                continue
            indent = len(stripped) - len(stripped.lstrip(" "))
            lines.append((indent, stripped.lstrip(" ")))

        return MiniYaml._parse_block(lines, 0, 0)[0]

    @staticmethod
    def _parse_block(lines: List[Tuple[int, str]], start: int, base_indent: int) -> Tuple[Any, int]:
        """解析一个块（dict 或 list），返回 (结果, 消费的行数)。"""
        if start >= len(lines):
            return {}, start

        first_indent, first_content = lines[start]
        if first_indent < base_indent:
            return {}, start

        # 列表？
        if first_content.startswith("- "):
            return MiniYaml._parse_list(lines, start, first_indent)
        return MiniYaml._parse_dict(lines, start, first_indent)

    @staticmethod
    def _parse_dict(lines: List[Tuple[int, str]], start: int, indent: int) -> Tuple[Dict[str, Any], int]:
        result: Dict[str, Any] = {}
        i = start
        while i < len(lines):
            cur_indent, cur_content = lines[i]
            if cur_indent < indent:
                break
            if cur_indent > indent:
                # 异常缩进，跳过避免死循环
                i += 1
                continue
            if ":" not in cur_content:
                i += 1
                continue
            key, _, value_part = cur_content.partition(":")
            key = key.strip()
            value_part = value_part.strip()
            if value_part == "":
                # 嵌套结构
                if i + 1 < len(lines) and lines[i + 1][0] > indent:
                    child, consumed = MiniYaml._parse_block(lines, i + 1, lines[i + 1][0])
                    result[key] = child
                    i = consumed
                else:
                    result[key] = {}
                    i += 1
            else:
                result[key] = MiniYaml._parse_value(value_part)
                i += 1
        return result, i

    @staticmethod
    def _parse_list(lines: List[Tuple[int, str]], start: int, indent: int) -> Tuple[List[Any], int]:
        result: List[Any] = []
        i = start
        while i < len(lines):
            cur_indent, cur_content = lines[i]
            if cur_indent < indent:
                break
            if cur_indent > indent:
                i += 1
                continue
            if not cur_content.startswith("- "):
                break
            item_content = cur_content[2:].strip()
            if ":" in item_content and not item_content.startswith('"'):
                # 列表项是 dict：'- key: value'（或嵌套）
                # 重组成 dict 块
                # 简单做法：把 '- key: value' 转成 'key: value' 行
                rebuilt = [(cur_indent, item_content)]
                j = i + 1
                while j < len(lines) and lines[j][0] > indent:
                    j += 1
                if j > i + 1:
                    # Continuation keys are indented farther than the list
                    # item.  Derive that amount from this item instead of
                    # assuming a particular YAML indentation style.  Deeper
                    # nested mappings retain their relative indentation.
                    continuation_base = min(lines[k][0] for k in range(i + 1, j))
                    dedent = max(0, continuation_base - cur_indent)
                    for k in range(i + 1, j):
                        continuation_indent, continuation_content = lines[k]
                        rebuilt.append((continuation_indent - dedent, continuation_content))
                child_dict, _ = MiniYaml._parse_dict(rebuilt, 0, cur_indent)
                result.append(child_dict)
                i = j
            else:
                result.append(MiniYaml._parse_value(item_content))
                i += 1
        return result, i


# ============================================================================
# Viewer 注册表加载与匹配
# ============================================================================

@dataclass
class ViewerConfig:
    name: str
    url_patterns: List[str]
    iframe_selectors: List[str]
    meta_panel: Dict[str, Any]
    sequence_select: Dict[str, Any]


def load_viewers(path: Path) -> Dict[str, ViewerConfig]:
    """加载 viewers.yaml，返回 name → ViewerConfig 映射。"""
    raw = MiniYaml.parse(path.read_text(encoding="utf-8"))
    viewers_raw = raw.get("viewers", {})
    result: Dict[str, ViewerConfig] = {}
    for name, cfg in viewers_raw.items():
        if not isinstance(cfg, dict):
            continue
        result[name] = ViewerConfig(
            name=name,
            url_patterns=cfg.get("url_patterns") or [],
            iframe_selectors=cfg.get("iframe_selectors") or [],
            meta_panel=cfg.get("meta_panel", {}) or {},
            sequence_select=cfg.get("sequence_select", {}) or {},
        )
    return result


def match_viewer(viewers: Dict[str, ViewerConfig], goto_urls: List[str]) -> ViewerConfig:
    """按 URL 匹配 viewer，未命中返回 generic。"""
    for url in goto_urls:
        for name, cfg in viewers.items():
            if name == "generic":
                continue
            for pat in cfg.url_patterns:
                if pat in url:
                    return cfg
    return viewers.get("generic") or next(iter(viewers.values()))


# ============================================================================
# 录制脚本解析
# ============================================================================

MARKER_PATTERN = re.compile(
    r"#\s*\[MARKER:\s*Meta\s+信息工具\s+@\s*(\d{8}_\d{6})\]\s*$"
)

GOTO_PATTERN = re.compile(r'page\.goto\(\s*["\']([^"\']+)["\']')
LOCATOR_PATTERN = re.compile(r'\.locator\(\s*["\']([^"\']+)["\']')
PAGE_VAR_PATTERN = re.compile(r'\b(page\d*)\s*=')


@dataclass
class MarkerContext:
    ts: str
    line_no: int
    page_var: str
    goto_urls: List[str]
    existing_locators: List[str]


def find_markers(script_text: str, context_radius: int = 5) -> List[MarkerContext]:
    """扫描录制脚本，提取所有 Meta 信息工具 marker 及其上下文。

    goto_urls / page_vars 在全文范围内收集（URL 通常在文件开头，marker 在中间）。
    existing_locators 只在 marker 附近窗口收集（避免被无关 locator 污染）。
    """
    lines = script_text.splitlines()
    all_goto_urls = GOTO_PATTERN.findall(script_text)
    all_page_vars = PAGE_VAR_PATTERN.findall(script_text)
    results: List[MarkerContext] = []
    for i, line in enumerate(lines):
        m = MARKER_PATTERN.search(line)
        if not m:
            continue
        ts = m.group(1)
        lo = max(0, i - context_radius)
        hi = min(len(lines), i + context_radius + 1)
        window = "\n".join(lines[lo:hi])
        page_vars = PAGE_VAR_PATTERN.findall(window)
        page_var = page_vars[-1] if page_vars else (all_page_vars[-1] if all_page_vars else "page")
        existing_locators = LOCATOR_PATTERN.findall(window)
        results.append(MarkerContext(
            ts=ts,
            line_no=i,
            page_var=page_var,
            goto_urls=all_goto_urls,
            existing_locators=existing_locators,
        ))
    return results


# ============================================================================
# iframe 选择器反推
# ============================================================================

# 常见的 iframe 选择器特征（从录制脚本中识别）
IFRAME_HINTS = ("iframe", "frame", "viewer")


def infer_iframe_selector(viewer: ViewerConfig, existing_locators: List[str]) -> str:
    """从已有 locator 中反推 iframe 选择器，回退到 viewer 配置。"""
    for loc in existing_locators:
        loc_lower = loc.lower()
        if any(h in loc_lower for h in IFRAME_HINTS):
            return loc
    if viewer.iframe_selectors:
        return viewer.iframe_selectors[0]
    # generic 兜底：从已有 locator 里挑第一个 id="..." 选择器
    for loc in existing_locators:
        if loc.startswith("[id="):
            return loc
    return ""


# ============================================================================
# 代码生成
# ============================================================================

def _python_literal(s: str) -> str:
    """把字符串转成 Python 字面量（带双引号转义）。"""
    return json.dumps(s, ensure_ascii=False)


def _locator_expression(target: str, recipe: Any) -> str:
    """Return a Playwright locator expression from an ``open_steps`` recipe.

    ``open_steps`` is intentionally data-driven.  A recipe may be a plain CSS
    selector, or a small mapping describing one of the accessible Playwright
    locator methods.  Supporting both forms keeps the shared viewer registry
    readable while preserving compatibility with future viewers.
    """
    if isinstance(recipe, str):
        return f"{target}.locator({_python_literal(recipe)})"
    if not isinstance(recipe, dict):
        raise ValueError("open_steps locator recipe must be a string or mapping")

    method = str(recipe.get("method") or recipe.get("kind") or "locator").lower()
    if method in {"locator", "css", "selector"}:
        selector = recipe.get("selector")
        if selector is None:
            selector = recipe.get("value")
        if not isinstance(selector, str) or not selector:
            raise ValueError("locator recipe requires a non-empty selector")
        return f"{target}.locator({_python_literal(selector)})"
    if method in {"get_by_title", "title"}:
        value = recipe.get("value", recipe.get("name"))
        if not isinstance(value, str) or not value:
            raise ValueError("get_by_title recipe requires a non-empty value")
        return f"{target}.get_by_title({_python_literal(value)})"
    if method in {"get_by_text", "text"}:
        value = recipe.get("value", recipe.get("text", recipe.get("name")))
        if not isinstance(value, str) or not value:
            raise ValueError("get_by_text recipe requires a non-empty value")
        return f"{target}.get_by_text({_python_literal(value)})"
    if method in {"get_by_role", "role"}:
        role = recipe.get("role")
        name = recipe.get("name", recipe.get("value"))
        if not isinstance(role, str) or not role:
            raise ValueError("get_by_role recipe requires a non-empty role")
        if not isinstance(name, str) or not name:
            raise ValueError("get_by_role recipe requires a non-empty name")
        exact = recipe.get("exact")
        exact_arg = f", exact={bool(exact)!r}" if exact is not None else ""
        return (
            f"{target}.get_by_role({_python_literal(role)}, "
            f"name={_python_literal(name)}{exact_arg})"
        )
    raise ValueError(f"unsupported open_steps locator method: {method}")


def _step_recipe(step: Any, key: str) -> Any:
    """Read a click/expect recipe, accepting concise compatibility aliases."""
    if not isinstance(step, dict):
        raise ValueError("each open_steps item must be a mapping")
    recipe = step.get(key)
    if recipe is not None:
        return recipe
    if key == "click":
        recipe = step.get("locator")
        if recipe is None:
            recipe = step.get("selector")
        if recipe is not None:
            return recipe
        if step.get("method") or step.get("kind"):
            return step
    if key == "expect_visible":
        recipe = step.get("wait_for")
        if recipe is not None:
            return recipe
    raise ValueError(f"open_steps item requires {key}")


def _open_steps_code(
    frame_var: str,
    open_steps: List[Any],
    wait_timeout_ms: int = 10000,
) -> List[str]:
    """Generate a strict, fail-fast multi-step panel-open sequence.

    Every step resolves a fresh locator, waits for it to become visible (the
    subsequent click supplies Playwright's actionable check), clicks it, then
    resolves a fresh expectation locator and waits for that locator to become
    visible.  There is deliberately no catch/fallback here: a failed step must
    fail the marker rather than being mistaken for a successfully opened panel.
    """
    result: List[str] = []
    for index, step in enumerate(open_steps):
        click_recipe = _step_recipe(step, "click")
        expect_recipe = _step_recipe(step, "expect_visible")
        click_var = f"_meta_open_step_{index}"
        expect_var = f"_meta_open_expect_{index}"
        result.append(f"{click_var} = {_locator_expression(frame_var, click_recipe)}")
        result.append(
            f'{click_var}.wait_for(state="visible", timeout={wait_timeout_ms})'
        )
        result.append(f'{click_var}.click(timeout={wait_timeout_ms})')
        result.append(f"{expect_var} = {_locator_expression(frame_var, expect_recipe)}")
        result.append(
            f'{expect_var}.wait_for(state="visible", timeout={wait_timeout_ms})'
        )
    return result


def generate_replacement_code(ctx: MarkerContext, viewer: ViewerConfig) -> str:
    """生成替换 marker 块的完整代码。"""
    page = ctx.page_var
    iframe = infer_iframe_selector(viewer, ctx.existing_locators)
    open_steps = viewer.meta_panel.get("open_steps", []) or []
    button_names = viewer.meta_panel.get("open_button_names", []) or []
    panel_selectors = viewer.meta_panel.get("panel_container_selectors", []) or []
    tag_format = viewer.meta_panel.get("tag_row_format", "flex_div")
    tag_pattern = viewer.meta_panel.get("tag_pattern", r"\(?\d{4}[,\)\s-]\s*\d{4}\)?")
    ts = ctx.ts

    frame_var = "frame" if iframe else page

    lines: List[str] = []
    lines.append(f"# [MARKER: Meta 信息工具 @ {ts}]")
    lines.append(f"# viewer={viewer.name}, iframe={iframe or '(从录制脚本反推)'}")

    # 打开面板
    if iframe:
        lines.append(f"{frame_var} = {page}.locator({_python_literal(iframe)}).content_frame")
    if open_steps:
        # A configured sequence is authoritative.  Do not catch a failed
        # step or fall through to button-name aliases: the next state may not
        # have been reached and extraction must not report a false success.
        lines.extend(_open_steps_code(frame_var, open_steps))
    elif button_names:
        # 多按钮依次尝试：找到能点的就停，全失败就跳过（面板可能已开）
        lines.append("opened = False")
        lines.append("for _btn_name in [")
        for i, btn in enumerate(button_names):
            sep = "," if i < len(button_names) - 1 else ""
            lines.append(f"    {_python_literal(btn)}{sep}")
        lines.append("]:")
        lines.append("    try:")
        lines.append(f"        {frame_var}.get_by_role(\"button\", name=_btn_name).click(timeout=3000)")
        lines.append("        opened = True")
        lines.append("        break")
        lines.append("    except Exception:")
        lines.append("        continue")
        lines.append("if not opened:")
        lines.append("    pass  # 面板可能已经打开或按钮文案全部不匹配，继续尝试 DOM 提取")
    lines.append(f"{page}.wait_for_timeout(1000)")

    # DOM 提取（按 tag_row_format 选策略）
    lines.extend(_dom_extraction_code(frame_var, panel_selectors, tag_format, tag_pattern))

    # VL 回退
    lines.append("if len(rows) < 10:  # DOM 提取失败")
    lines.append(f"    {page}.screenshot(path={_python_literal('dicom_panel.jpeg')}, full_page=True)")
    lines.append("    # 调用 VL 模型识别 → rows = parse_vl_response(vl_result)")
    lines.append("    # VL prompt 见 marker-meta-extract/references/vl_prompt.md")

    # 校验 + 落盘
    lines.append("try:")
    lines.append("    from marker_meta_info_skill import validate_metadata")
    lines.append("    validation = validate_metadata(rows)")
    lines.append("except Exception as e:")
    lines.append("    validation = {'error': str(e)}")
    lines.append("")
    lines.append("output = {")
    lines.append(f"    'extracted_at': {_python_literal(ts)},")
    lines.append(f"    'viewer': {_python_literal(viewer.name)},")
    lines.append("    'source': 'dom' if len(rows) >= 10 else 'vl',")
    lines.append("    'total_tags': len(rows),")
    lines.append("    'tags': rows,")
    lines.append("    'validation': validation,")
    lines.append("}")
    lines.append(f"with open({_python_literal('dicom_meta.json')}, 'w', encoding='utf-8') as f:")
    lines.append("    json.dump(output, f, ensure_ascii=False, indent=2)")
    return "\n".join(lines)


def _dom_extraction_code(frame_var: str, panel_selectors: List[str], tag_format: str, tag_pattern: str) -> List[str]:
    """根据 tag_row_format 生成对应的 DOM 提取代码（Python locator 版，兼容 FrameLocator）。"""
    result = [
        "import re",
        f"_TAG_RE = re.compile({_python_literal(tag_pattern)})",
        "rows = []",
    ]

    if tag_format == "table_tr_td":
        _table_tr_td_code(result, frame_var, panel_selectors)
    elif tag_format == "flex_div":
        _flex_div_code(result, frame_var)
    elif tag_format == "tree_node":
        _flex_div_code(result, frame_var)  # tree_node 也用 body inner_text 兜底
    else:
        _flex_div_code(result, frame_var)  # 未知格式默认兜底
    return result


def _table_tr_td_code(result: List[str], frame_var: str, panel_selectors: List[str]) -> None:
    """生成 table > tr > td 的 Python locator 提取代码。"""
    if panel_selectors:
        containers = ", ".join(_python_literal(s) for s in panel_selectors)
        result.append(f"for _sel_ in [{containers}]:")
        result.append(f"    for _tr_ in {frame_var}.locator(_sel_ + ' tr').all():")
        result.append(f"        _cells_ = _tr_.locator('td').all()")
        result.append(f"        if len(_cells_) >= 3:")
        result.append(f"            _tag_ = _cells_[0].text_content().strip()")
        result.append(f"            if _TAG_RE.match(_tag_):")
        result.append(f"                rows.append({{")
        result.append(f"                    'tag': _tag_,")
        result.append(f"                    'desc': _cells_[1].text_content().strip(),")
        result.append(f"                    'value': _cells_[2].text_content().strip(),")
        result.append(f"                }})")
    else:
        result.append(f"for _tr_ in {frame_var}.locator('table tr').all():")
        result.append(f"    _cells_ = _tr_.locator('td').all()")
        result.append(f"    if len(_cells_) >= 3:")
        result.append(f"        _tag_ = _cells_[0].text_content().strip()")
        result.append(f"        if _TAG_RE.match(_tag_):")
        result.append(f"            rows.append({{")
        result.append(f"                'tag': _tag_,")
        result.append(f"                'desc': _cells_[1].text_content().strip(),")
        result.append(f"                'value': _cells_[2].text_content().strip(),")
        result.append(f"            }})")


def _flex_div_code(result: List[str], frame_var: str) -> None:
    """生成 flex_div / tree_node 的 Python locator 提取代码（body inner_text + 正则）。"""
    result.append(f"_body_text = {frame_var}.locator('body').inner_text()")
    result.append("_seen = set()")
    result.append("for _line in _body_text.split('\\n'):")
    result.append("    _line = _line.strip()")
    result.append("    if not _line or len(_line) < 10:")
    result.append("        continue")
    result.append("    _m = _TAG_RE.search(_line)")
    result.append("    if not _m:")
    result.append("        continue")
    result.append("    _tag = _m.group(0)")
    result.append("    if _tag in _seen:")
    result.append("        continue")
    result.append("    _seen.add(_tag)")
    result.append("    _remainder = _line.replace(_tag, '', 1).strip().lstrip(':').strip()")
    result.append("    _parts = [p.strip() for p in re.split(r'[\\t]', _remainder, maxsplit=2) if p.strip()]")
    result.append("    rows.append({'tag': _tag, 'desc': _parts[0] if len(_parts) > 0 else '', 'value': _parts[1] if len(_parts) > 1 else ''})")


# ============================================================================
# 录制脚本替换
# ============================================================================

def patch_script(script_text: str, viewers: Dict[str, ViewerConfig]) -> Tuple[str, List[Dict[str, Any]]]:
    """替换脚本中所有 Meta 信息工具 marker，返回 (新文本, 处理报告)。"""
    markers = find_markers(script_text)
    if not markers:
        return script_text, []

    lines = script_text.splitlines()
    # 从后往前替换，避免 line_no 失效
    report: List[Dict[str, Any]] = []
    for ctx in reversed(markers):
        viewer = match_viewer(viewers, ctx.goto_urls)
        replacement = generate_replacement_code(ctx, viewer)
        # 替换 marker 行 + 紧跟的下一行（marker 模板自带 # TODO 行）
        end_line = ctx.line_no + 1
        lines[ctx.line_no:end_line + 1] = [replacement]
        report.append({
            "ts": ctx.ts,
            "line_no": ctx.line_no + 1,
            "viewer": viewer.name,
            "iframe": infer_iframe_selector(viewer, ctx.existing_locators),
        })

    report.reverse()
    return "\n".join(lines) + "\n", report


def patch_script_with_meta_extraction(
    script_path: str,
    viewers_path: str = "skills/_shared/viewers.yaml",
    marker_ts: Optional[str] = None,
) -> str:
    """便捷函数：读脚本 → 替换 → 返回新文本。"""
    viewers = load_viewers(Path(viewers_path))
    text = Path(script_path).read_text(encoding="utf-8")
    new_text, report = patch_script(text, viewers)
    if marker_ts:
        report = [r for r in report if r["ts"] == marker_ts]
    return new_text


# ============================================================================
# CLI
# ============================================================================

def main() -> int:
    parser = argparse.ArgumentParser(description="补全 Meta 信息工具 marker")
    parser.add_argument("--script", required=True, help="录制脚本路径（如 processed_script.py）")
    parser.add_argument("--viewers", default="skills/_shared/viewers.yaml", help="viewer 注册表路径")
    parser.add_argument("--output-dir", default="out/", help="输出目录")
    parser.add_argument("--marker-ts", default=None, help="只处理指定 ts 的 marker（默认全部）")
    args = parser.parse_args()

    viewers = load_viewers(Path(args.viewers))
    text = Path(args.script).read_text(encoding="utf-8")
    new_text, report = patch_script(text, viewers)

    if args.marker_ts:
        report = [r for r in report if r["ts"] == args.marker_ts]
        if not report:
            print(f"[warn] 未找到 ts={args.marker_ts} 的 marker", file=sys.stderr)

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    patched_path = out_dir / "patched_script.py"
    patched_path.write_text(new_text, encoding="utf-8")

    report_path = out_dir / "patch_report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"[ok] 处理 {len(report)} 个 marker")
    for r in report:
        print(f"  - ts={r['ts']} viewer={r['viewer']} iframe={r['iframe']}")
    print(f"[ok] 输出: {patched_path}")
    print(f"[ok] 报告: {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
