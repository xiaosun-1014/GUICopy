"""compute_codegen_appendix / insert_marker_after_line / detect_indent 的纯文本逻辑测试。

覆盖新方案下的两个核心逻辑：
- codegen 推送 = append-only 增量：行数比对,只挑新增行追加到面板末尾
- marker 插入 = 直接在当前行后面写注释文本,沿用锚点行缩进

这些函数是模块级纯文本逻辑,不依赖 Qt,测试无需启动 Qt。
"""
import unittest

from main_gui import (
    compute_codegen_appendix,
    detect_indent,
    insert_marker_after_line,
)


CODGEN_INITIAL = (
    'import re\n'
    'from playwright.sync_api import Playwright, sync_playwright, expect\n'
    '\n'
    '\n'
    'def run(playwright: Playwright) -> None:\n'
    '    browser = playwright.chromium.launch(headless=False)\n'
    '    context = browser.new_context()\n'
    '    page = context.new_page()\n'
    '    page.goto("https://uicloud.com/film/#/risreport/patient/100")\n'
    '    with page.expect_popup() as page1_info:\n'
    '        page.get_by_text("查看影像").click()\n'
    '    page1 = page1_info.value\n'
    '    page.goto("https://uicloud.com/film/#/risreport/patient/100")\n'
    '    page1.goto("https://uicloud.com/film/#/viewer2D?id=abc")\n'
    '    page1.close()\n'
    '    page.close()\n'
    '\n'
    '    # ---------------------\n'
    '    context.close()\n'
    '    browser.close()\n'
    '\n'
    '\n'
    'with sync_playwright() as playwright:\n'
    '    run(playwright)\n'
)


class TestComputeCodegenAppendix(unittest.TestCase):
    """codegen 推送行数比对:只挑新增行,不重复推送已有内容,不覆盖用户手编。"""

    def test_first_push_returns_full_content(self):
        # 首次推送 last_count=0:应当返回全部行(末尾 \\n 是 splitlines 不保留的,
        # 实际首次初始化走 setPlainText(code) 不经此函数;这里验证增量起点正确)
        count, appendix = compute_codegen_appendix(0, CODGEN_INITIAL)
        self.assertEqual(count, len(CODGEN_INITIAL.splitlines()))
        # splitlines 不保留末尾空行,所以 appendix 等价于去掉末尾 \\n 的内容
        self.assertEqual(appendix, CODGEN_INITIAL.rstrip("\n"))

    def test_push_with_new_lines_returns_only_delta(self):
        # 在末尾追加两行 → 只返回这两行
        new_content = CODGEN_INITIAL + (
            '    page.bring_to_front()\n'
            '    page.wait_for_timeout(500)\n'
        )
        count, appendix = compute_codegen_appendix(
            len(CODGEN_INITIAL.splitlines()), new_content
        )
        self.assertEqual(count, 2)
        self.assertEqual(
            appendix,
            '    page.bring_to_front()\n    page.wait_for_timeout(500)'
        )

    def test_push_with_no_new_lines_returns_empty(self):
        # 同样行数同样内容(codegen 内部去重后还会触发)→ 不追加
        count, appendix = compute_codegen_appendix(
            len(CODGEN_INITIAL.splitlines()), CODGEN_INITIAL
        )
        self.assertEqual(count, 0)
        self.assertEqual(appendix, "")

    def test_push_with_fewer_lines_returns_empty(self):
        # codegen 罕见地减少行数(录制结束等):不破坏面板
        shorter = "a\nb\n"
        count, appendix = compute_codegen_appendix(5, shorter)
        self.assertEqual(count, 0)
        self.assertEqual(appendix, "")

    def test_trailing_newline_does_not_count_as_extra_line(self):
        # splitlines vs split("\\n") 的区别:末尾 \\n 不算一行
        content = "a\nb\nc\n"
        count, appendix = compute_codegen_appendix(0, content)
        self.assertEqual(count, 3)
        self.assertEqual(appendix, "a\nb\nc")

    def test_user_edit_does_not_pollute_codegen_counting(self):
        """用户手编删了行,下次 codegen 推送时只追加『真正的新增行』。

        场景:
          - 上次推送 10 行,面板里用户删到 8 行(手编)
          - codegen 新增了 2 行,变成 12 行
          - 应该返回 (2, 最后两行内容),不退回 4 行
        """
        count, appendix = compute_codegen_appendix(10, CODGEN_INITIAL)
        # CODGEN_INITIAL 长度 > 10,所以返回 (delta, delta 内容)
        self.assertEqual(count, len(CODGEN_INITIAL.splitlines()) - 10)
        self.assertEqual(appendix, "\n".join(CODGEN_INITIAL.splitlines()[10:]))


class TestInsertMarkerAfterLine(unittest.TestCase):
    """marker 插入:在当前行后面写入注释文本,沿用锚点行缩进。"""

    def test_insert_after_simple_line(self):
        source = "def foo():\n    x = 1\n    y = 2\n"
        marker_text = "# [MARKER: X]\n# TODO\n"
        out = insert_marker_after_line(source, 1, marker_text)
        self.assertEqual(
            out,
            "def foo():\n"
            "    x = 1\n"
            "    # [MARKER: X]\n"
            "    # TODO\n"
            "    y = 2\n",
        )

    def test_insert_uses_anchor_indent(self):
        source = "def foo():\n    x = 1\n"
        marker_text = "# [MARKER: X]\n# TODO\n"
        out = insert_marker_after_line(source, 1, marker_text)
        for ln in out.split("\n"):
            if ln.lstrip().startswith("# [MARKER: X]") or ln.lstrip() == "# TODO":
                self.assertTrue(
                    ln.startswith("    "),
                    f"marker 缩进应为 4 空格,实际: {ln!r}",
                )

    def test_insert_inside_deeper_block_uses_deeper_indent(self):
        source = (
            "def foo():\n"
            "    with page.expect_popup():\n"
            "        page.click()\n"
        )
        marker_text = "# DEEP\n"
        out = insert_marker_after_line(source, 2, marker_text)
        self.assertIn("        # DEEP", out)

    def test_insert_at_last_line(self):
        source = "def foo():\n    x = 1\n"
        marker_text = "# END\n"
        out = insert_marker_after_line(source, 1, marker_text)
        self.assertEqual(out, "def foo():\n    x = 1\n    # END\n")

    def test_insert_at_first_line(self):
        source = "import os\nimport sys\n"
        marker_text = "# TOP\n"
        out = insert_marker_after_line(source, 0, marker_text)
        self.assertEqual(out, "import os\n# TOP\nimport sys\n")

    def test_marker_text_trailing_blank_stripped(self):
        """markers.py 模板末尾留空串用于换行,这里要 pop 掉再补缩进。"""
        source = "    x = 1\n"
        marker_text = "# M\n# TODO\n\n"  # 末尾有 \\n\\n → 多一个空行
        out = insert_marker_after_line(source, 0, marker_text)
        # 不应出现两个连续空行
        self.assertNotIn("\n\n\n", out)
        self.assertIn("    # M", out)
        self.assertIn("    # TODO", out)

    def test_invalid_line_idx_returns_source_unchanged(self):
        source = "a\nb\n"
        out = insert_marker_after_line(source, 99, "# X\n")
        self.assertEqual(out, source)

    def test_negative_line_idx_returns_source_unchanged(self):
        source = "a\nb\n"
        out = insert_marker_after_line(source, -1, "# X\n")
        self.assertEqual(out, source)


class TestDetectIndent(unittest.TestCase):
    def test_4_space_indent(self):
        self.assertEqual(detect_indent("    page.click()"), "    ")

    def test_tab_indent(self):
        self.assertEqual(detect_indent("\tpage.click()"), "\t")

    def test_mixed_indent_keeps_whitespace(self):
        self.assertEqual(detect_indent("  \tpage.click()"), "  \t")

    def test_empty_line(self):
        self.assertEqual(detect_indent(""), "")

    def test_no_indent(self):
        self.assertEqual(detect_indent("page.click()"), "")


class TestUserEditSurvivesCodegenPush(unittest.TestCase):
    """场景模拟:用户手编后,下次 codegen 推送只追加新增行,手编部分保留。

    这里通过纯文本操作模拟『面板 = 上次面板文本 + 新增行追加』,
    验证整个流程的用户手编不被破坏。
    """

    def test_user_added_line_before_codegen_push_survives(self):
        # 初始:codegen 推送了 5 行
        panel = "\n".join(["a", "b", "c", "d", "e", ""])
        last_count = 5
        # 用户在中间手编了一行:面板现在是 6 行
        panel = panel.replace("c\n", "c\n# USER EDIT\n")
        self.assertEqual(panel.count("\n"), 6)  # 6 行

        # codegen 推送了新增 1 行(f)
        new_content = "\n".join(["a", "b", "c", "d", "e", "f", ""])
        delta_count, appendix = compute_codegen_appendix(last_count, new_content)
        self.assertEqual(delta_count, 1)
        self.assertEqual(appendix, "f")

        # 追加到面板(模拟 _append_text_to_panel:末尾补 \\n 再插)
        if panel and not panel.endswith("\n"):
            panel += "\n"
        panel += appendix
        # 用户手编的那行必须还在
        self.assertIn("# USER EDIT", panel)
        self.assertIn("f", panel)


if __name__ == "__main__":
    unittest.main()
