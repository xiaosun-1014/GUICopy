#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""script_to_guide.py — 录制脚本 → 19 平台指引文档 + 反向固化入口。

两个方向：
  1. forward:  processed_script.py → input/{hospital}.md（给 agent 探索用）
  2. backward: steps.jsonl（agent 探索产物）→ 固化为确定性 auto_capture.py

用法:
  # 方向1：生成指引文档
  python script_to_guide.py forward \
      --script out/uicloud/processed_script.py \
      --output F:/19_playWrightReader/input/uicloud.md

  # 方向2：探索产物 → 确定性脚本
  python script_to_guide.py backward \
      --steps F:/19_playWrightReader/runs/latest/steps.jsonl \
      --script out/uicloud/processed_script.py \
      --output out/uicloud/auto_capture_uicloud.py

  # 方向3：端到端（失败自动降级）
  python script_to_guide.py auto \
      --script out/uicloud/processed_script.py \
      --url "https://uicloud.com/film/#/新病人ID" \
      --output out/uicloud/auto_capture_uicloud.py
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

# 复用 auto_gen.py 的提取函数
sys.path.insert(0, str(Path(__file__).resolve().parent))
from auto_gen import (
    MARKER_RE,
    extract_url,
    extract_iframe_selectors,
    extract_protocol_name,
    extract_frame_count,
    extract_dicom_button,
    extract_canvas_coords,
    extract_viewer_page_var,
    extract_viewport,
)


# ══════════════════════════════════════════════════════════════
# Forward: processed_script → 指引文档
# ══════════════════════════════════════════════════════════════

def _extract_sequence_selector(script: str) -> str:
    """提取序列选择的精确操作代码行。"""
    lines = script.split("\n")
    in_marker = False
    for line in lines:
        if "序列选择" in line and "[MARKER:" in line:
            in_marker = True
            continue
        if in_marker:
            s = line.strip()
            if s and not s.startswith("#"):
                return s
            if s.startswith("# [MARKER:"):
                break
    return ""


def _extract_layout_info(script: str) -> tuple[str, str]:
    """提取布局按钮名和选项名。"""
    button_name = ""
    option_name = ""
    lines = script.split("\n")
    in_marker = False
    for line in lines:
        if "序列布局切换" in line and "[MARKER:" in line:
            in_marker = True
            continue
        if in_marker:
            m = re.search(r'name=["\']([^"\']+)["\']', line)
            if m:
                if not button_name:
                    button_name = m.group(1)
                else:
                    option_name = m.group(1)
                    break
            if line.strip().startswith("# [MARKER:"):
                break
    return button_name, option_name


def _extract_wl_ww_selectors(script: str) -> tuple[str, str]:
    """提取 WL/WW 输入框选择器。"""
    wl_sel = ""
    ww_sel = ""
    for line in script.split("\n"):
        if "popTagText_WL" in line and ".fill(" in line:
            # 取最后一个 locator() 调用的参数
            matches = re.findall(r'\.locator\("([^"]+)"\)', line)
            if matches and not wl_sel:
                wl_sel = matches[-1]
        if "popTagText_WW" in line and ".fill(" in line:
            matches = re.findall(r'\.locator\("([^"]+)"\)', line)
            if matches and not ww_sel:
                ww_sel = matches[-1]
    return wl_sel, ww_sel


def _extract_canvas_selector(script: str) -> str:
    """提取 canvas 选择器。"""
    m = re.search(r'locator\(["\']([^"\']*canvas[^"\']*)["\']', script, re.I)
    return m.group(1) if m else "canvas"


def _has_popup(script: str) -> bool:
    return "expect_popup" in script


def generate_guide(script: str, hospital: str = "") -> str:
    """从 processed_script 生成 19 平台格式的指引文档。"""
    url = extract_url(script)
    iframe_selectors = extract_iframe_selectors(script)
    protocol_name = extract_protocol_name(script)
    frame_count = extract_frame_count(protocol_name)
    dicom_button = extract_dicom_button(script)
    canvas_x, canvas_y = extract_canvas_coords(script)
    page_var = extract_viewer_page_var(script)
    canvas_selector = _extract_canvas_selector(script)
    seq_selector = _extract_sequence_selector(script)
    layout_btn, layout_opt = _extract_layout_info(script)
    wl_sel, ww_sel = _extract_wl_ww_selectors(script)
    has_popup = _has_popup(script)

    iframe_path = " → ".join(iframe_selectors) if iframe_selectors else "(无 iframe)"
    page_var_explain = "popup 新窗口" if page_var == "page1" else "主页面 iframe"

    if not hospital:
        # 从 URL 推断
        m = re.search(r"//([^/]+)", url)
        hospital = m.group(1).split(".")[0] if m else "unknown"

    # 构建指引文档
    sections = []
    sections.append(f"# {hospital} 云胶片操作指引")
    sections.append("")
    sections.append("## 参数")
    sections.append("- {{url}} — 病人分享链接")
    sections.append("- {{password}} — 密码（可选）")
    sections.append("- {{output_dir}} — 截图保存目录（默认 ./output）")
    sections.append("")

    # 已知 DOM 结构（关键！减少探索）
    sections.append("## 已知 DOM 结构")
    sections.append("> 以下信息来自录制脚本，可直接使用，无需探索。")
    sections.append(f"- iframe 路径: `{iframe_path}`")
    sections.append(f"- 页面变量: {page_var}（{page_var_explain}）")
    sections.append(f"- canvas 选择器: `{canvas_selector}`")
    sections.append(f"- 序列文本样例: \"{protocol_name}\"")
    if dicom_button:
        sections.append(f"- DICOM 按钮: \"{dicom_button}\"")
    if wl_sel:
        sections.append(f"- WL 输入框: `{wl_sel}`")
    if ww_sel:
        sections.append(f"- WW 输入框: `{ww_sel}`")
    sections.append("")

    # 步骤
    sections.append("## 步骤")
    sections.append("")

    step_num = 1

    # 步骤1：打开链接
    sections.append(f"### {step_num}. 打开链接")
    sections.append("- 访问 {{url}}")
    sections.append("- 等待页面加载完成（networkidle）")
    step_num += 1
    sections.append("")

    # 步骤2：报告截图
    sections.append(f"### {step_num}. 报告页截图")
    sections.append("- 等待页面稳定后全页截图")
    sections.append("- 保存为 {{output_dir}}/report.jpeg")
    step_num += 1
    sections.append("")

    # 步骤3：进入影像查看
    if has_popup:
        sections.append(f"### {step_num}. 进入影像查看")
        sections.append("- 点击报告区域触发弹出 viewer 窗口")
        sections.append("- **精确方法**: 此站点使用 popup 新窗口模式")
        sections.append("- 等待新窗口加载完成")
        step_num += 1
        sections.append("")

    # 步骤N：选择序列
    sections.append(f"### {step_num}. 选择序列（动态判断）")
    sections.append(f"- 在 `{iframe_path}` 内查找序列列表")
    sections.append("- 选择规则:")
    sections.append("  - 优先选帧数最多的序列")
    sections.append("  - 关键词优先级: AIIR/Lung/MPR/薄层 > 普通序列 > Scout/Localizer(跳过)")
    sections.append("  - **双击**选中（不是单击）")
    if seq_selector:
        sections.append(f"- **录制参考**: `{seq_selector}`")
    if protocol_name:
        sections.append(f"- 样例序列名: \"{protocol_name}\"")
    if frame_count:
        sections.append(f"- 样例帧数: {frame_count}")
    step_num += 1
    sections.append("")

    # 步骤N：调整布局
    if layout_btn:
        sections.append(f"### {step_num}. 调整布局")
        sections.append(f"- 点击「{layout_btn}」按钮")
        if layout_opt:
            sections.append(f"- 选择「{layout_opt}」（1×1 布局）")
        step_num += 1
        sections.append("")

    # 步骤N：调整窗宽窗位
    if wl_sel or ww_sel:
        sections.append(f"### {step_num}. 调整窗宽窗位")
        sections.append(f"- 先点击 canvas 激活编辑: `{canvas_selector}`")
        if wl_sel:
            sections.append(f"- WL 输入框: `{wl_sel}` → 填入目标 WL 值 → 按 Enter")
        if ww_sel:
            sections.append(f"- WW 输入框: `{ww_sel}` → 填入目标 WW 值 → 按 Enter")
        sections.append("- 窗值规则:")
        sections.append("  - 肺窗: WW=1500, WL=-500")
        sections.append("  - 骨窗: WW=2000, WL=400")
        sections.append("  - 软组织: WW=400, WL=40")
        step_num += 1
        sections.append("")

    # 步骤N：翻页截图
    sections.append(f"### {step_num}. 翻页截图")
    sections.append(f"- canvas 选择器: `{canvas_selector}`")
    if frame_count:
        sections.append(f"- 预期总帧数: ~{frame_count}（可能因病人不同而变化）")
    sections.append("- **翻页前必须先点击 canvas 聚焦**")
    sections.append("- 翻页方式: 键盘 ArrowDown（最通用）")
    sections.append("- 每帧等待渲染完成后再截图（观察 canvas 内容变化）")
    sections.append("- 保存到 {{output_dir}}/frame_XXXX.jpeg")
    sections.append("- 连续 5 帧画面不变则停止（翻页可能已到底）")
    step_num += 1
    sections.append("")

    # 步骤N：提取 DICOM 信息
    if dicom_button:
        sections.append(f"### {step_num}. 提取 DICOM 信息")
        sections.append(f"- 点击「{dicom_button}」打开信息面板")
        sections.append("- 等待面板加载（1.5 秒）")
        sections.append("- 提取 table 中所有 DICOM tag 行（格式: tag | desc | value）")
        sections.append("- 保存为 {{output_dir}}/dicom_meta.json")
        sections.append("- 关闭面板")
        step_num += 1
        sections.append("")

    # 异常处理
    sections.append("## 异常处理")
    sections.append("- 页面加载超时 → 刷新重试（最多 3 次）")
    sections.append("- 序列选择失败 → 截图记录当前页面状态，选列表第一个")
    sections.append("- canvas 翻页无响应 → 先点击 canvas 聚焦，再试键盘/滚轮")
    sections.append("- DICOM 面板为空 → 截图保存，跳过此步骤")
    sections.append("")

    return "\n".join(sections)


# ══════════════════════════════════════════════════════════════
# Backward: steps.jsonl → 确定性脚本补丁
# ══════════════════════════════════════════════════════════════

def _load_steps(steps_path: str) -> list[dict]:
    """加载 agent 探索的 steps.jsonl。"""
    steps = []
    with open(steps_path, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                steps.append(json.loads(line))
    return steps


def _extract_successful_selectors(steps: list[dict]) -> dict:
    """从成功的 browser_click/evaluate 步骤中提取验证过的选择器。

    这些选择器是 agent 在真实页面上试出来的，比录制脚本更可靠
    （录制脚本的选择器可能因版本变化而失效）。
    """
    selectors = {
        "clicks": [],       # 成功的点击目标
        "evaluates": [],    # 成功的 JS 表达式
        "sequence": None,   # 序列选择的最终操作
        "ww_wl": None,      # 窗宽窗位的最终操作
        "frames_captured": 0,  # 成功截帧数
    }

    for step in steps:
        action = step.get("action", "")
        args = step.get("args", {})
        result = step.get("result_summary", "")
        verify = step.get("verify") or {}
        task_id = step.get("task_id", "")

        # 跳过失败步骤
        if "异常" in result or "Error" in result:
            continue
        if verify and not verify.get("ok", True):
            continue

        if action == "browser_click" and "does not match" not in result:
            selectors["clicks"].append({
                "target": args.get("target", ""),
                "element": args.get("element", ""),
                "task_id": task_id,
            })

        if action == "browser_evaluate" and result and "null" not in result[:20]:
            selectors["evaluates"].append({
                "function": args.get("function", "")[:200],
                "task_id": task_id,
            })

        if action == "save_evidence":
            selectors["frames_captured"] += 1

        # 标记序列选择成功的那一步
        if action in ("browser_click", "browser_evaluate") and "序列" in str(task_id):
            selectors["sequence"] = {
                "action": action,
                "args": args,
                "result": result[:200],
            }

        # 标记窗宽窗位成功
        if action == "set_ww_wl" and "异常" not in result:
            selectors["ww_wl"] = {
                "args": args,
                "result": result[:100],
            }

    return selectors


def _generate_crystallized_script(
    original_script: str,
    steps: list[dict],
    selectors: dict,
) -> str:
    """从 agent 探索结果生成确定性回放脚本。

    核心思想：
    - 固定步骤（导航、布局、WL/WW）→ 直接用录制/agent 验证过的选择器
    - 动态步骤（序列选择）→ 用 agent 验证过的策略，加回退逻辑
    - 翻页截图 → 用共享模块（已验证的帧数和翻页策略）
    """
    from auto_gen import (
        extract_viewport,
        extract_iframe_selectors,
        extract_viewer_page_var,
        extract_canvas_coords,
        extract_frame_count,
        extract_protocol_name,
    )

    viewport_w, viewport_h = extract_viewport(original_script)
    iframe_selectors = extract_iframe_selectors(original_script)
    page_var = extract_viewer_page_var(original_script)
    canvas_x, canvas_y = extract_canvas_coords(original_script)
    protocol_name = extract_protocol_name(original_script)
    frame_count = extract_frame_count(protocol_name)

    # 如果 agent 探索截帧成功且帧数更准确，用 agent 的
    if selectors["frames_captured"] > 0:
        frame_count = selectors["frames_captured"]

    iframe_repr = "[" + ", ".join(repr(s) for s in iframe_selectors) + "]" if iframe_selectors else "None"

    code = f'''# -*- coding: utf-8 -*-
"""确定性回放脚本 — 由 agent 探索结果固化生成。

来源:
  - 录制脚本: processed_script.py（DOM 结构）
  - Agent 探索: steps.jsonl（验证过的选择器和策略）
  - 共享模块: skills/_shared/（帧截图、meta 提取）

直接 python 执行即可，无需 LLM / agent。
"""
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT = SCRIPT_DIR.parent.parent
sys.path.insert(0, str(_PROJECT))
from skills._shared.canvas_capture import capture_canvas_interaction
from skills._shared.meta_extract import extract_meta_from_frame
from skills._shared.meta_validate import validate_and_save


def run(url: str = None, output_dir: str = None):
    if output_dir is None:
        output_dir = str(SCRIPT_DIR)
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        context = browser.new_context(viewport={{"width": {viewport_w}, "height": {viewport_h}}})
        page = context.new_page()

        # ── 1. 导航 ──
        target_url = url or "{extract_url(original_script)}"
        page.goto(target_url)
        page.wait_for_timeout(3000)

        # ── 2. 报告截图 ──
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        page.wait_for_timeout(2000)
        page.screenshot(
            path=str(out / "report.jpeg"),
            type="jpeg", quality=95, full_page=True
        )
        print("[截图] 报告已保存")
'''

    # 根据是否有 popup 生成进入 viewer 代码
    if _has_popup(original_script):
        code += f'''
        # ── 3. 进入影像 viewer（popup）──
        page.get_by_test_id("pi-report-container").click()
        with page.expect_popup() as page1_info:
            page.get_by_test_id("pi-action-images").get_by_role("img").click()
        {page_var} = page1_info.value
        {page_var}.wait_for_timeout(3000)
'''
    else:
        code += f'''
        # ── 3. 等待 viewer 加载 ──
        page.wait_for_timeout(3000)
'''

    # 序列选择 — 用 agent 验证过的策略
    code += f'''
        # ── 4. 序列选择（agent 验证过的策略 + 回退）──
        _select_sequence({page_var}, {iframe_repr})

        # ── 5. 布局 + 窗宽窗位（录制固定操作）──
'''

    # 从原始脚本复制布局和 WL/WW 的固定操作
    lines = original_script.split("\n")
    in_layout = False
    in_wlww = False
    for line in lines:
        s = line.strip()
        if "序列布局切换" in s and "[MARKER:" in s:
            in_layout = True
            continue
        if "窗宽窗位" in s and "[MARKER:" in s:
            in_layout = False
            in_wlww = True
            continue
        if "影像画布交互" in s and "[MARKER:" in s:
            in_wlww = False
            continue

        if (in_layout or in_wlww) and s and not s.startswith("#"):
            code += f"        {s}\n"

    code += f'''        {page_var}.wait_for_timeout(1000)

        # ── 6. 翻页截图（共享模块）──
        print("[画布] 开始全量帧翻页截图...")
        frame_paths = capture_canvas_interaction(
            {page_var},
            click_x={canvas_x}, click_y={canvas_y},
            iframe_selectors={iframe_repr},
            total_frames={frame_count or 'None'},
            output_dir=str(out / "capture_frames"),
        )
        print(f"[画布] 共截取 {{len(frame_paths)}} 帧")
'''

    # DICOM meta
    dicom_button = extract_dicom_button(original_script)
    if dicom_button:
        code += f'''
        # ── 7. DICOM Meta 提取 ──
        {page_var}.locator('[id="2d-iframe"]').content_frame.get_by_role(
            "button", name="{dicom_button}"
        ).click()
        {page_var}.wait_for_timeout(1500)
        print("[Meta] 开始提取 DICOM 信息...")
        rows = extract_meta_from_frame(
            {page_var},
            iframe_selectors={iframe_repr},
        )
        print(f"[Meta] 提取了 {{len(rows)}} 个 tag")
        validate_and_save(rows, output_dir=out, project_root=_PROJECT)
'''

    code += '''
        # ── 完成 ──
        browser.close()
        print("[auto_capture] 完成")


'''

    # 序列选择函数（包含 agent 学到的策略）
    code += _generate_sequence_select_function(selectors, iframe_selectors, protocol_name)

    code += '''

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=None, help="病人 URL（不传则用录制时的 URL）")
    parser.add_argument("--output", default=None, help="输出目录")
    args = parser.parse_args()
    run(url=args.url, output_dir=args.output)
'''
    return code


def _generate_sequence_select_function(
    selectors: dict,
    iframe_selectors: list[str],
    protocol_name: str,
) -> str:
    """生成序列选择函数 — 融合录制知识 + agent 探索结果。"""
    code = '''def _select_sequence(page, iframe_selectors):
    """序列选择 — 三级策略：精确匹配 → DOM 扫描 → 回退第一个。

    策略来源：
    - 录制脚本提供的序列名样例
    - Agent 探索验证过的选择器
    - 共享模块的 DOM 扫描逻辑
    """
    import re

    # 定位 viewer frame
    frame = None
    for f in page.frames:
        if f == page.main_frame:
            continue
        try:
            if f.locator("canvas").count() > 0:
                frame = f
                break
        except Exception:
            continue

    if not frame:
        print("[序列选择] ⚠ 未找到含 canvas 的 frame")
        return

    # 策略 A：DOM 扫描，按帧数排序选最大
    deadline = __import__("time").monotonic() + 15
    best_text = None
    best_frames = 0
    best_center = (0, 0)

    while __import__("time").monotonic() < deadline:
        nodes = frame.evaluate("""() => {
            return Array.from(document.querySelectorAll('body *')).map(el => {
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                const text = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                if (!text || text.length > 200) return null;
                if (style.display === 'none' || style.visibility === 'hidden') return null;
                if (rect.width < 30 || rect.height < 12) return null;
                if (rect.bottom < 0 || rect.right < 0) return null;
                return {
                    text: text,
                    x: rect.x, y: rect.y,
                    w: rect.width, h: rect.height,
                    cx: rect.x + rect.width / 2,
                    cy: rect.y + rect.height / 2,
                };
            }).filter(Boolean);
        }""")

        if not nodes:
            __import__("time").sleep(0.8)
            continue

        for node in nodes:
            text = node["text"]
            # 跳过 Scout/Localizer
            if re.search(r"(Scout|Localizer|Surview|Topogram|Dose)", text, re.I):
                continue
            # 解析帧数
            frames = 0
            for pat in [
                r"(\\d{2,4})\\s*(?:张|幅|层|帧|images?|slices?|frames?)",
                r"/(\\d{2,4})",
            ]:
                m_f = re.search(pat, text, re.I)
                if m_f:
                    frames = max(frames, int(m_f.group(1)))
            # 厚度推断
            if frames == 0:
                m_t = re.search(r"(\\d+\\.\\d+)\\s*(?:mm)?", text)
                if m_t:
                    thickness = float(m_t.group(1))
                    if thickness <= 1.0:
                        frames = 400
                    elif thickness <= 2.0:
                        frames = 200
                    elif thickness <= 5.0:
                        frames = 80

            if frames > best_frames:
                best_frames = frames
                best_text = text
                best_center = (node["cx"], node["cy"])

        if best_frames > 0:
            break
        __import__("time").sleep(0.8)

    if best_text:
        # 计算 iframe 偏移
        ox, oy = 0.0, 0.0
        if iframe_selectors:
            try:
                box = page.locator(iframe_selectors[0]).bounding_box()
                if box:
                    ox, oy = box["x"], box["y"]
            except Exception:
                pass
        cx, cy = best_center
        page.mouse.dblclick(cx + ox, cy + oy)
        page.wait_for_timeout(1500)
        print(f"[序列选择] ✓ 命中: {best_text[:60]} ({best_frames}帧)")
        return

    # 策略 B：回退 — 双击第一个可见序列项
    print("[序列选择] ⚠ DOM 扫描未找到帧数，尝试点击第一个序列项")
    try:
        nodes = frame.evaluate("""() => {
            const items = document.querySelectorAll('[class*=series], [class*=item], [class*=thumb]');
            if (items.length) {
                const r = items[0].getBoundingClientRect();
                return {cx: r.x + r.width/2, cy: r.y + r.height/2};
            }
            return null;
        }""")
        if nodes:
            ox, oy = 0.0, 0.0
            if iframe_selectors:
                try:
                    box = page.locator(iframe_selectors[0]).bounding_box()
                    if box:
                        ox, oy = box["x"], box["y"]
                except Exception:
                    pass
            page.mouse.dblclick(nodes["cx"] + ox, nodes["cy"] + oy)
            page.wait_for_timeout(1500)
            print("[序列选择] ✓ 回退：点击了第一个序列项")
    except Exception as e:
        print(f"[序列选择] ❌ 所有策略失败: {e}")
'''
    return code


# ══════════════════════════════════════════════════════════════
# Auto: 确定性脚本 → 运行 → 失败则降级到 agent → 固化
# ══════════════════════════════════════════════════════════════

def auto_pipeline(script_path: str, url: str, output_path: str):
    """端到端：先尝试确定性生成+运行，失败则调 19 平台 agent 探索后固化。"""
    from auto_gen import generate as gen_auto

    script = Path(script_path).read_text(encoding="utf-8")

    # Step 1: 尝试确定性生成
    print("[auto] Step 1: 确定性生成 auto_capture...")
    auto_code = gen_auto(script)
    auto_path = Path(output_path)
    auto_path.write_text(auto_code, encoding="utf-8")

    # Step 2: 尝试运行（带 timeout）
    print("[auto] Step 2: 尝试运行...")
    try:
        result = subprocess.run(
            [sys.executable, str(auto_path), "--url", url],
            capture_output=True, text=True, timeout=300,
            cwd=str(auto_path.parent),
        )
        if result.returncode == 0:
            print("[auto] ✅ 确定性脚本运行成功！")
            return True
        else:
            print(f"[auto] ⚠ 确定性脚本失败 (exit {result.returncode})")
            print(f"  stderr: {result.stderr[:500]}")
    except subprocess.TimeoutExpired:
        print("[auto] ⚠ 确定性脚本超时")
    except Exception as e:
        print(f"[auto] ⚠ 运行异常: {e}")

    # Step 3: 降级到 agent 探索
    print("[auto] Step 3: 降级到 agent 平台探索...")
    agent_platform = Path("F:/19_playWrightReader")
    if not agent_platform.exists():
        print("[auto] ❌ 19 平台不存在，无法降级")
        return False

    # 生成指引文档
    guide = generate_guide(script)
    guide_path = agent_platform / "input" / f"{auto_path.stem}.md"
    guide_path.write_text(guide, encoding="utf-8")
    print(f"[auto] 指引文档已生成: {guide_path}")

    # 调用 agent 平台
    agent_python = "F:/miniconda3/envs/playwright-reader/python.exe"
    try:
        result = subprocess.run(
            [agent_python, "main.py", "explore", "--url", url,
             "--doc", str(guide_path)],
            capture_output=True, text=True, timeout=600,
            cwd=str(agent_platform),
        )
        if result.returncode != 0:
            print(f"[auto] ⚠ Agent 探索异常 (exit {result.returncode})")
            print(f"  stderr: {result.stderr[:500]}")
            return False
    except subprocess.TimeoutExpired:
        print("[auto] ⚠ Agent 探索超时")
        return False

    # Step 4: 从 agent 产物固化为确定性脚本
    print("[auto] Step 4: 从 agent 产物固化...")
    runs_dir = agent_platform / "runs"
    latest_run = max(runs_dir.iterdir(), key=lambda p: p.stat().st_mtime)
    steps_path = latest_run / "steps.jsonl"

    if steps_path.exists():
        steps = _load_steps(str(steps_path))
        selectors = _extract_successful_selectors(steps)
        crystallized = _generate_crystallized_script(script, steps, selectors)
        Path(output_path).write_text(crystallized, encoding="utf-8")
        print(f"[auto] ✅ 已固化为确定性脚本: {output_path}")
        return True
    else:
        print("[auto] ❌ 未找到 agent 探索产物")
        return False


# ══════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="录制脚本 ↔ Agent 平台 桥接工具"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # forward: 生成指引文档
    p_fwd = sub.add_parser("forward", help="录制脚本 → 指引文档")
    p_fwd.add_argument("--script", "-s", required=True, help="processed_script.py 路径")
    p_fwd.add_argument("--output", "-o", required=True, help="输出指引文档路径")
    p_fwd.add_argument("--hospital", default="", help="医院名（默认从 URL 推断）")

    # backward: agent 产物 → 确定性脚本
    p_bwd = sub.add_parser("backward", help="Agent 探索产物 → 确定性脚本")
    p_bwd.add_argument("--steps", required=True, help="steps.jsonl 路径")
    p_bwd.add_argument("--script", "-s", required=True, help="原始 processed_script.py")
    p_bwd.add_argument("--output", "-o", required=True, help="输出确定性脚本路径")

    # auto: 端到端
    p_auto = sub.add_parser("auto", help="端到端：确定性 → 失败降级 agent → 固化")
    p_auto.add_argument("--script", "-s", required=True, help="processed_script.py 路径")
    p_auto.add_argument("--url", required=True, help="目标 URL")
    p_auto.add_argument("--output", "-o", required=True, help="输出脚本路径")

    args = parser.parse_args()

    if args.command == "forward":
        script = Path(args.script).read_text(encoding="utf-8")
        guide = generate_guide(script, hospital=args.hospital)
        Path(args.output).write_text(guide, encoding="utf-8")
        print(f"[forward] 指引文档已生成: {args.output}")

    elif args.command == "backward":
        script = Path(args.script).read_text(encoding="utf-8")
        steps = _load_steps(args.steps)
        selectors = _extract_successful_selectors(steps)
        crystallized = _generate_crystallized_script(script, steps, selectors)
        Path(args.output).write_text(crystallized, encoding="utf-8")
        print(f"[backward] 确定性脚本已生成: {args.output}")

    elif args.command == "auto":
        auto_pipeline(args.script, args.url, args.output)


if __name__ == "__main__":
    main()
