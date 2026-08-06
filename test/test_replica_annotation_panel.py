import sys
import unittest

from PyQt6.QtWidgets import QApplication

from replica_annotation_panel import ReplicaAnnotationPanel
from rewrite_script import parse_action_plan


APP = QApplication.instance() or QApplication(sys.argv)


class ReplicaAnnotationPanelTests(unittest.TestCase):
    def setUp(self):
        self.panel = ReplicaAnnotationPanel()
        self.panel.set_editable(True)
        self.source = '''# [MARKER: 序列选择]
page.locator(".series").nth(2).dblclick()
# [MARKER: 影像画布交互]
page.mouse.click(819, 318)
'''
        self.plan = parse_action_plan(self.source)
        self.panel.set_plan(self.source, self.plan)

    def tearDown(self):
        self.panel.close()

    def test_groups_actions_by_marker_and_filters_high_risk(self):
        self.assertEqual(self.panel.tree.topLevelItemCount(), 2)
        self.panel.high_risk_only.setChecked(True)
        self.assertEqual(self.panel.tree.topLevelItemCount(), 2)

    def test_locator_selection_previews_risk_without_mutating_source(self):
        locator_item = self.panel.tree.topLevelItem(0).child(0)
        self.panel.tree.setCurrentItem(locator_item)
        self.panel.expression_editor.setPlainText(
            'page.get_by_test_id("series-primary")'
        )

        self.assertIn("ordinal", self.panel.risk_label.text())
        self.assertIn("stable_attribute", self.panel.risk_label.text())
        self.assertTrue(self.panel.apply_button.isEnabled())
        self.assertEqual(self.panel.source, self.source)
        self.assertEqual(
            locator_item.foreground(2).color().name(),
            "#d97706",
        )

    def test_invalid_expression_disables_apply_and_shows_reason(self):
        locator_item = self.panel.tree.topLevelItem(0).child(0)
        self.panel.tree.setCurrentItem(locator_item)
        self.panel.expression_editor.setPlainText("page.locator(selector)")

        self.assertFalse(self.panel.apply_button.isEnabled())
        self.assertIn("static literal", self.panel.error_label.text())

    def test_coordinate_action_is_read_only(self):
        coordinate_item = self.panel.tree.topLevelItem(1).child(0)
        self.panel.tree.setCurrentItem(coordinate_item)

        self.assertTrue(self.panel.expression_editor.isReadOnly())
        self.assertFalse(self.panel.apply_button.isEnabled())
        self.assertIn("coordinate", self.panel.error_label.text())

    def test_apply_emits_action_id_and_expression(self):
        received = []
        self.panel.locator_apply_requested.connect(
            lambda action_id, expression: received.append(
                (action_id, expression)
            )
        )
        locator_item = self.panel.tree.topLevelItem(0).child(0)
        self.panel.tree.setCurrentItem(locator_item)
        self.panel.expression_editor.setPlainText(
            'page.get_by_test_id("series-primary")'
        )
        self.panel.apply_button.click()

        self.assertEqual(
            received,
            [("a_000_001", 'page.get_by_test_id("series-primary")')],
        )


if __name__ == "__main__":
    unittest.main()
