"""复现 marker 位置异常的根因测试。

针对 main_gui.py 当前同步逻辑的三个 bug:
  Bug 1: _on_code_ready 用「整行原文」当锚点比对,codegen 改写锚点行后
         marker 飞到面板末尾(test_anchor_rewritten_marker_drifts_to_end)。
  Bug 2: _delete_current_line 删了 _display_items 但不清 _marker_anchors,
         下次 codegen 推送时被删的 marker 又被重新插回(test_deleted_marker_resurrected_on_push)。
  Bug 3: 多个相同行(空行 / 重复动作行)时 anchor_text 命中第一个匹配,
         marker 飞到错误的第一个位置(test_duplicate_anchor_line_wrong_position)。

这些测试在「当前未修复代码」下应当 FAIL,用于锁定根因;
修复后应当 PASS。
"""
import sys
import unittest

from PyQt6.QtWidgets import QApplication

from main_gui import MainWindow
from markers import DEFAULT_MARKERS

_app = QApplication.instance() or QApplication(sys.argv)


# 一个带收尾段的完整 codegen 模板,贴近真实录制输出
BASE = (
    "from playwright.sync_api import Playwright, sync_playwright, expect\n"
    "\n"
    "\n"
    "def run(playwright: Playwright) -> None:\n"
    "    browser = playwright.chromium.launch(headless=False)\n"
    "    context = browser.new_context()\n"
    "    page = context.new_page()\n"
    "    page.goto(\"https://example.com\")\n"
    "    page.get_by_text(\"查看\").click()\n"
    "\n"
    "    # ---------------------\n"
    "    context.close()\n"
    "    browser.close()\n"
    "\n"
    "\n"
    "with sync_playwright() as playwright:\n"
    "    run(playwright)\n"
)


def _make_win():
    win = MainWindow()
    win._on_code_ready(BASE)
    return win


def _cursor_at(win, line_idx):
    doc = win.code_view.document()
    block = doc.findBlockByNumber(line_idx)
    if block.isValid():
        cursor = win.code_view.textCursor()
        cursor.setPosition(block.position())
        win.code_view.setTextCursor(cursor)


class TestAnchorRewrittenDrift(unittest.TestCase):
    """Bug 1:锚点行被 codegen 改写后,marker 不应飞到末尾。"""

    def setUp(self):
        self.win = _make_win()

    def tearDown(self):
        self.win.close()

    def test_anchor_rewritten_marker_drifts_to_end(self):
        # 1) 在 page.goto 行后插入 marker
        base_lines = BASE.splitlines()
        goto_idx = next(i for i, l in enumerate(base_lines) if 'page.goto' in l)
        _cursor_at(self.win, goto_idx)
        self.win._insert_marker(DEFAULT_MARKERS[0])

        # 确认 marker 紧跟 page.goto
        panel = self.win.code_view.toPlainText()
        lines = panel.splitlines()
        m_idx = next(i for i, l in enumerate(lines) if 'MARKER: 报告截图' in l)
        self.assertEqual(lines[m_idx - 1], '    page.goto("https://example.com")')

        # 2) codegen 推送 V2:把 page.goto 改写为带 trailing 空格 + 换行差异,
        #    模拟 playwright 重新格式化同一行(anchor_text 整行比对会失败)
        v2_lines = BASE.splitlines()
        v2_lines[goto_idx] = '    page.goto("https://example.com")  '  # 末尾两个空格
        v2 = "\n".join(v2_lines)

        self.win._on_code_ready(v2)
        panel2 = self.win.code_view.toPlainText()
        lines2 = panel2.splitlines()

        # marker 仍应紧跟在(改写后的)page.goto 行之后,而不是飞到末尾
        goto_idx2 = next(i for i, l in enumerate(lines2) if 'page.goto' in l)
        m_idx2 = next(i for i, l in enumerate(lines2) if 'MARKER: 报告截图' in l)
        self.assertEqual(
            m_idx2, goto_idx2 + 1,
            f"BUG 1: marker 飞到 {m_idx2},应在 {goto_idx2 + 1}(锚点行被改写后丢失重定位)"
        )


class TestDeletedMarkerResurrected(unittest.TestCase):
    """Bug 2:删除 marker 行后,下次 codegen 推送不应让它复活。"""

    def setUp(self):
        self.win = _make_win()

    def tearDown(self):
        self.win.close()

    def test_deleted_marker_resurrected_on_push(self):
        # 1) 插入 marker 在 page.goto 后
        base_lines = BASE.splitlines()
        goto_idx = next(i for i, l in enumerate(base_lines) if 'page.goto' in l)
        _cursor_at(self.win, goto_idx)
        self.win._insert_marker(DEFAULT_MARKERS[0])
        self.assertIn("[MARKER: 报告截图", self.win.code_view.toPlainText())

        # 2) 删除 marker 所在行(把光标移到 marker 行,调用 _delete_current_line)
        _cursor_at(self.win, goto_idx + 1)
        self.win._delete_current_line()
        self.assertNotIn(
            "[MARKER: 报告截图", self.win.code_view.toPlainText(),
            "删除后 marker 应已从面板消失"
        )

        # 3) codegen 推送一个仅末尾新增行(保持锚点行不变)
        v2_lines = BASE.splitlines()
        insert_at = goto_idx + 1  # page.goto 后插一行新动作
        v2_lines[insert_at:insert_at] = ['    page.wait_for_timeout(500)']
        v2 = "\n".join(v2_lines)

        self.win._on_code_ready(v2)
        panel = self.win.code_view.toPlainText()

        self.assertNotIn(
            "[MARKER: 报告截图", panel,
            "BUG 2: 已删除的 marker 在 codegen 推送后又复活了"
        )


class TestDuplicateAnchorLine(unittest.TestCase):
    """Bug 3:锚点行文本在 codegen 输出里有重复时,marker 不应错位。"""

    def setUp(self):
        self.win = _make_win()

    def tearDown(self):
        self.win.close()

    def test_duplicate_anchor_line_wrong_position(self):
        # 构造一个有两处 'page.goto' 的场景:第二处才是真正录制的新动作。
        # 用一个 anchor 含重复行的 codegen 推送初始化面板。
        base_with_dup = BASE.replace(
            '    page.get_by_text("查看").click()\n',
            '    page.get_by_text("查看").click()\n'
            '    page.goto("https://example.com")\n',  # 第二个 goto
            1,
        )
        self.win._panel_initialized = False
        self.win._display_items.clear()
        self.win._marker_anchors.clear()
        self.win._on_code_ready(base_with_dup)

        lines = base_with_dup.splitlines()
        # 第二个 page.goto 的行号(这是用户真正想锚定的新动作)
        goto_idxs = [i for i, l in enumerate(lines) if 'page.goto' in l]
        self.assertGreaterEqual(len(goto_idxs), 2, "前置:应有 2 个 page.goto")
        second_goto_idx = goto_idxs[-1]

        _cursor_at(self.win, second_goto_idx)
        self.win._insert_marker(DEFAULT_MARKERS[0])

        # 推送一个保持两处 goto 不变的 V2(末尾新增一行)
        v2_lines = base_with_dup.splitlines()
        v2_lines.append('    page.wait_for_timeout(500)')
        v2 = "\n".join(v2_lines)
        self.win._on_code_ready(v2)

        panel = self.win.code_view.toPlainText()
        final_lines = panel.splitlines()
        goto_final_idxs = [i for i, l in enumerate(final_lines) if 'page.goto' in l]
        m_idx = next(i for i, l in enumerate(final_lines) if 'MARKER: 报告截图' in l)

        # marker 应紧跟在【第二个】page.goto 之后(用户当时光标所在处),
        # 而不是第一个(goto_final_idxs[0] + 1)
        self.assertEqual(
            m_idx, goto_final_idxs[-1] + 1,
            f"BUG 3: marker 在 {m_idx},应在第二个 goto({goto_final_idxs[-1]}) 之后,"
            f"而非第一个({goto_final_idxs[0]})"
        )


if __name__ == "__main__":
    unittest.main()
