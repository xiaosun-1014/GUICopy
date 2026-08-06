import ast
import unittest
from pathlib import Path

from rewrite_script import locator_risk_report, parse_action_plan


ROOT = Path(__file__).resolve().parents[1]


class ReplicaActionParserTests(unittest.TestCase):
    def _parse(self, hospital: str):
        source = (ROOT / "out" / hospital / f"processed_script_{hospital}.py").read_text(encoding="utf-8")
        return parse_action_plan(source)

    def test_uicloud_popup_and_frame_actions_are_grouped_by_marker(self):
        plan = self._parse("uicloud")

        self.assertEqual(len(plan.popup_expectations), 1)
        popup = plan.popup_expectations[0]
        self.assertEqual(popup.source_page_var, "page")
        self.assertEqual(popup.result_page_var, "page1")
        sequence = next(group for group in plan.marker_groups if group.marker_label == "序列选择")
        action = sequence.actions[0]
        self.assertEqual(action.action_type, "dblclick")
        self.assertEqual(action.locator.page_var, "page1")
        self.assertEqual(action.locator.frame_chain[0].selector, '[id="2d-iframe"]')
        self.assertEqual(action.locator.locator_kind, "text")

    def test_cxhospital_nested_frames_and_ordinals_are_parsed(self):
        plan = self._parse("cxhospital")

        layout = next(group for group in plan.marker_groups if group.marker_label == "序列布局切换")
        action = layout.actions[0]
        self.assertEqual([hop.selector for hop in action.locator.frame_chain], ["#iframe", 'iframe[name="imageFrame"]'])
        self.assertEqual(action.locator.ordinal_op, "first")
        self.assertEqual(action.locator.locator_kind, "css")
        wlww = next(group for group in plan.marker_groups if group.marker_label == "窗宽窗位 WL/WW")
        self.assertEqual(wlww.actions[3].action_type, "fill")
        self.assertEqual(wlww.actions[3].locator.ordinal_op, "first")

    def test_bootstrap_and_non_locator_actions_have_explicit_policy(self):
        source = '''from playwright.sync_api import Playwright\n\ndef run(playwright: Playwright):\n    page = playwright.chromium.launch().new_page()\n    page.goto("https://example.test")\n    # [MARKER: 影像画布交互]\n    page.keyboard.press("ArrowDown")\n    page.mouse.move(10, 20)\n    page.mouse.dblclick(30, 40)\n    page.locator("canvas").click()\n'''
        plan = parse_action_plan(source)

        self.assertGreater(plan.bootstrap.source_end_line, plan.bootstrap.source_start_line)
        group = plan.marker_groups[0]
        self.assertEqual([action.action_source_kind for action in group.actions[:3]], ["keyboard", "mouse_xy", "mouse_xy"])
        self.assertTrue(all(action.replay_policy == "execute" for action in group.actions[:3]))
        self.assertIsNone(group.actions[0].locator)
        ast.parse(plan.instrumented_source)

    def test_locator_risk_report_accounts_for_locator_classes(self):
        report = locator_risk_report(self._parse("cxhospital"))

        self.assertGreater(report["ordinal"], 0)
        self.assertGreater(report["stable_id"], 0)
        self.assertGreaterEqual(report["non_locator"], 0)

    def test_get_by_title_action_is_preserved_for_offline_replay(self):
        source = '''def run(page):
    # [MARKER: Meta 信息工具]
    page.get_by_title("更多").click()
'''

        plan = parse_action_plan(source)

        action = plan.marker_groups[0].actions[0]
        self.assertEqual(action.action_type, "click")
        self.assertEqual(action.locator.locator_kind, "title")
        self.assertEqual(action.locator.locator_args["args"], ["更多"])

    def test_locator_action_preserves_literal_position_mapping(self):
        source = '''# [MARKER: 影像画布交互]
page.locator("canvas").click(position={"x": 422, "y": 419})
'''

        action = parse_action_plan(source).marker_groups[0].actions[0]

        self.assertEqual(action.action_args["position"], {"x": 422, "y": 419})

    def test_gui_marker_uuid_is_preserved_in_groups_and_actions(self):
        source = '# [MARKER: 报告截图]\npage.locator("#report").click()\n'
        annotations = {
            "markers": [{
                "marker_id": "4f0df6de-71e9-4e3e-a186-f64be41d12fd",
                "line": 1,
                "label": "报告截图",
            }]
        }
        plan = parse_action_plan(source, annotations["markers"])
        self.assertEqual(
            plan.marker_groups[0].marker_id,
            "4f0df6de-71e9-4e3e-a186-f64be41d12fd",
        )
        self.assertEqual(
            plan.marker_groups[0].actions[0].marker_id,
            "4f0df6de-71e9-4e3e-a186-f64be41d12fd",
        )


if __name__ == "__main__":
    unittest.main()
