import importlib.util
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
GENERATOR_PATH = (
    PROJECT_ROOT
    / "skills"
    / "marker-meta-extract"
    / "scripts"
    / "extract_dicom_meta.py"
)


def _load_generator():
    spec = importlib.util.spec_from_file_location("test_meta_generator", GENERATOR_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载 Meta 生成器: {GENERATOR_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


META_GENERATOR = _load_generator()


class MarkerMetaGeneratorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.viewers = META_GENERATOR.load_viewers(
            PROJECT_ROOT / "skills" / "_shared" / "viewers.yaml"
        )

    def test_ftimage_open_steps_parse_as_two_complete_mappings(self):
        steps = self.viewers["ftimage"].meta_panel["open_steps"]

        self.assertEqual(len(steps), 2)
        self.assertEqual(steps[0]["method"], "get_by_title")
        self.assertEqual(steps[0]["value"], "更多")
        self.assertEqual(
            steps[0]["expect_visible"]["selector"],
            "#moreBox a.tool.tool-tags[data-tool='tags']",
        )
        self.assertEqual(steps[1]["selector"], "#moreBox a.tool.tool-tags[data-tool='tags']")
        self.assertEqual(steps[1]["expect_visible"]["selector"], "#tagsBox")

    def test_ftimage_generated_open_steps_are_more_then_tags_then_panel(self):
        steps = self.viewers["ftimage"].meta_panel["open_steps"]
        generated = "\n".join(META_GENERATOR._open_steps_code("page", steps))

        more = generated.index('page.get_by_title("更多")')
        tags = generated.index("#moreBox a.tool.tool-tags")
        panel = generated.index('page.locator("#tagsBox")')
        self.assertLess(more, tags)
        self.assertLess(tags, panel)
        self.assertIn('wait_for(state="visible"', generated)

    def test_generic_inline_empty_iframe_selectors_is_a_list(self):
        generic = self.viewers["generic"]

        self.assertEqual(generic.iframe_selectors, [])
        self.assertIsInstance(generic.iframe_selectors, list)

    def test_zscloud_identity_attrs_inline_list_is_a_list(self):
        identity_attrs = self.viewers["zscloud"].sequence_select["identity_attrs"]

        self.assertEqual(identity_attrs, ["id"])
        self.assertIsInstance(identity_attrs, list)

    def test_open_steps_support_four_space_mapping_continuations(self):
        parsed = META_GENERATOR.MiniYaml.parse(
            """viewers:
  custom:
    meta_panel:
      open_steps:
        - method: "get_by_title"
            value: "More"
            expect_visible:
                method: "locator"
                selector: "#tags"
        - method: "locator"
            selector: "#tags"
            expect_visible:
                method: "locator"
                selector: "#tagsBox"
"""
        )
        steps = parsed["viewers"]["custom"]["meta_panel"]["open_steps"]

        self.assertEqual(steps[0]["value"], "More")
        self.assertEqual(steps[0]["expect_visible"]["selector"], "#tags")
        self.assertEqual(steps[1]["selector"], "#tags")
        self.assertEqual(steps[1]["expect_visible"]["selector"], "#tagsBox")

    def test_missing_open_steps_keeps_legacy_button_name_generation(self):
        viewer = META_GENERATOR.ViewerConfig(
            name="legacy",
            url_patterns=[],
            iframe_selectors=[],
            meta_panel={"open_button_names": ["Metadata"]},
            sequence_select={},
        )
        context = META_GENERATOR.MarkerContext(
            ts="20260819_120000",
            line_no=0,
            page_var="page",
            goto_urls=[],
            existing_locators=[],
        )
        generated = META_GENERATOR.generate_replacement_code(context, viewer)

        self.assertIn("for _btn_name in [", generated)
        self.assertIn('"Metadata"', generated)
        self.assertNotIn("_meta_open_step_0", generated)


if __name__ == "__main__":
    unittest.main()
