"""
Skill 执行引擎 — 把 skill 目录拼成 prompt → LLM 补全 → 验证 → 修正。

用法:
    python agent.py processed_script.py -o completed.py
    python agent.py processed_script.py --dry-run          # 只预览 prompt，不调 LLM
    python agent.py processed_script.py -o out.py --retry 3  # 失败最多重试 3 次

工作原理:
    1. 解析脚本中的 # [MARKER: xxx] 标记
    2. 匹配 skills/xxx/ 目录
    3. 加载 skill bundle: SKILL.md + references/*.md + test_data/*
    4. 拼成 prompt → 调用 LLM 生成补全代码
    5. 语法检查 + 结构校验
    6. 失败则把错误信息追加到 prompt，重新调用 LLM 修正
    7. 输出完整脚本
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

# ── 加载 .env 文件（项目根） ──
try:
    from dotenv import load_dotenv
    _env_path = Path(__file__).resolve().parent / ".env"
    if _env_path.is_file():
        load_dotenv(_env_path)
except ImportError:
    pass  # python-dotenv 未安装，仅依赖系统环境变量

SKILLS_DIR = Path(__file__).resolve().parent / "skills"
# 优先读新名（通用），兼容旧名
DEFAULT_API_KEY = os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
DEFAULT_BASE_URL = os.environ.get("LLM_BASE_URL") or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
DEFAULT_MODEL = os.environ.get("LLM_MODEL") or os.environ.get("AUTOCOMPLETE_MODEL", "gpt-4o")
try:
    DEFAULT_MAX_TOKENS = int(
        os.environ.get("LLM_MAX_TOKENS")
        or os.environ.get("AUTOCOMPLETE_MAX_TOKENS", "12000")
    )
except ValueError:
    DEFAULT_MAX_TOKENS = 12000

# marker 名称 → skill 目录名
MARKER_MAP = {
    "报告截图": "marker-report-screenshot",
    "序列选择": "marker-sequence-select",
    "Meta 信息工具": "marker-meta-extract",
    "影像画布交互": "marker-canvas-capture",
}
# 注：「窗宽窗位 WL/WW」「序列布局切换」是录制时手编的固定操作（点击/输入填充），
#     agent.py 见到它们时 SKILLS_DIR / "_not_found_" 不存在，会静默跳过，
#     marker 块（注释 + 录制操作）原样保留在 completed.py 里。
#     见 AGENTS.md:65-74 与 CLAUDE.md「常见坑」。


# ═══════════════════════════════════════════════════
# 1. 解析 marker
# ═══════════════════════════════════════════════════

MARKER_RE = re.compile(
    r"^(?P<indent>[ \t]*)# \[MARKER: (?P<name>[^\]]+?)(?: @ (?P<ts>\d{8}_\d{6}))?\]"
)

# 这些 marker 的后续 Playwright 动作是人工“示教”代码。Skill 生成通用实现后，
# 示教动作必须随 marker 一起被替换，否则会在动态逻辑后再次执行硬编码操作。
REPLACE_RECORDED_ACTIONS = {
    "序列选择",
    "Meta 信息工具",
    "影像画布交互",
}

_SCRIPT_TEARDOWN_LINES = {
    "# ---------------------",
    "context.close()",
    "browser.close()",
    "with sync_playwright() as playwright:",
    "run(playwright)",
}


def _find_marker_end(lines: List[str], start: int, name: str) -> int:
    """返回 marker 替换块的右开区间。

    普通 marker 只包含 marker 与连续注释。动态 marker 还包含直到下一个
    marker 或脚本收尾之前的示教动作。
    """
    j = start + 1
    while j < len(lines):
        stripped = lines[j].strip()
        if stripped == "" or (stripped.startswith("#") and not MARKER_RE.match(lines[j])):
            j += 1
            continue
        break

    if name not in REPLACE_RECORDED_ACTIONS:
        return j

    while j < len(lines):
        stripped = lines[j].strip()
        if MARKER_RE.match(lines[j]) or stripped in _SCRIPT_TEARDOWN_LINES:
            break
        j += 1
    return j


def parse_markers(script: str) -> List[Dict]:
    lines = script.split("\n")
    markers = []
    i = 0
    while i < len(lines):
        m = MARKER_RE.match(lines[i])
        if m:
            name = m.group("name").strip()
            ts = m.group("ts") or ""
            indent = m.group("indent")
            j = _find_marker_end(lines, i, name)
            markers.append({
                "name": name, "ts": ts, "indent": indent,
                "line_start": i + 1, "line_end": j,
                "raw": "\n".join(lines[i:j]),
                "context_before": lines[max(0, i - 8):i],
                "context_after": lines[j:min(len(lines), j + 20)],
            })
            i = j
        else:
            i += 1
    return markers


# ═══════════════════════════════════════════════════
# 2. 加载 skill bundle
# ═══════════════════════════════════════════════════

MAX_FILE_BYTES = 200_000  # 单文件最大 200KB，防止 prompt 溢出

def load_skill_bundle(skill_dir: Path) -> Dict[str, str]:
    """加载 skill 目录的所有内容，返回 {文件名: 内容}。"""
    bundle = {}
    if not skill_dir.exists():
        return bundle

    for f in sorted(skill_dir.rglob("*")):
        if not f.is_file():
            continue
        if f.suffix not in (".md", ".json", ".txt", ".py"):
            continue
        if f.stat().st_size > MAX_FILE_BYTES:
            continue
        try:
            content = f.read_text(encoding="utf-8")
        except Exception:
            continue
        # 用相对路径做 key
        key = str(f.relative_to(skill_dir)).replace("\\", "/")
        bundle[key] = content
    return bundle


def bundle_to_prompt(bundle: Dict[str, str], marker: Dict) -> str:
    """把 skill bundle 拼成发给 LLM 的 prompt。"""
    parts = []

    # ── 核心指令 ──
    if "SKILL.md" in bundle:
        body = bundle["SKILL.md"]
        # 去掉 YAML frontmatter
        if body.startswith("---"):
            body = body.split("---", 2)[-1]
        parts.append(f"## Skill 指令\n{body.strip()}")

    # ── 参考文档 ──
    refs = {k: v for k, v in bundle.items() if k.startswith("references/")}
    for name, content in refs.items():
        label = name.replace("references/", "").replace(".md", "").replace("_", " ").title()
        parts.append(f"## 参考：{label}\n{content.strip()[:8000]}")  # 截断长文档

    # ── 示例数据 (few-shot) ──
    inputs = {k: v for k, v in bundle.items() if "test_data" in k and "out_" not in k}
    outputs = {k: v for k, v in bundle.items() if "out_" in k}

    if inputs:
        parts.append("## 示例输入")
        for name, content in sorted(inputs.items())[:2]:  # 最多 2 个示例
            parts.append(f"### {name}\n```\n{content.strip()[:4000]}\n```")

    if outputs:
        parts.append("## 期望输出")
        for name, content in sorted(outputs.items())[:3]:  # 最多 3 个输出
            parts.append(f"### {name}\n```json\n{content.strip()[:4000]}\n```")

    # ── 当前标记上下文 ──
    parts.append(f"""
## 当前需要补全的标记

标记名称: {marker['name']}
缩进级别: {repr(marker['indent'])}

### 标记之前的代码
```python
{chr(10).join(marker['context_before'])}
```

### 标记块（需要替换）
```python
{marker['raw']}
```

### 标记之后的代码
```python
{chr(10).join(marker['context_after'][:15])}
```

## 要求
1. 只输出替换标记块的 Python 代码，不要解释
2. 保持缩进 {repr(marker['indent'])}
3. marker 注释行本身保留
4. **生成代码的输出格式必须与期望输出一致**
5. 代码直接放在 ```python ``` 代码块内
""")
    return "\n\n".join(parts)


# ═══════════════════════════════════════════════════
# 3. LLM 调用
# ═══════════════════════════════════════════════════

def call_llm(prompt: str, model: str = DEFAULT_MODEL) -> str:
    if not DEFAULT_API_KEY:
        raise RuntimeError("请设置 OPENAI_API_KEY 环境变量")
    from openai import OpenAI
    client = OpenAI(api_key=DEFAULT_API_KEY, base_url=DEFAULT_BASE_URL)
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": "你是 Playwright Python 脚本补全助手。只输出代码，不要解释。"},
            {"role": "user", "content": prompt},
        ],
        temperature=0.2, max_tokens=DEFAULT_MAX_TOKENS,
    )
    return resp.choices[0].message.content


# ═══════════════════════════════════════════════════
# 4. 验证
# ═══════════════════════════════════════════════════

def validate_syntax(code: str) -> Optional[str]:
    """Python 语法检查。返回 None 表示通过，否则返回错误信息。"""
    try:
        ast.parse(code)
        return None
    except SyntaxError as e:
        return f"SyntaxError: {e.msg} at line {e.lineno}, offset {e.offset}"

def extract_code_block(response: str) -> str:
    """从 LLM 响应中提取 ```python ... ``` 代码块。"""
    m = re.search(r"```python\s*\n(.*?)```", response, re.DOTALL)
    if m:
        return m.group(1).rstrip()
    # 没有代码块标记，返回全文
    return response.strip()


def _generate_deterministic_meta(marker: Dict) -> str:
    """用录制动作打开/关闭面板，并调用共享 Meta 提取模块。"""
    action_lines = [
        line.strip()
        for line in marker["raw"].splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    close_actions = [
        line for line in action_lines
        if re.search(r"(?:\.close\b|关闭|close\()", line, re.I)
    ]
    open_actions = [line for line in action_lines if line not in close_actions]

    context_text = "\n".join(
        marker["context_before"] + action_lines + marker["context_after"]
    )
    page_match = re.search(r"\b(page\d*)\.", context_text)
    page_var = page_match.group(1) if page_match else "page"

    iframe_selectors: List[str] = []
    for match in re.finditer(r"\.locator\(([^)]+)\)\.content_frame", context_text):
        raw_selector = match.group(1).strip()
        try:
            selector = ast.literal_eval(raw_selector)
        except Exception:
            selector = raw_selector.strip("'\"")
        if isinstance(selector, str) and selector not in iframe_selectors:
            iframe_selectors.append(selector)

    iframe_repr = repr(iframe_selectors) if iframe_selectors else "None"
    block = [
        f"# [MARKER: Meta 信息工具"
        f"{' @ ' + marker['ts'] if marker['ts'] else ''}]",
        "import sys",
        "from pathlib import Path",
        "SCRIPT_DIR = Path(__file__).resolve().parent",
        "_PROJECT = SCRIPT_DIR.parent.parent",
        "if str(_PROJECT) not in sys.path:",
        "    sys.path.insert(0, str(_PROJECT))",
        "from skills._shared.meta_extract import extract_meta_from_frame",
        "from skills._shared.meta_validate import validate_and_save",
        *open_actions,
        f"{page_var}.wait_for_timeout(1500)",
        "print(\"[Meta] 开始提取 DICOM 信息...\")",
        "rows = extract_meta_from_frame(",
        f"    {page_var},",
        f"    iframe_selectors={iframe_repr},",
        ")",
        "print(f\"[Meta] 提取了 {len(rows)} 个 tag\")",
        "if len(rows) < 10:",
        f"    {page_var}.screenshot(",
        "        path=str(SCRIPT_DIR / \"dicom_panel_fallback.jpeg\"),",
        "        type=\"jpeg\", quality=95, full_page=True,",
        "    )",
        "validate_and_save(rows, output_dir=SCRIPT_DIR, project_root=_PROJECT)",
        *close_actions,
    ]
    indent = marker["indent"]
    return "\n".join(
        (indent + line) if line else line
        for line in block
    )


def _generate_deterministic_canvas(marker: Dict) -> str:
    """调用共享画布采集模块，并保留录制时的点击坐标。"""
    context_text = "\n".join(
        marker["context_before"]
        + marker["raw"].splitlines()
        + marker["context_after"]
    )
    page_match = re.search(r"\b(page\d*)\.", context_text)
    page_var = page_match.group(1) if page_match else "page"

    position_match = re.search(
        r"position\s*=\s*\{\s*['\"]x['\"]\s*:\s*"
        r"(?P<x>-?\d+(?:\.\d+)?)\s*,\s*['\"]y['\"]\s*:\s*"
        r"(?P<y>-?\d+(?:\.\d+)?)\s*\}",
        marker["raw"],
    )
    click_x = position_match.group("x") if position_match else "0"
    click_y = position_match.group("y") if position_match else "0"

    block = [
        f"# [MARKER: 影像画布交互"
        f"{' @ ' + marker['ts'] if marker['ts'] else ''}]",
        "import sys",
        "from pathlib import Path",
        "SCRIPT_DIR = Path(__file__).resolve().parent",
        "_PROJECT = SCRIPT_DIR.parent.parent",
        "if str(_PROJECT) not in sys.path:",
        "    sys.path.insert(0, str(_PROJECT))",
        "from skills._shared.canvas_capture import capture_canvas_interaction",
        "frame_paths = capture_canvas_interaction(",
        f"    {page_var},",
        f"    click_x={click_x}, click_y={click_y},",
        '    total_frames=locals().get("seq_frames"),',
        '    series_name=locals().get("seq_name"),',
        '    output_root=SCRIPT_DIR / "canvas_frames",',
        ")",
        'print(f"[画布] 已保存 {len(frame_paths)} 帧")',
    ]
    indent = marker["indent"]
    return "\n".join(
        (indent + line) if line else line
        for line in block
    )


def _wrap_sequence_state_waits(marker: Dict, generated_code: str) -> str:
    """Prefer structural series items, then fall back to generated selection."""
    context_text = "\n".join(
        marker["context_before"]
        + marker["raw"].splitlines()
        + marker["context_after"]
    )
    page_match = re.search(r"\b(page\d*)\.", context_text)
    page_var = page_match.group(1) if page_match else "page"
    indent = marker["indent"]
    prefix = [
        "import sys as _sequence_sys",
        "from pathlib import Path as _SequencePath",
        "_SEQUENCE_PROJECT = _SequencePath(__file__).resolve().parents[2]",
        "if str(_SEQUENCE_PROJECT) not in _sequence_sys.path:",
        "    _sequence_sys.path.insert(0, str(_SEQUENCE_PROJECT))",
        "from skills._shared.viewer_state import (",
        "    select_structural_series,",
        "    wait_for_pre_action_state,",
        "    wait_for_post_action_state,",
        ")",
        f'wait_for_pre_action_state({page_var}, "序列选择")',
        f"_structural_series = select_structural_series({page_var})",
        "if _structural_series is not None:",
        "    seq_name, seq_frames = _structural_series",
        "else:",
    ]
    suffix = [
        f'if not wait_for_post_action_state({page_var}, "序列选择"):',
        '    raise RuntimeError("序列选择后报告遮罩未隐藏或工具栏未就绪")',
    ]
    prefix_text = "\n".join(indent + line for line in prefix)
    fallback_text = "\n".join(
        (indent + "    " + line[len(indent):])
        if line.strip() and line.startswith(indent)
        else (indent + "    " + line if line.strip() else line)
        for line in generated_code.splitlines()
    )
    suffix_text = "\n".join(indent + line for line in suffix)
    return f"{prefix_text}\n{fallback_text}\n{suffix_text}"


# ═══════════════════════════════════════════════════
# 5. 主流程
# ═══════════════════════════════════════════════════

def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


DETERMINISTIC_GENERATORS = {
    "Meta 信息工具": _generate_deterministic_meta,
    "影像画布交互": _generate_deterministic_canvas,
}


def _marker_generator(marker: Dict) -> str:
    """返回 marker 的生成方式：deterministic | skipped | llm。"""
    skill_dir = SKILLS_DIR / MARKER_MAP.get(marker["name"], "_not_found_")
    if not skill_dir.exists():
        return "skipped"
    if marker["name"] in DETERMINISTIC_GENERATORS:
        return "deterministic"
    return "llm"


def process_script(script: str, dry_run: bool = False,
                   max_retries: int = 3, model: str = DEFAULT_MODEL,
                   event_sink: Optional[Callable[[Dict], None]] = None) -> str:
    notify = event_sink or (lambda event: None)
    markers = parse_markers(script)
    lines = script.split("\n")

    notify({
        "event": "agent_started",
        "input_sha256": _sha256(script),
        "model": model,
        "marker_count": len(markers),
    })

    print(f"找到 {len(markers)} 个 marker:", file=sys.stderr)
    for m in markers:
        skill_dir = SKILLS_DIR / MARKER_MAP.get(m["name"], "_not_found_")
        has_skill = skill_dir.exists()
        print(f"  L{m['line_start']}: [{m['name']}] → "
              f"{'✅ ' + skill_dir.name if has_skill else '❌ 无 skill'}", file=sys.stderr)

    if dry_run:
        notify({"event": "agent_finished", "status": "dry_run"})
        return script

    # 进入处理循环前，为所有 marker（含将跳过的）发 marker_started
    for m in markers:
        notify({
            "event": "marker_started",
            "label": m["name"],
            "line": m["line_start"],
            "generator": _marker_generator(m),
        })

    # 从后往前处理（避免行号偏移）
    for marker in reversed(markers):
        skill_dir = SKILLS_DIR / MARKER_MAP.get(marker["name"], "_not_found_")
        if not skill_dir.exists():
            notify({
                "event": "marker_skipped",
                "label": marker["name"],
                "line": marker["line_start"],
                "reason": "no_skill",
            })
            continue

        generator = DETERMINISTIC_GENERATORS.get(marker["name"])
        if generator is not None:
            indented = generator(marker)
            test_lines = lines.copy()
            test_lines[marker["line_start"] - 1:marker["line_end"]] = indented.split("\n")
            error_msg = validate_syntax("\n".join(test_lines))
            if error_msg is not None:
                notify({
                    "event": "agent_failed",
                    "label": marker["name"],
                    "line": marker["line_start"],
                    "status": "deterministic_syntax_error",
                })
                raise RuntimeError(
                    f"生成 [{marker['name']}] 失败：确定性代码语法错误: {error_msg}"
                )
            lines[marker["line_start"] - 1:marker["line_end"]] = indented.split("\n")
            print(
                f"\n处理 [{marker['name']}] (L{marker['line_start']})"
                " → ✅ deterministic",
                file=sys.stderr,
            )
            notify({
                "event": "marker_finished",
                "label": marker["name"],
                "line": marker["line_start"],
                "status": "success",
                "generator": "deterministic",
                "output_line_count": len(indented.split("\n")),
            })
            continue

        bundle = load_skill_bundle(skill_dir)
        prompt = bundle_to_prompt(bundle, marker)

        print(f"\n{'='*60}", file=sys.stderr)
        print(f"处理 [{marker['name']}] (L{marker['line_start']})", file=sys.stderr)
        print(f"Skill: {skill_dir.name}", file=sys.stderr)
        print(f"Bundle: {len(bundle)} 个文件 ("
              f"SKILL.md={'✅' if 'SKILL.md' in bundle else '❌'}, "
              f"refs={sum(1 for k in bundle if k.startswith('references/'))}, "
              f"test_data={sum(1 for k in bundle if 'test_data' in k)})", file=sys.stderr)

        code = None
        indented = None
        error_msg = "LLM 未返回有效代码"
        attempts_used = 0
        successful_prompt_sha = None
        for attempt in range(1, max_retries + 1):
            print(f"  尝试 {attempt}/{max_retries}...", file=sys.stderr)

            if attempt == 1:
                current_prompt = prompt
            else:
                current_prompt = prompt + f"\n\n## 上一次生成的代码有错误，请修正\n```\n{error_msg}\n```"

            notify({
                "event": "marker_attempt",
                "label": marker["name"],
                "line": marker["line_start"],
                "attempt": attempt,
                "max_attempts": max_retries,
                "prompt_sha256": _sha256(current_prompt),
            })

            try:
                response = call_llm(current_prompt, model)
            except Exception as e:
                notify({
                    "event": "agent_failed",
                    "label": marker["name"],
                    "line": marker["line_start"],
                    "status": "llm_call_failed",
                })
                raise RuntimeError(
                    f"生成 [{marker['name']}] 失败：LLM 调用失败: {e}"
                ) from e

            code = extract_code_block(response)
            if not code:
                error_msg = "LLM 未返回可提取的 Python 代码"
                print(f"  ⚠ 未提取到代码块", file=sys.stderr)
                continue

            # 加缩进
            indented = "\n".join(
                (marker["indent"] + ln) if ln.strip() and not ln.startswith(marker["indent"]) else ln
                for ln in code.split("\n")
            )

            # 语法检查
            # 把补全代码嵌入完整脚本做语法检查
            test_lines = lines.copy()
            test_lines[marker["line_start"] - 1:marker["line_end"]] = indented.split("\n")
            test_script = "\n".join(test_lines)
            error_msg = validate_syntax(test_script)

            if error_msg is None:
                print(f"  ✅ 语法检查通过", file=sys.stderr)
                attempts_used = attempt
                successful_prompt_sha = _sha256(current_prompt)
                break
            else:
                print(f"  ❌ {error_msg}", file=sys.stderr)
        else:
            notify({
                "event": "agent_failed",
                "label": marker["name"],
                "line": marker["line_start"],
                "status": "exceeded_retries",
            })
            raise RuntimeError(
                f"生成 [{marker['name']}] 失败：超过 {max_retries} 次重试；"
                f"最后错误: {error_msg}"
            )

        if code and indented is not None:
            if marker["name"] == "序列选择":
                indented = _wrap_sequence_state_waits(marker, indented)
                wrapped_lines = lines.copy()
                wrapped_lines[
                    marker["line_start"] - 1:marker["line_end"]
                ] = indented.split("\n")
                wrapped_error = validate_syntax("\n".join(wrapped_lines))
                if wrapped_error is not None:
                    notify({
                        "event": "agent_failed",
                        "label": marker["name"],
                        "line": marker["line_start"],
                        "status": "sequence_wrapper_syntax_error",
                    })
                    raise RuntimeError(
                        f"生成 [{marker['name']}] 失败：状态等待包装语法错误: "
                        f"{wrapped_error}"
                    )
            lines[marker["line_start"] - 1:marker["line_end"]] = indented.split("\n")
            print(f"  → 已替换 {marker['line_end'] - marker['line_start'] + 1} 行 → {len(indented.split(chr(10)))} 行", file=sys.stderr)
            notify({
                "event": "marker_finished",
                "label": marker["name"],
                "line": marker["line_start"],
                "status": "success",
                "generator": "llm",
                "attempts": attempts_used,
                "prompt_sha256": successful_prompt_sha,
                "output_line_count": len(indented.split("\n")),
            })

    notify({
        "event": "agent_finished",
        "status": "success",
        "output_sha256": _sha256("\n".join(lines)),
    })
    return "\n".join(lines)


# ═══════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Skill 执行引擎 — 根据 marker 智能补全录制脚本")
    parser.add_argument("input", help="输入脚本 (.py)")
    parser.add_argument("-o", "--output", help="输出文件 (默认 stdout)")
    parser.add_argument("--dry-run", action="store_true", help="只预览 prompt，不调用 LLM")
    parser.add_argument("--retry", type=int, default=3, help="失败重试次数 (默认 3)")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"LLM 模型 (默认: {DEFAULT_MODEL})")
    parser.add_argument("--show-prompt", action="store_true", help="打印第一个 marker 的完整 prompt 并退出")
    parser.add_argument("--emit-jsonl", action="store_true",
                        help="以 JSONL 事件流输出到 stdout（须配合 --output）")
    args = parser.parse_args()

    if args.emit_jsonl and not args.output:
        parser.error("--emit-jsonl requires --output")
    if args.emit_jsonl and args.show_prompt:
        parser.error("--emit-jsonl conflicts with --show-prompt")

    script = Path(args.input).read_text(encoding="utf-8")

    if args.show_prompt:
        markers = parse_markers(script)
        for m in markers:
            skill_dir = SKILLS_DIR / MARKER_MAP.get(m["name"], "_not_found_")
            if skill_dir.exists():
                bundle = load_skill_bundle(skill_dir)
                prompt = bundle_to_prompt(bundle, m)
                print(f"=== PROMPT for [{m['name']}] ({skill_dir.name}) ===")
                print(prompt)
                break
        return

    def _emit_jsonl(event):
        print(json.dumps(event, ensure_ascii=False))
        sys.stdout.flush()

    event_sink = _emit_jsonl if args.emit_jsonl else None

    result = process_script(script, dry_run=args.dry_run,
                            max_retries=args.retry, model=args.model,
                            event_sink=event_sink)

    if args.output:
        Path(args.output).write_text(result, encoding="utf-8")
        print(f"\n已保存 → {args.output}", file=sys.stderr)
    else:
        print(result)


if __name__ == "__main__":
    main()
