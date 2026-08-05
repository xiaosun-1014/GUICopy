"""Product-pipeline documentation contract test.

Locks the README to the NEW fixed artifact names so the product docs never
drift back to the old timestamped / png contract:
  - must document the one-click adapter+replica product pipeline;
  - must use the fixed names ``report.jpeg`` and ``dicom_meta.json``;
  - must NOT reference the old ``report_*.png`` / ``dicom_meta_*.json`` patterns.
"""
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class TestPipelineDocumentation(unittest.TestCase):
    def _readme(self) -> str:
        return (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")

    def test_readme_documents_adapter_replica_product_pipeline(self):
        readme = self._readme()
        self.assertIn("生成 Adapter + 离线复刻", readme)

    def test_readme_uses_fixed_report_and_meta_names(self):
        readme = self._readme()
        self.assertIn("report.jpeg", readme)
        self.assertIn("dicom_meta.json", readme)

    def test_readme_has_no_old_timestamped_png_contract(self):
        readme = self._readme()
        self.assertNotIn("report_*.png", readme)
        self.assertNotIn("dicom_meta_*.json", readme)


if __name__ == "__main__":
    unittest.main()
