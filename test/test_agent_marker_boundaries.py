import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
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


class AgentEventSinkTests(unittest.TestCase):
    """事件协议（agent 子协议 §3–§7）：event_sink + --emit-jsonl。"""

    def test_default_call_without_sink_is_unchanged(self):
        """默认调用 process_script 不传 sink：行为不变、无副作用。"""
        script = """def run(page):
    # [MARKER: Meta 信息工具 @ 20260730_225417]
    page.get_by_title("更多").click()
    page.locator("#moreBox a.tool.tool-tags").click()
    page.locator("#tagsBox a.close").click()
    context.close()
"""
        with patch.object(
            agent,
            "call_llm",
            side_effect=AssertionError("Meta marker must not call the LLM"),
        ):
            completed = agent.process_script(script)
        self.assertIn("extract_meta_from_frame", completed)
        self.assertIn("context.close()", completed)
        self.assertIsNone(agent.validate_syntax(completed))

    def test_dry_run_emits_only_started_and_finished(self):
        """--dry-run：只 agent_started + agent_finished{status:"dry_run"}。"""
        script = """def run(page):
    # [MARKER: Meta 信息工具]
    page.get_by_title("更多").click()
    context.close()
"""
        events = []
        result = agent.process_script(script, dry_run=True, event_sink=events.append)
        self.assertEqual(result, script)
        self.assertEqual(
            [ev["event"] for ev in events],
            ["agent_started", "agent_finished"],
        )
        self.assertEqual(events[-1]["status"], "dry_run")

    def test_event_sequence_deterministic_and_skipped(self):
        """事件序列：agent_started → [marker_started…] → agent_finished；
        确定性 marker 无 marker_attempt；无 skill marker 发 marker_skipped。"""
        script = """def run(page):
    # [MARKER: Meta 信息工具 @ 20260730_225417]
    page.get_by_title("更多").click()
    page.locator("#moreBox a.tool.tool-tags").click()
    page.locator("#tagsBox a.close").click()
    # [MARKER: 窗宽窗位 WL/WW]
    page.locator('input[name="customizeWl"]').fill("0")
    context.close()
"""
        events = []
        with patch.object(
            agent,
            "call_llm",
            side_effect=AssertionError("Meta marker must not call the LLM"),
        ):
            completed = agent.process_script(script, event_sink=events.append)

        self.assertIsNone(agent.validate_syntax(completed))

        event_names = [ev["event"] for ev in events]
        self.assertEqual(event_names[0], "agent_started")
        self.assertEqual(event_names[-1], "agent_finished")
        self.assertEqual(events[0]["marker_count"], 2)
        self.assertEqual(events[-1]["status"], "success")
        self.assertIn("output_sha256", events[-1])

        started = {ev["label"]: ev for ev in events if ev["event"] == "marker_started"}
        self.assertEqual(started["Meta 信息工具"]["generator"], "deterministic")
        self.assertEqual(started["Meta 信息工具"]["line"], 2)
        self.assertEqual(
            started["窗宽窗位 WL/WW"]["generator"], "skipped"
        )
        self.assertEqual(started["窗宽窗位 WL/WW"]["line"], 6)

        # 无 skill marker → marker_skipped reason=no_skill
        skipped = [ev for ev in events if ev["event"] == "marker_skipped"]
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]["label"], "窗宽窗位 WL/WW")
        self.assertEqual(skipped[0]["reason"], "no_skill")

        # 确定性 marker → marker_finished (deterministic)，且无 marker_attempt
        finished = {ev["label"]: ev for ev in events if ev["event"] == "marker_finished"}
        self.assertIn("Meta 信息工具", finished)
        self.assertEqual(finished["Meta 信息工具"]["generator"], "deterministic")
        self.assertEqual(finished["Meta 信息工具"]["status"], "success")
        self.assertGreater(finished["Meta 信息工具"]["output_line_count"], 0)
        attempts = [ev for ev in events if ev["event"] == "marker_attempt"]
        self.assertEqual(attempts, [])

    def test_llm_retries_exhausted_emits_agent_failed_no_marker_finished(self):
        """重试耗尽：event 流含 agent_failed 且该 marker 无 marker_finished；
        agent_failed 不含完整响应/prompt。"""
        script = """def run(page):
    # [MARKER: 序列选择]
    # TODO: 动态选择序列
    page.get_by_text("固定序列").click()
"""
        events = []
        with patch.object(
            agent,
            "call_llm",
            return_value="```python\n# [MARKER: 序列选择]\nbroken = r\"\n```",
        ):
            with self.assertRaisesRegex(RuntimeError, "序列选择"):
                agent.process_script(script, max_retries=2, event_sink=events.append)

        marker_events = [ev for ev in events if ev.get("label") == "序列选择"]
        names = [ev["event"] for ev in marker_events]
        self.assertIn("agent_failed", names)
        self.assertNotIn("marker_finished", names)
        self.assertEqual(marker_events[-1]["status"], "exceeded_retries")
        # 无完整响应/prompt 泄漏
        self.assertNotIn("prompt", marker_events[-1])
        self.assertNotIn("response", marker_events[-1])

        attempts = [ev for ev in marker_events if ev["event"] == "marker_attempt"]
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0]["attempt"], 1)
        self.assertEqual(attempts[-1]["attempt"], 2)
        self.assertEqual(attempts[0]["max_attempts"], 2)
        self.assertIn("prompt_sha256", attempts[0])

    def test_llm_call_failed_emits_agent_failed(self):
        """call_llm 抛错 → agent_failed{status:llm_call_failed}。"""
        script = """def run(page):
    # [MARKER: 序列选择]
    # TODO
    page.get_by_text("x").click()
"""
        events = []
        with patch.object(agent, "call_llm", side_effect=RuntimeError("boom")):
            with self.assertRaisesRegex(RuntimeError, "LLM 调用失败"):
                agent.process_script(script, event_sink=events.append)
        last = events[-1]
        self.assertEqual(last["event"], "agent_failed")
        self.assertEqual(last["status"], "llm_call_failed")
        self.assertNotIn("prompt", last)
        self.assertNotIn("response", last)

    def test_llm_success_emits_attempt_then_marker_finished(self):
        """LLM 成功：marker_attempt → marker_finished{generator:llm, attempts, prompt_sha256}。"""
        script = """def run(page):
    # [MARKER: 序列选择]
    # TODO: 动态选择序列
    page.get_by_text("固定序列").click()
"""
        generated_block = """```python
# [MARKER: 序列选择]
seq_name, seq_frames = select_series(page)
```"""
        events = []
        with patch.object(agent, "call_llm", return_value=generated_block):
            agent.process_script(script, event_sink=events.append)

        marker_events = [ev for ev in events if ev.get("label") == "序列选择"]
        names = [ev["event"] for ev in marker_events]
        self.assertEqual(names[0], "marker_started")
        self.assertEqual(names[1], "marker_attempt")
        self.assertEqual(names[-1], "marker_finished")
        self.assertEqual(marker_events[-1]["generator"], "llm")
        self.assertEqual(marker_events[-1]["status"], "success")
        self.assertEqual(marker_events[-1]["attempts"], 1)
        self.assertIn("prompt_sha256", marker_events[-1])
        self.assertGreater(marker_events[-1]["output_line_count"], 0)

    def test_sequence_wrap_second_validation_failure_emits_agent_failed_enum_reason(self):
        """序列选择 wrap 二次验证失败 → agent_failed reason ∈ 枚举集，且无 marker_finished。"""
        script = """def run(page):
    # [MARKER: 序列选择]
    # TODO: 动态选择序列
    page.get_by_text("固定序列").click()
"""
        # 首轮返回合法代码（通过初次语法检查），wrap 后产出非法语法触发二次验证失败
        generated_block = """```python
# [MARKER: 序列选择]
seq_name, seq_frames = select_series(page)
```"""
        all_allowed_reasons = {
            "llm_call_failed",
            "generated_code_syntax_invalid",
            "exceeded_retries",
            "deterministic_syntax_error",
        }
        events = []
        with patch.object(agent, "call_llm", return_value=generated_block), \
             patch.object(agent, "_wrap_sequence_state_waits", return_value='broken = r"'):
            with self.assertRaisesRegex(RuntimeError, "状态等待包装语法错误"):
                agent.process_script(script, event_sink=events.append)

        marker_events = [ev for ev in events if ev.get("label") == "序列选择"]
        failed = [ev for ev in marker_events if ev["event"] == "agent_failed"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[-1]["status"], "generated_code_syntax_invalid")
        self.assertIn(failed[-1]["status"], all_allowed_reasons)
        self.assertNotIn(
            "marker_finished",
            [ev["event"] for ev in marker_events],
        )

    def test_cli_emit_jsonl_requires_output(self):
        """--emit-jsonl 不带 --output → parser.error (SystemExit 码 2)。"""
        with patch("sys.argv", ["agent.py", "foo.py", "--emit-jsonl"]):
            with self.assertRaises(SystemExit) as cm:
                agent.main()
        self.assertEqual(cm.exception.code, 2)

    def test_cli_emit_jsonl_conflicts_show_prompt(self):
        """--emit-jsonl + --show-prompt → parser.error (SystemExit 码 2)。"""
        with patch(
            "sys.argv",
            ["agent.py", "foo.py", "--emit-jsonl", "-o", "out.py", "--show-prompt"],
        ):
            with self.assertRaises(SystemExit) as cm:
                agent.main()
        self.assertEqual(cm.exception.code, 2)

    def test_cli_emit_jsonl_dry_run_writes_only_event_lines_to_stdout(self):
        """--emit-jsonl --dry-run：stdout 只输出两行 JSON 事件，代码写入 --output。"""
        buf = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            inp = Path(tmp) / "in.py"
            inp.write_text("def run(page):\n    pass\n", encoding="utf-8")
            out = Path(tmp) / "out.py"
            with patch(
                "sys.argv",
                ["agent.py", str(inp), "--emit-jsonl", "-o", str(out), "--dry-run"],
            ), redirect_stdout(buf):
                agent.main()
            self.assertTrue(out.exists())
        lines = [l for l in buf.getvalue().splitlines() if l.strip()]
        self.assertEqual(len(lines), 2)
        parsed = [json.loads(l) for l in lines]
        self.assertEqual(
            [ev["event"] for ev in parsed],
            ["agent_started", "agent_finished"],
        )
        self.assertEqual(parsed[-1]["status"], "dry_run")


if __name__ == "__main__":
    unittest.main()
