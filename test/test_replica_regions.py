import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from playwright.sync_api import sync_playwright

from capture_snapshot import capture_marker_interaction_region, discover_series_candidates


class ReplicaRegionTests(unittest.TestCase):
    def test_series_region_harvests_scrollable_items_and_restores_position(self):
        markup = """<style>#series{height:40px;overflow:auto}.item{height:20px}</style>
        <div id='series' class='series-list'>
          <div class='item' data-series='one'>Thin 1.0 400幅</div>
          <div class='item' data-series='two'>Thick 5.0 80幅</div>
          <div class='item' data-series='three'>MPR 120幅</div>
        </div>"""
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.set_content(markup)
            page.locator("#series").evaluate("element => element.scrollTop = 20")
            region = capture_marker_interaction_region(page, "序列选择", "d_series", max_scroll_steps=10)
            restored = page.locator("#series").evaluate("element => element.scrollTop")
            browser.close()

        self.assertEqual(region.region_type, "series")
        self.assertEqual({member.dom.attributes.get("data-series") for member in region.members}, {"one", "two", "three"})
        self.assertEqual(restored, 20)
        self.assertTrue(region.series_collection.reached_end)
        self.assertEqual(region.series_collection.warning, None)

    def test_series_harvest_records_partial_warning_when_budget_is_exhausted(self):
        markup = """<style>#series{height:20px;overflow:auto}.item{height:20px}</style>
        <div id='series' class='series-list'>
          <div class='item' data-series='one'>One</div><div class='item' data-series='two'>Two</div>
        </div>"""
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.set_content(markup)
            region = capture_marker_interaction_region(page, "序列选择", "d_series", max_scroll_steps=0)
            browser.close()

        self.assertTrue(region.series_collection.virtualized)
        self.assertFalse(region.series_collection.reached_end)
        self.assertEqual(region.series_collection.warning, "series_virtualized_partial")

    # ---- Phase 2: discover_series_candidates API ----

    def test_discovery_plain_list_keeps_dom_order_and_selected_state(self):
        markup = """<div id='s'>
          <div class='item' data-series='a' aria-selected='true'>A series</div>
          <div class='item' data-series='b'>B series</div>
          <div class='item' data-series='c'>C series</div>
        </div>"""
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.set_content(markup)
            descriptors, members, evidence = discover_series_candidates(page.locator("#s"), "d1", max_scroll_steps=5)
            browser.close()

        self.assertEqual([d.series_key for d in descriptors], ["a", "b", "c"])
        self.assertEqual([d.ordinal for d in descriptors], [0, 1, 2])
        self.assertEqual([d.label for d in descriptors], ["A series", "B series", "C series"])
        # Descriptor count is auditable against region members with unique, aligned ids.
        self.assertEqual(len(descriptors), len(members))
        self.assertEqual([m.member_id for m in members], [d.member_id for d in descriptors])
        self.assertEqual(len({m.member_id for m in members}), len(members))
        self.assertEqual(evidence.discovered_count, 3)
        # Selected state is correct and the short list reached its end.
        self.assertTrue(descriptors[0].selected)
        self.assertFalse(descriptors[1].selected)
        self.assertTrue(evidence.reached_end)

    def test_discovery_virtualized_list_deduplicates_reused_nodes(self):
        # A single DOM row is reused; as the container scrolls, the row's
        # content cycles one -> two -> three -> one -> ... Discovery must
        # collect each logical series exactly once.
        markup = """<style>#s{height:30px;overflow:auto}#row{height:30px}</style>
        <div id='s'><div id='row' class='item' data-series='one'>Series one</div></div>
        <script>
          const el = document.getElementById('s');
          const row = document.getElementById('row');
          const names = [['one', 'Series one'], ['two', 'Series two'], ['three', 'Series three']];
          el.addEventListener('scroll', () => {
            const idx = Math.floor(el.scrollTop / 30) % 3;
            row.setAttribute('data-series', names[idx][0]);
            row.textContent = names[idx][1];
          });
        </script>"""
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 500, "height": 300})
            page.set_content(markup)
            # Pad container height so scrolling has room while row stays the only item.
            page.locator("#s").evaluate("el => { el.style.height = '30px'; const hold = document.createElement('div'); hold.style.height='500px'; el.appendChild(hold); }")
            descriptors, members, evidence = discover_series_candidates(
                page.locator("#s"), "d_virt", max_scroll_steps=40, max_duration_s=3.0
            )
            browser.close()

        keys = {d.series_key for d in descriptors}
        self.assertEqual(len(descriptors), 3)
        self.assertEqual(keys, {"one", "two", "three"})
        self.assertEqual(evidence.collected_count, 3)
        self.assertEqual(evidence.discovered_count, 3)

    def test_discovery_same_name_series_produce_distinct_keys_via_stable_attrs(self):
        markup = """<div id='s'>
          <div class='item' data-series='Thin' data-series-uid='uid-1'>Thin series</div>
          <div class='item' data-series='Thin' data-series-uid='uid-2'>Thin series</div>
        </div>"""
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.set_content(markup)
            descriptors, _, _ = discover_series_candidates(page.locator("#s"), "d1", max_scroll_steps=5)
            browser.close()

        keys = [d.series_key for d in descriptors]
        self.assertEqual(len(keys), 2)
        self.assertEqual(len(set(keys)), 2)
        self.assertEqual(set(keys), {"uid-1", "uid-2"})

    def test_discovery_no_stable_attribute_fallback_key_is_deterministic(self):
        markup = """<div id='s'><ul>
          <li>Alpha</li><li>Beta</li>
        </ul></div>"""
        def run():
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                page = browser.new_page()
                page.set_content(markup)
                descriptors, _, _ = discover_series_candidates(page.locator("#s"), "d1", max_scroll_steps=5)
                browser.close()
                return [(d.series_key, d.ordinal) for d in descriptors]

        first = run()
        second = run()
        self.assertEqual(len(first), 2)
        self.assertEqual(len(set(key for key, _ in first)), 2)
        # Fully deterministic within (and across) identical captures.
        self.assertEqual(first, second)
        self.assertIn("alpha", first[0][0])
        self.assertIn("d1", first[0][0])

    def test_discovery_budget_exhausted_reports_partial_not_complete(self):
        markup = """<style>#s{height:20px;overflow:auto}.item{height:20px}</style>
        <div id='s'><div class='item' data-series='one'>One</div><div class='item' data-series='two'>Two</div></div>"""
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.set_content(markup)
            descriptors, _, evidence = discover_series_candidates(page.locator("#s"), "d1", max_scroll_steps=0)
            browser.close()

        self.assertFalse(evidence.reached_end)
        self.assertEqual(evidence.warning, "series_virtualized_partial")
        self.assertEqual(evidence.discovered_count, 2)

    def test_discovery_restores_original_scroll_position(self):
        markup = """<style>#s{height:40px;overflow:auto}.item{height:20px}</style>
        <div id='s'>
          <div class='item' data-series='a'>A</div><div class='item' data-series='b'>B</div>
          <div class='item' data-series='c'>C</div><div class='item' data-series='d'>D</div>
        </div>"""
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.set_content(markup)
            page.locator("#s").evaluate("el => el.scrollTop = 20")
            discover_series_candidates(page.locator("#s"), "d1", max_scroll_steps=10)
            restored = page.locator("#s").evaluate("el => el.scrollTop")
            browser.close()

        self.assertEqual(restored, 20)

    # ---- Real-site mirror fixtures (ft / zscloud) — see
    #      test/fixtures/multi_series/README.md "真实站点镜像 fixture" ----

    def test_ft_fixture_discovery_uses_a_span_total_selector(self):
        # FTImage rows are `a > div.desc > span.total` inside the first
        # div.os-viewport; no id/data-* identity attributes, so keys fall
        # back to row text. The 8th row (MPR-Sag_bone) starts below the fold.
        fixture = Path(__file__).parent / "fixtures" / "multi_series" / "ft_series_list.html"
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.goto(fixture.as_uri())
            root = page.locator("[class*=os-viewport]").first
            descriptors, members, evidence = discover_series_candidates(
                root, "ft", item_selector="a:has(span.total)", identity_attrs=[]
            )
            # 默认 _SERIES_ITEM_SELECTOR 不含 `a` → 同一 root 不传参数必须命中 0 个，
            # 证明 ft 必须显式传 item_selector（改默认行为也不会误通过）。
            default_descriptors, _, default_evidence = discover_series_candidates(root, "ft")
            browser.close()

        self.assertEqual(len(descriptors), 8)
        # 无稳定身份 → 文本 fallback，8 行文本各不相同 → key 唯一
        self.assertEqual(len({d.series_key for d in descriptors}), 8)
        self.assertEqual(evidence.discovered_count, 8)
        self.assertEqual(evidence.collected_count, 8)
        self.assertEqual(len(descriptors), len(members))
        self.assertTrue(evidence.reached_end)
        self.assertEqual(evidence.warning, None)
        # 滚动后出现的行也被枚举（文本 fallback 的 label 保留原文）
        self.assertTrue(any("MPR-Sag_bone" in d.label for d in descriptors))
        self.assertEqual(len({m.member_id for m in members}), len(members))
        # 默认选择器命中 0 个
        self.assertEqual(len(default_descriptors), 0)
        self.assertEqual(default_evidence.discovered_count, 0)

    def test_ft_fixture_interfering_viewport_counts_nothing(self):
        # The real page has TWO os-viewports; only the first holds series rows.
        # Container scoping must keep the interfering one at zero even with the
        # ft selector (it contains no a:has(span.total) series rows).
        fixture = Path(__file__).parent / "fixtures" / "multi_series" / "ft_series_list.html"
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.goto(fixture.as_uri())
            interfering = page.locator("[class*=os-viewport]").nth(1)
            descriptors, _, evidence = discover_series_candidates(
                interfering, "ft", item_selector="a:has(span.total)", identity_attrs=[]
            )
            browser.close()

        self.assertEqual(len(descriptors), 0)
        self.assertEqual(evidence.discovered_count, 0)

    def test_zs_fixture_discovery_scopes_to_studylist_container(self):
        # zscloud rows are li.ui-draggable[id] inside #HLeftThumnail; the id is
        # a fictional SeriesInstanceUID-shaped value and becomes the series_key.
        # Out-of-container li nodes (patient-header / examination LI) must NOT be
        # counted when the root is bounded to the StudyList container.
        fixture = Path(__file__).parent / "fixtures" / "multi_series" / "zs_series_list.html"
        expected_uids = {
            "1.2.826.0.1.3680043.201.1001",
            "1.2.826.0.1.3680043.201.1002",
            "1.2.826.0.1.3680043.201.1003",
            "1.2.826.0.1.3680043.201.1004",
        }
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.goto(fixture.as_uri())
            root = page.locator("#HLeftThumnail")
            descriptors, members, evidence = discover_series_candidates(
                root, "zs", item_selector="li.ui-draggable", identity_attrs=["id"]
            )
            browser.close()

        keys = [d.series_key for d in descriptors]
        self.assertEqual(len(descriptors), 4)
        # 每个 series_key 都是虚构 UID 且唯一
        self.assertEqual(set(keys), expected_uids)
        self.assertEqual(len(set(keys)), 4)
        self.assertEqual(evidence.discovered_count, 4)
        self.assertEqual(len(descriptors), len(members))
        # 容器外的干扰 li（.9001 / .9002）不计入
        self.assertNotIn("1.2.826.0.1.3680043.201.9001", keys)
        self.assertNotIn("1.2.826.0.1.3680043.201.9002", keys)

    def test_series_scope_root_prefers_first_visible_configured_container(self):
        # Step 1 container coverage: `_series_scope_root` with a container
        # selector must resolve the FIRST visible div.os-viewport (owning the
        # target a:has(span.total) row) — never the interfering second
        # os-viewport further down the page, and never the body fallback.
        from batch_capture_replicate import _series_scope_root
        fixture = Path(__file__).parent / "fixtures" / "multi_series" / "ft_series_list.html"
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.goto(fixture.as_uri())
            root = _series_scope_root(page.locator("a:has(span.total)").first, "div.os-viewport")
            text = root.evaluate("el => el.textContent")
            tag = root.evaluate("el => el.tagName")
            os_class = root.evaluate("el => el.className")
            browser.close()

        self.assertEqual(tag, "DIV")
        self.assertEqual(os_class, "os-viewport")
        # 命中第一个（序列）容器而非干扰容器
        self.assertIn("Scout", text)
        self.assertNotIn("Interfering", text)

    # ---- _series_viewer_config_for URL matching (batch_capture_replicate) ----

    def test_series_viewer_config_matches_known_viewer_urls(self):
        from batch_capture_replicate import _series_viewer_config_for

        ft_cfg = _series_viewer_config_for(SimpleNamespace(url="https://yyx.ftimage.cn/dimage/index.html"))
        self.assertEqual(ft_cfg.get("item_container_selector"), "div.os-viewport")
        self.assertEqual(ft_cfg.get("item_selector"), "a:has(span.total)")
        self.assertEqual(ft_cfg.get("identity_attrs"), [])

        zs_cfg = _series_viewer_config_for(SimpleNamespace(url="https://zscloud.zs-hospital.sh.cn/film/#/shared"))
        self.assertEqual(zs_cfg.get("item_container_selector"), "#HLeftThumnail")
        self.assertEqual(zs_cfg.get("item_selector"), "li.ui-draggable")
        self.assertEqual(zs_cfg.get("identity_attrs"), ["id"])

    def test_series_viewer_config_unknown_url_or_missing_url_returns_empty(self):
        from batch_capture_replicate import _series_viewer_config_for

        # 未知 viewer → {}（回退硬编码默认）
        self.assertEqual(_series_viewer_config_for(SimpleNamespace(url="https://example.com/film/")), {})
        # stub page 无 url 属性 → {}（不因缺 URL 抛错）
        self.assertEqual(_series_viewer_config_for(SimpleNamespace()), {})

    def test_series_viewer_config_returns_empty_on_broken_yaml(self):
        from batch_capture_replicate import _series_viewer_config_for

        # 坏 YAML / 缺失文件 → {}（配置坏了也绝不中断捕获）
        with tempfile.TemporaryDirectory() as tmp:
            bad_path = Path(tmp) / "viewers.yaml"
            bad_path.write_text("viewers: [", encoding="utf-8")
            with patch("batch_capture_replicate._SERIES_VIEWERS_YAML", bad_path):
                cfg = _series_viewer_config_for(SimpleNamespace(url="https://yyx.ftimage.cn/dimage/index.html"))
            self.assertEqual(cfg, {})
            missing = Path(tmp) / "no-such-viewers.yaml"
            with patch("batch_capture_replicate._SERIES_VIEWERS_YAML", missing):
                cfg = _series_viewer_config_for(SimpleNamespace(url="https://yyx.ftimage.cn/dimage/index.html"))
            self.assertEqual(cfg, {})


if __name__ == "__main__":
    unittest.main()
