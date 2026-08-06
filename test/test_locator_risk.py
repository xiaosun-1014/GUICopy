import unittest

from locator_risk import LOCATOR_RISK_ORDER, classify_locator_risk
from replica_models import ActionTarget, LocatorRecipe, Point


def locator_target(
    kind: str,
    argument: str,
    *,
    expression: str | None = None,
    ordinal_op: str | None = None,
) -> ActionTarget:
    source = expression or f"page.locator({argument!r})"
    locator = LocatorRecipe(
        source_expression=source,
        page_var="page",
        frame_chain=[],
        locator_kind=kind,
        locator_args={"args": [argument]},
        ordinal_op=ordinal_op,
        ordinal_value=2 if ordinal_op == "nth" else None,
    )
    return ActionTarget(
        "a_000_001", "marker-1", "click", "locator", {},
        locator, None, None, None, None, "execute", None, "d_main", None,
    )


class LocatorRiskTests(unittest.TestCase):
    def test_risk_order_has_every_persisted_bucket(self):
        self.assertEqual(
            list(LOCATOR_RISK_ORDER),
            [
                "stable_id",
                "aria",
                "stable_attribute",
                "text",
                "ordinal",
                "structural",
                "coordinate",
            ],
        )

    def test_semantic_locator_kinds_have_distinct_buckets(self):
        cases = [
            (locator_target("role", "button"), "aria"),
            (locator_target("label", "Patient ID"), "aria"),
            (locator_target("title", "More"), "aria"),
            (locator_target("test_id", "open-viewer"), "stable_attribute"),
            (locator_target("text", "Body 1.0"), "text"),
        ]
        for target, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(classify_locator_risk(target), expected)

    def test_css_rules_distinguish_direct_stable_and_structural_selectors(self):
        cases = [
            ("#report", "stable_id"),
            ('[id="report"]', "stable_id"),
            ('[data-testid="report"]', "stable_attribute"),
            ('[name="accession"]', "stable_attribute"),
            (".toolbar > button:nth-child(2)", "structural"),
            ('div[class="toolbar"] button', "structural"),
        ]
        for selector, expected in cases:
            with self.subTest(selector=selector):
                self.assertEqual(classify_locator_risk(locator_target("css", selector)), expected)

    def test_ordinal_coordinate_and_non_locator_are_distinct(self):
        ordinal = locator_target(
            "css",
            ".series",
            expression='page.locator(".series").nth(2)',
            ordinal_op="nth",
        )
        coordinate = ActionTarget(
            "a_mouse", "marker-1", "click", "mouse_xy", {"args": [10, 20]},
            None, None, None, Point(10, 20, "page_viewport_css"), None,
            "execute", None, "d_main", None,
        )
        keyboard = ActionTarget(
            "a_key", "marker-1", "press", "keyboard", {},
            None, None, None, None, "ArrowDown",
            "execute", None, "d_main", None,
        )
        self.assertEqual(classify_locator_risk(ordinal), "ordinal")
        self.assertEqual(classify_locator_risk(coordinate), "coordinate")
        self.assertEqual(classify_locator_risk(keyboard), "non_locator")


if __name__ == "__main__":
    unittest.main()
