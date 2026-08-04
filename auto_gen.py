#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""auto_gen.py — 从录制脚本自动生成全自动采图脚本（无需 LLM，无需 viewers.yaml）。

用法:
    python auto_gen.py --input out/cxhospital/processed_script_cxhospital.py \
                       --output out/cxhospital/auto_capture_cxhospital.py

原理:
    1. 读取 processed_script.py（GUI 录制产出）
    2. 从录制代码中自动提取：iframe 嵌套路径、协议名、DICOM 按钮、canvas 坐标、URL
    3. 生成 auto_capture.py：import 共享模块 + 保留导航操作 + marker 块替换
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# marker 正则（与 agent.py 一致）
MARKER_RE = re.compile(
    r"^(?P<indent>[ \t]*)# \[MARKER: (?P<name>[^\]]+?)(?: @ (?P<ts>\d{8}_\d{6}))?\]"
)

# marker → 保留原始录制代码
KEEP_ORIGINAL = {"序列布局切换", "窗宽窗位 WL/WW"}

# 每个 marker 之后自动追加的等待（毫秒）
# 解决录制脚本没有 wait_for_timeout 导致回放时步骤太快的问题
POST_MARKER_WAITS = {
    "报告截图": 2000,          # 报告页稳定 → 进入 viewer 前
    "序列布局切换": 1000,      # 布局切换后等 DOM 稳定
    "序列选择": 1500,          # 序列加载
    "窗宽窗位 WL/WW": 1000,    # 窗宽窗位生效
}

# ==================================================================
# 配置提取
# ==================================================================

def extract_url(script: str) -> str:
    m = re.search(r'page\.goto\("([^"]+)"\)', script)
    return m.group(1) if m else ""


def extract_iframe_selectors(script: str) -> list[str]:
    """提取 iframe 嵌套路径。匹配 .locator(X).content_frame — X 就是 iframe 选择器。"""
    selectors: list[str] = []
    for m in re.finditer(r'\.locator\(([^)]+)\)\.content_frame', script):
        sel = _clean_selector(m.group(1))
        if sel not in selectors:
            selectors.append(sel)
    return selectors


def extract_frame_count(protocol_name: str) -> int | None:
    """从协议名中提取帧数。如 'x 1.0 AIIR_LungMPR205362幅' → 362。"""
    if not protocol_name:
        return None
    # 匹配 "362幅" / "205 images" 等显式标记
    for pat in (
        r"(\d{2,4})\s*(?:张|幅|层|帧|images?|imgs?|slices?|frames?)",
        r"(?:切片数|图像数|张数|层数|帧数|Images?|Slices?|Frames?)\s*[:：]?\s*(\d{2,4})",
    ):
        matches = re.findall(pat, protocol_name, re.I)
        if matches:
            candidates = [int(m) for m in matches if 50 <= int(m) <= 2000]
            if candidates:
                return max(candidates)
    # 匹配裸 ≥50 的 3-4 位数字
    nums = [int(m) for m in re.findall(r"\b(\d{3,4})\b", protocol_name) if 50 <= int(m) <= 2000]
    if nums:
        return max(nums)
    return None


def extract_protocol_name(script: str) -> str:
    lines = script.split("\n")
    marker_line = -1
    for i, line in enumerate(lines):
        if "序列选择" in line and "[MARKER:" in line:
            marker_line = i
            break

    if marker_line >= 0:
        for line in lines[marker_line:min(len(lines), marker_line + 8)]:
            m = re.search(r'get_by_text\(["\']([^"\']+)["\']\)', line)
            if m:
                return m.group(1)
        for line in lines[max(0, marker_line - 5):marker_line]:
            m = re.search(r'get_by_text\(["\']([^"\']+)["\']\)', line)
            if m:
                return m.group(1)

    for line in lines:
        m = re.search(r'get_by_text\(["\']([^"\']+)["\']\)', line)
        if m:
            return m.group(1)
    return ""


def extract_dicom_button(script: str) -> str:
    """提取 DICOM 按钮选择器。"""
    m = re.search(r'locator\(["\']([^"\']*[Dd]icom[^"\']*)["\']\)', script)
    if m:
        return _clean_selector(m.group(1))
    m = re.search(r'get_by_role\(["\']button["\']\s*,\s*name=["\']([^"\']*[Dd][Ii][Cc][Oo][Mm][^"\']*)["\']\)', script)
    if m:
        return m.group(1)
    return ""


def extract_canvas_coords(script: str) -> tuple[int, int]:
    m = re.search(r'position=\{"x":(\d+),"y":(\d+)\}', script)
    if m:
        return int(m.group(1)), int(m.group(2))
    return 0, 0


def extract_viewer_page_var(script: str) -> str:
    """检测 viewer 所用的页面变量。uicloud 用 page1（popup），cxhospital 用 page。"""
    if re.search(r'page1\s*=\s*page1_info\.value', script) or re.search(r'page1\.locator', script):
        return "page1"
    return "page"


def extract_viewport(script: str) -> tuple[int, int]:
    m = re.search(r'new_context\(viewport=\{"width":(\d+),"height":(\d+)\}', script)
    if m:
        return int(m.group(1)), int(m.group(2))
    return 1904, 1000


def _clean_selector(raw: str) -> str:
    s = raw.strip().strip("'\"").replace('\\"', '"').replace("\\'", "'")
    return s


# ==================================================================
# 脚本生成
# ==================================================================

def _maybe_add_wait(out: list[str], marker_name: str, page_var: str) -> None:
    """如果 marker 在 POST_MARKER_WAITS 中，追加 wait_for_timeout。"""
    ms = POST_MARKER_WAITS.get(marker_name)
    if ms:
        out.append(_indent_line(f"{page_var}.wait_for_timeout({ms})"))


def _detect_indent(s: str) -> int:
    i = 0
    while i < len(s) and s[i] in (" ", "\t"):
        i += 1
    return i


OUTPUT_BASE_INDENT = 8  # 生成脚本中函数体的基础缩进（with sync_playwright 内）


def _indent_line(stripped: str, extra: int = 0) -> str:
    """返回 OUTPUT_BASE_INDENT + extra 空格的缩进行。"""
    return " " * (OUTPUT_BASE_INDENT + extra) + stripped


def _format_iframe(selectors: list[str]) -> str:
    if not selectors:
        return "None"
    return "[" + ", ".join(repr(s) for s in selectors) + "]"


def _generate_header(script_name: str) -> str:
    return f'''# -*- coding: utf-8 -*-
"""auto_capture{script_name}.py — 全自动采图（由 auto_gen.py 生成）

无需 LLM、无需 viewers.yaml。直接 python 执行即可。
"""
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT = SCRIPT_DIR.parent.parent  # out/{{hospital}}/ → out/ → 项目根
sys.path.insert(0, str(_PROJECT))
from skills._shared.canvas_capture import capture_canvas_interaction
from skills._shared.meta_extract import extract_meta_from_frame
from skills._shared.meta_validate import validate_and_save
'''


def _generate_run_header(viewport_w: int, viewport_h: int) -> str:
    return f'''
def run():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        context = browser.new_context(viewport={{"width": {viewport_w}, "height": {viewport_h}}})
        page = context.new_page()

        # ══════════════════════════════════════════
        # 导航（从录制脚本保留）
        # ══════════════════════════════════════════
'''


def _generate_footer() -> str:
    return '''
        # ══════════════════════════════════════════
        browser.close()
        print("[auto_capture] 完成")

if __name__ == "__main__":
    run()
'''


# 跳过行检查（不在生成脚本中输出的原始脚本行）
SKIP_PATTERNS = [
    re.compile(r'\s*(browser|context|page)\s*='),   # 已在 header 生成
]
SKIP_EXACT = {
    "context.close()", "browser.close()",
    "# ---------------------",
    "with sync_playwright() as playwright:",
    "run(playwright)",
}


def generate(script: str, script_name: str = "",
             user_total_frames: int | None = None) -> str:
    """从 processed_script.py 生成 auto_capture.py。"""
    config = {
        "url": extract_url(script),
        "iframe_selectors": extract_iframe_selectors(script),
        "protocol_name": extract_protocol_name(script),
        "dicom_button": extract_dicom_button(script),
        "page_var": extract_viewer_page_var(script),
        "frame_count": user_total_frames or extract_frame_count(extract_protocol_name(script)),
    }
    canvas_x, canvas_y = extract_canvas_coords(script)
    viewport_w, viewport_h = extract_viewport(script)
    iframe_repr = _format_iframe(config["iframe_selectors"])
    pv = config["page_var"]
    fc = config["frame_count"]  # "page" or "page1"

    print(f"[auto_gen] 提取配置:", file=sys.stderr)
    print(f"  URL:         {config['url'][:80]}...", file=sys.stderr)
    print(f"  iframe:      {config['iframe_selectors']}", file=sys.stderr)
    print(f"  协议名:      {config['protocol_name'] or '(未检测到)'}", file=sys.stderr)
    print(f"  DICOM按钮:   {config['dicom_button'] or '(未检测到)'}", file=sys.stderr)
    print(f"  canvas坐标:  x={canvas_x}, y={canvas_y}", file=sys.stderr)
    print(f"  viewer变量:  {pv}", file=sys.stderr)
    print(f"  帧数(协议):  {fc or '未提取'}", file=sys.stderr)

    lines = script.split("\n")
    out: list[str] = []
    out.append(_generate_header(script_name))
    out.append(_generate_run_header(viewport_w, viewport_h))

    # 跳过 run(playwright) 之前的所有内容
    i = 0
    while i < len(lines):
        if lines[i].strip().startswith("def run("):
            i += 1
            break
        i += 1

    # 计算原始函数体的基础缩进（取第一个非跳过 action 行的缩进）
    base_indent = 4  # 默认
    for k in range(i, min(len(lines), i + 20)):
        s = lines[k].strip()
        if not s or s.startswith("#"):
            continue
        if any(p.match(lines[k]) for p in SKIP_PATTERNS):
            continue
        base_indent = _detect_indent(lines[k])
        break

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # 跳过 setup 行
        if any(p.match(line) for p in SKIP_PATTERNS):
            i += 1
            continue

        # 跳过收尾行
        if stripped in SKIP_EXACT:
            i += 1
            continue

        # 检测 marker 行
        m = MARKER_RE.match(line)
        if not m:
            if stripped.startswith("# [MARKER:") or stripped.startswith("# TODO:"):
                i += 1
                continue
            # 正常录制行 → 保留原始相对缩进
            rel_indent = max(0, _detect_indent(line) - base_indent)
            out.append(_indent_line(stripped, extra=rel_indent))
            # page.goto 之后自动加等待
            if stripped.startswith("page.goto("):
                out.append(_indent_line(f"page.wait_for_timeout(3000)"))
            # popup 完成后等 viewer 加载
            if stripped.startswith("page1 = page1_info.value"):
                out.append(_indent_line(f"page1.wait_for_timeout(2000)"))
            i += 1
            continue

        # ── marker 处理 ──
        name = m.group("name").strip()

        # 先消费 marker 注释行
        j = i + 1
        while j < len(lines) and (lines[j].strip() == "" or lines[j].strip().startswith("#")):
            j += 1

        # 对于需要消费后续 action 行的 marker，扩展 j 边界
        _consume_actions = {"影像画布交互", "Meta 信息工具", "序列选择",
                           "序列布局切换", "窗宽窗位 WL/WW"}
        if name in _consume_actions:
            while j < len(lines):
                s = lines[j].strip()
                if MARKER_RE.match(lines[j]):
                    break
                if s in SKIP_EXACT:
                    break
                if s and not s.startswith("#"):
                    j += 1
                else:
                    break

        if name in KEEP_ORIGINAL:
            out.append(_indent_line(f"# [MARKER: {name}] — 保留录制操作"))
            _copy_action_lines(lines, i + 1, j, base_indent, out)
            _maybe_add_wait(out, name, pv)
            i = j
            continue

        if name == "报告截图":
            out.append(_indent_line(f"# [MARKER: 报告截图]"))
            out.append(_indent_line(f"page.screenshot(path=str(SCRIPT_DIR / \"report.jpeg\"), type=\"jpeg\", quality=95, full_page=True)"))
            out.append(_indent_line(f"print(\"[截图] 报告已保存: report.jpeg\")"))
            out.append(_indent_line(f"page.wait_for_timeout(2000)"))
            i = j
            continue

        if name == "序列选择":
            protocol = config["protocol_name"]
            label = f"# [MARKER: 序列选择 — 用户协议: {protocol}]" if protocol else f"# [MARKER: 序列选择]"
            out.append(_indent_line(label))
            _copy_action_lines(lines, i + 1, j, base_indent, out)
            _maybe_add_wait(out, name, pv)
            i = j
            continue

        if name == "影像画布交互":
            out.append(_indent_line(f"# [MARKER: 影像画布交互]"))
            out.append(_indent_line(f"print(\"[画布] 开始全量帧翻页截图...\")"))
            out.append(_indent_line(f"frame_paths = capture_canvas_interaction("))
            out.append(_indent_line(f"{pv},", extra=4))
            out.append(_indent_line(f"click_x={canvas_x}, click_y={canvas_y},", extra=4))
            out.append(_indent_line(f"iframe_selectors={iframe_repr},", extra=4))
            if fc:
                out.append(_indent_line(f"total_frames={fc},", extra=4))
            out.append(_indent_line(f"output_dir=str(SCRIPT_DIR / \"capture_frames\"),", extra=4))
            out.append(_indent_line(f")", extra=4))
            out.append(_indent_line(f"print(f\"[画布] 共截取 {{len(frame_paths)}} 帧\")"))
            i = j
            continue

        if name == "Meta 信息工具":
            out.append(_indent_line(f"# [MARKER: Meta 信息工具]"))
            _copy_action_lines(lines, i + 1, j, base_indent, out,
                              skip_if=lambda s: re.search(r'close|Close', s))
            out.append(_indent_line(f"{pv}.wait_for_timeout(1500)"))
            out.append(_indent_line(f"print(\"[Meta] 开始提取 DICOM 信息...\")"))
            out.append(_indent_line(f"rows = extract_meta_from_frame("))
            out.append(_indent_line(f"{pv},", extra=4))
            out.append(_indent_line(f"iframe_selectors={iframe_repr},", extra=4))
            out.append(_indent_line(f")", extra=4))
            out.append(_indent_line(f"print(f\"[Meta] 提取了 {{len(rows)}} 个 tag\")"))
            out.append(_indent_line(f"validate_and_save(rows, output_dir=SCRIPT_DIR, project_root=_PROJECT)"))
            i = j
            continue

        # 未知 marker — 保留原始
        out.append(_indent_line(f"# [MARKER: {name}] — 未识别，保留原始"))
        _copy_action_lines(lines, i + 1, j, base_indent, out)
        _maybe_add_wait(out, name, pv)
        i = j

    out.append(_generate_footer())
    return "\n".join(out)


def _copy_action_lines(lines: list[str], start: int, end: int,
                        base_indent: int, out: list[str],
                        skip_if: callable = None) -> None:
    """复制原始脚本的 action 行（保持相对缩进）。可选的 skip_if 过滤。"""
    for k in range(start, end):
        s = lines[k].strip()
        if not s or s.startswith("# [MARKER:") or s.startswith("# TODO:"):
            continue
        if skip_if and skip_if(s):
            continue
        rel_indent = max(0, _detect_indent(lines[k]) - base_indent)
        out.append(_indent_line(s, extra=rel_indent))


# ==================================================================
# CLI
# ==================================================================

def main():
    parser = argparse.ArgumentParser(description="从录制脚本生成全自动采图脚本（无需 LLM）")
    parser.add_argument("--input", "-i", required=True, help="输入: processed_script.py")
    parser.add_argument("--output", "-o", required=True, help="输出: auto_capture.py")
    parser.add_argument("--total-frames", "-n", type=int, default=None,
                       help="总帧数（默认从协议名自动提取，提取不准时手动指定）")
    args = parser.parse_args()

    script = Path(args.input).read_text(encoding="utf-8")

    stem = Path(args.input).stem
    script_name = "_" + stem[len("processed_script"):] if stem.startswith("processed_script_") else ""

    result = generate(script, script_name, user_total_frames=args.total_frames)
    Path(args.output).write_text(result, encoding="utf-8")
    print(f"[auto_gen] 已生成 → {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
