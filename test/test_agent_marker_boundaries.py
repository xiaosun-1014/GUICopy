import unittest
from pathlib import Path
from unittest.mock import patch

import agent


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class AgentMarkerBoundaryTests(unittest.TestCase):
    def test_sequence_marker_replaces_recorded_action_but_keeps_teardown(self):
        script = """from playwright.sync_api import sync_playwright

def run(playwright):
    browser = playwright.chromium.launch()
    context = browser.new_context()
    page = context.new_page()
    # [MARKER: 序列选择]
    # TODO: 对当前序列帧做判定 / 切帧
    page.get_by_role("link", name="固定病例序列 共 41张").dblclick()
    context.close()
    browser.close()
"""

        generated_block = """```python
# [MARKER: 序列选择]
seq_name, seq_frames = select_series(page)
```"""

        with patch.object(agent, "call_llm", return_value=generated_block):
            completed = agent.process_script(script)

        self.assertIn("seq_name, seq_frames = select_series(page)", completed)
        self.assertNotIn("固定病例序列", completed)
        self.assertIn("select_structural_series", completed)
        self.assertIn("wait_for_pre_action_state", completed)
        self.assertIn("wait_for_post_action_state", completed)
        self.assertIn(
            "_SequencePath(__file__).resolve().parents[2]",
            completed,
        )
        self.assertNotIn(
            "_SequencePath(__file__).resolve().parent.parent",
            completed,
        )
        self.assertLess(
            completed.index('wait_for_pre_action_state(page, "序列选择")'),
            completed.index("_structural_series = select_structural_series(page)"),
        )
        self.assertLess(
            completed.index("_structural_series = select_structural_series(page)"),
            completed.index("seq_name, seq_frames = select_series(page)"),
        )
        self.assertLess(
            completed.index("seq_name, seq_frames = select_series(page)"),
            completed.index('wait_for_post_action_state(page, "序列选择")'),
        )
        self.assertIn("context.close()", completed)
        self.assertIn("browser.close()", completed)

    def test_keep_original_marker_retains_recorded_actions(self):
        script = """def run(page):
    # [MARKER: 窗宽窗位 WL/WW]
    # TODO: 批量遍历预设窗
    page.locator('input[name="customizeWl"]').fill("0")
    page.locator('input[name="customizeWW"]').fill("2000")
    page.get_by_role("button", name="确定").click()
    context.close()
"""

        completed = agent.process_script(script)

        self.assertIn('input[name="customizeWl"]', completed)
        self.assertIn('input[name="customizeWW"]', completed)
        self.assertIn('name="确定"', completed)
        self.assertIn("context.close()", completed)

    def test_ftimage_dynamic_markers_include_recorded_dom_actions(self):
        script = (
            PROJECT_ROOT / "out" / "ftimage" / "processed_script_ftimage.py"
        ).read_text(encoding="utf-8")

        markers = {marker["name"]: marker for marker in agent.parse_markers(script)}

        self.assertIn('name="x 10.0_lung 共 41张"', markers["序列选择"]["raw"])
        self.assertNotIn("[MARKER: Meta 信息工具", markers["序列选择"]["raw"])

        self.assertIn('#moreBox a.tool.tool-tags', markers["Meta 信息工具"]["raw"])
        self.assertIn('#tagsBox a.close', markers["Meta 信息工具"]["raw"])
        self.assertNotIn("[MARKER: 窗宽窗位 WL/WW", markers["Meta 信息工具"]["raw"])

        self.assertIn('page.locator("canvas").click', markers["影像画布交互"]["raw"])
        self.assertNotIn("context.close()", markers["影像画布交互"]["raw"])

    def test_sequence_skill_supports_canvas_in_main_document(self):
        skill = (
            PROJECT_ROOT / "skills" / "marker-sequence-select" / "SKILL.md"
        ).read_text(encoding="utf-8")

        self.assertIn("return page1.main_frame", skill)
        self.assertIn("seq_name", skill)
        self.assertIn("seq_frames", skill)

    def test_agent_rejects_invalid_llm_code_after_all_retries(self):
        script = """def run(page):
    # [MARKER: 序列选择]
    # TODO: 动态选择序列
    page.get_by_text("固定序列").click()
"""

        with patch.object(
            agent,
            "call_llm",
            return_value="```python\n# [MARKER: 序列选择]\nbroken = r\"\n```",
        ):
            with self.assertRaisesRegex(RuntimeError, "序列选择"):
                agent.process_script(script, max_retries=2)

    def test_agent_allows_large_skill_completions(self):
        self.assertGreaterEqual(agent.DEFAULT_MAX_TOKENS, 8192)

    def test_meta_marker_uses_deterministic_generator_without_llm(self):
        script = """def run(page):
    # [MARKER: Meta 信息工具 @ 20260730_225417]
    # TODO: 提取当前检查的 Meta 信息
    page.get_by_title("更多").click()
    page.locator("#moreBox a.tool.tool-tags").click()
    page.locator("#tagsBox a.close").click()
    # [MARKER: 窗宽窗位 WL/WW]
    page.locator('input[name="customizeWl"]').fill("0")
    context.close()
"""

        with patch.object(
            agent,
            "call_llm",
            side_effect=AssertionError("Meta marker must not call the LLM"),
        ):
            completed = agent.process_script(script)

        self.assertIn('page.get_by_title("更多").click()', completed)
        self.assertIn('#moreBox a.tool.tool-tags', completed)
        self.assertIn('#tagsBox a.close', completed)
        self.assertIn("extract_meta_from_frame", completed)
        self.assertIn("validate_and_save", completed)
        self.assertLess(
            completed.index("#moreBox a.tool.tool-tags"),
            completed.index("rows = extract_meta_from_frame"),
        )
        self.assertLess(
            completed.index("validate_and_save"),
            completed.index("#tagsBox a.close"),
        )
        self.assertIn('input[name="customizeWl"]', completed)
        self.assertIn("context.close()", completed)
        self.assertIsNone(agent.validate_syntax(completed))

    def test_canvas_marker_uses_deterministic_generator_without_llm(self):
        script = """def run(page1):
    # [MARKER: 影像画布交互]
    # TODO: 调用 VL 模型对当前帧做判定 / 切帧
    page1.locator("canvas").click(position={"x": 819, "y": 318})
    page1.screenshot(path="viewer_cx.png", full_page=True)
    context.close()
"""

        with patch.object(
            agent,
            "call_llm",
            side_effect=AssertionError("Canvas marker must not call the LLM"),
        ):
            completed = agent.process_script(script)

        self.assertIn("import sys", completed)
        self.assertIn("from pathlib import Path", completed)
        self.assertIn("SCRIPT_DIR = Path(__file__).resolve().parent", completed)
        self.assertIn("sys.path.insert(0, str(_PROJECT))", completed)
        self.assertIn(
            "from skills._shared.canvas_capture import capture_canvas_interaction",
            completed,
        )
        self.assertIn("capture_canvas_interaction(", completed)
        self.assertIn("    page1,", completed)
        self.assertIn("click_x=819, click_y=318", completed)
        self.assertIn('total_frames=locals().get("seq_frames")', completed)
        self.assertIn('series_name=locals().get("seq_name")', completed)
        self.assertIn("output_root=SCRIPT_DIR / \"canvas_frames\"", completed)
        self.assertIn("[画布] 已保存 {len(frame_paths)} 帧", completed)
        self.assertNotIn('page1.locator("canvas").click', completed)
        self.assertNotIn('path="viewer_cx.png"', completed)
        self.assertIn("context.close()", completed)
        self.assertIsNone(agent.validate_syntax(completed))

    def test_canvas_skill_requires_exact_n_outputs_without_dedup(self):
        skill = (
            PROJECT_ROOT / "skills" / "marker-canvas-capture" / "SKILL.md"
        ).read_text(encoding="utf-8")

        self.assertIn("每个请求索引无条件落盘", skill)
        self.assertIn(
            'SCRIPT_DIR / "canvas_frames" / YYYYMMDD_HHMMSS_ffffff',
            skill,
        )
        self.assertIn("canvas_frame_0001.jpeg..N", skill)
        self.assertIn("capture_manifest.json", skill)
        self.assertNotIn("seen_sizes", skill)
        self.assertNotIn("文件大小去重", skill)
        self.assertNotIn("删除重复帧", skill)

    def test_report_skill_uses_fixed_jpeg_output_path(self):
        skill = (
            PROJECT_ROOT / "skills" / "marker-report-screenshot" / "SKILL.md"
        ).read_text(encoding="utf-8")

        self.assertIn('SCRIPT_DIR / "report.jpeg"', skill)
        self.assertIn('type="jpeg", quality=95', skill)


if __name__ == "__main__":
    unittest.main()
