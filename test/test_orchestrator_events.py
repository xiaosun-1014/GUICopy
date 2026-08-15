"""orchestrator_events 纯逻辑模块的 Level-1 单测（无 Qt/浏览器/子进程）。

覆盖已批准规格 2026-08-05-gui-orchestrator-event-protocol.md 的验收点：
- ready 解析、未知 event 透传、非法行 → None
- normalize_child_event payload 转发 + 保留终态名改名（不入顶层 event）
- MarkerTracker upsert→counts 重算 + summary 覆盖
- TerminalGuard D4 终态唯一性（fatal/completed 顺序与次数）
- redact seeded registry 脱敏
"""
import json
import unittest

from orchestrator_events import (
    ORCHESTRATOR_EVENT_NAMES,
    RESERVED_TERMINAL_NAMES,
    SERIES_EVENT_NAMES,
    MarkerTracker,
    SeriesTracker,
    TerminalGuard,
    normalize_child_event,
    parse_envelope,
    ready_event,
    redact,
)


class TestParseEnvelope(unittest.TestCase):
    def test_ready_event_parses_with_correct_fields(self):
        line = '{"event": "ready", "version": 1, "run_id": "run_abc"}'
        obj = parse_envelope(line)
        self.assertIsNotNone(obj)
        self.assertEqual(obj["event"], "ready")
        self.assertEqual(obj["version"], 1)
        self.assertEqual(obj["run_id"], "run_abc")

    def test_ready_event_helper_matches_schema(self):
        ev = ready_event("run_abc")
        self.assertEqual(parse_envelope('{"event": "ready", "version": 1, "run_id": "run_abc"}'),
                         ev)

    def test_unknown_event_passthrough(self):
        # 前向兼容：未知 event 种类不崩、原样透传（§2）
        line = '{"event": "future_event", "version": 1, "payload": {"x": 1}}'
        obj = parse_envelope(line)
        self.assertIsNotNone(obj)
        self.assertEqual(obj["event"], "future_event")
        self.assertEqual(obj["payload"], {"x": 1})

    def test_malformed_json_returns_none(self):
        for bad in ("", "not json", "{", '{"event": "ready",', "123", "null",
                    '["a", "b"]', "\n  not json\n"):
            self.assertIsNone(parse_envelope(bad), msg=repr(bad))

    def test_non_string_input_returns_none(self):
        self.assertIsNone(parse_envelope(b"not str"))
        self.assertIsNone(parse_envelope(None))


class TestNormalizeChildEvent(unittest.TestCase):
    def test_child_event_forwarded_into_payload_top_level_copies_name(self):
        child = {"event": "auth_required", "message": "请完成登录后继续"}
        ev = normalize_child_event(child, "awaiting_auth", "run_1")
        self.assertEqual(ev["event"], "auth_required")
        self.assertEqual(ev["source"], "subprocess:auth_required")
        self.assertEqual(ev["payload"], child)  # 原始 JSON 原样承载
        # envelope 规范字段
        self.assertEqual(ev["version"], 1)
        self.assertEqual(ev["run_id"], "run_1")
        self.assertEqual(ev["stage"], "awaiting_auth")
        self.assertTrue("ts" in ev and ev["ts"])

    def test_capture_and_build_events_forwarded(self):
        for name in ("capture_started", "build_finished", "action_failed"):
            child = {"event": name, "entrypoint": "x"}
            ev = normalize_child_event(child, "capturing_live", "r")
            self.assertEqual(ev["event"], name, name)
            self.assertEqual(ev["payload"], child)

    def test_child_completed_renamed_not_top_level_terminal(self):
        child = {"event": "completed", "entrypoint": ".../index.html"}
        ev = normalize_child_event(child, "building_replica", "r")
        self.assertEqual(ev["event"], "capture_completed")
        self.assertNotEqual(ev["event"], "completed")
        self.assertEqual(ev["payload"], child)

    def test_child_failed_renamed_not_top_level_terminal(self):
        child = {"event": "failed", "reason": "series 采集失败"}
        ev = normalize_child_event(child, "capturing_live", "r")
        self.assertEqual(ev["event"], "capture_failed")
        self.assertNotEqual(ev["event"], "failed")
        self.assertEqual(ev["payload"], child)

    def test_child_fatal_renamed(self):
        child = {"event": "fatal", "message": "child 内部错误"}
        ev = normalize_child_event(child, "capturing_live", "r")
        self.assertEqual(ev["event"], "capture_fatal")
        self.assertNotEqual(ev["event"], "fatal")

    def test_child_other_orchestrator_reserved_names_renamed(self):
        # §4：child 事件名不得与任何 orchestrator 级事件名撞名
        for name in ("summary", "stage_started", "ready", "log", "marker_result"):
            child = {"event": name}
            ev = normalize_child_event(child, "capturing_live", "r")
            self.assertNotEqual(ev["event"], name, name)
            self.assertEqual(ev["event"], f"capture_{name}", name)


class TestMarkerTracker(unittest.TestCase):
    def test_upsert_recomputes_success_to_partial(self):
        tracker = MarkerTracker()
        tracker.upsert({"marker_id": "m1", "label": "影像画布", "status": "success"})
        self.assertEqual(tracker.counts(), {"success": 1, "partial": 0, "failed": 0, "skipped": 0})
        # 同一 marker 更新为 partial → 计数由 success=1,partial=0 变为 success=0,partial=1
        tracker.upsert({"marker_id": "m1", "label": "影像画布", "status": "partial"})
        self.assertEqual(tracker.counts(), {"success": 0, "partial": 1, "failed": 0, "skipped": 0})

    def test_multiple_markers_accumulate_independently(self):
        tracker = MarkerTracker()
        tracker.upsert({"marker_id": "m1", "status": "success"})
        tracker.upsert({"marker_id": "m2", "status": "failed"})
        tracker.upsert({"marker_id": "m3", "status": "skipped"})
        self.assertEqual(tracker.counts(),
                         {"success": 1, "partial": 0, "failed": 1, "skipped": 1})

    def test_overwrite_summary_is_authoritative(self):
        tracker = MarkerTracker()
        tracker.upsert({"marker_id": "m1", "status": "success"})
        tracker.upsert({"marker_id": "m2", "status": "partial"})
        # 权威快照覆盖（§5.5），非累加
        tracker.overwrite({"success": 2, "partial": 1, "failed": 0, "skipped": 0})
        self.assertEqual(tracker.counts(), {"success": 2, "partial": 1, "failed": 0, "skipped": 0})

    def test_overwrite_with_partial_summary_fills_missing_with_zero(self):
        tracker = MarkerTracker()
        tracker.overwrite({"success": 1})
        self.assertEqual(tracker.counts(),
                         {"success": 1, "partial": 0, "failed": 0, "skipped": 0})

    def test_upsert_after_overwrite_reverts_to_recompute(self):
        # 语义（D3）：新的 marker_result 回归「从最新明细重算」，summary 覆盖只在
        # 收到 summary 后生效；此后若再有 marker_result 则以明细为准。
        tracker = MarkerTracker()
        tracker.overwrite({"success": 9, "partial": 9, "failed": 9, "skipped": 9})
        tracker.upsert({"marker_id": "m1", "status": "partial"})
        self.assertEqual(tracker.counts(),
                         {"success": 0, "partial": 1, "failed": 0, "skipped": 0})

    def test_upsert_missing_marker_id_raises(self):
        tracker = MarkerTracker()
        with self.assertRaises(ValueError):
            tracker.upsert({"status": "success"})

    def test_marker_upsert_with_unknown_status_ignored_in_count(self):
        tracker = MarkerTracker()
        tracker.upsert({"marker_id": "m1", "status": "weird"})
        self.assertEqual(tracker.counts(),
                         {"success": 0, "partial": 0, "failed": 0, "skipped": 0})


class TestTerminalGuard(unittest.TestCase):
    def test_duplicate_fatal_rejected(self):
        guard = TerminalGuard()
        guard.note("fatal")
        with self.assertRaises(ValueError):
            guard.note("fatal")

    def test_duplicate_completed_rejected(self):
        guard = TerminalGuard()
        guard.note("completed")
        with self.assertRaises(ValueError):
            guard.note("completed")

    def test_completed_then_fatal_rejected(self):
        guard = TerminalGuard()
        guard.note("completed")
        with self.assertRaises(ValueError):
            guard.note("fatal")

    def test_fatal_then_regular_event_rejected(self):
        guard = TerminalGuard()
        guard.note("fatal")
        with self.assertRaises(ValueError):
            guard.note("stage_finished")
        with self.assertRaises(ValueError):
            guard.note("progress")

    def test_legal_sequence_fatal_summary_completed_passes(self):
        guard = TerminalGuard()
        guard.note("stage_started")
        guard.note("fatal")
        guard.note("summary")  # fatal 后只允许 summary/completed
        guard.note("summary")  # 允许多条? fatal 后 summary 仍被允许（仅限这两类）
        guard.note("completed")
        self.assertTrue(guard.certify())

    def test_fatal_then_completed_then_no_summary_still_notes_ok(self):
        guard = TerminalGuard()
        guard.note("fatal")
        guard.note("completed")
        self.assertTrue(guard.certify())

    def test_legal_success_completed_passes(self):
        for status_seq in (("completed",), ("stage_finished", "summary", "completed")):
            guard = TerminalGuard()
            for kind in status_seq:
                guard.note(kind)
            self.assertTrue(guard.certify(), status_seq)

    def test_regular_event_after_completed_rejected(self):
        # D4：completed 是唯一业务终态且必为最末事件，其后不允许再发任何业务事件
        for kind in ("stage_finished", "progress", "marker_result", "summary"):
            guard = TerminalGuard()
            guard.note("completed")
            with self.assertRaises(ValueError):
                guard.note(kind)

    def test_certify_requires_exactly_one_completed(self):
        # 完全没 completed → 违规
        guard = TerminalGuard()
        guard.note("fatal")
        with self.assertRaises(ValueError):
            guard.certify()

    def test_note_after_fatal_allows_only_summary_and_completed(self):
        guard = TerminalGuard()
        guard.note("fatal")
        # summary 与 completed 合法，其它非法
        guard.note("summary")
        guard.note("completed")
        self.assertTrue(guard.certify())


class TestRedact(unittest.TestCase):
    REGISTRY = [
        "张三",
        "ACC-2026-0812-0042",
        "token=abcd1234secret",
        "patient_id=88",
        "storage_state.json",
    ]

    def test_redacts_matching_seeded_values(self):
        text = "患者 张三, accession ACC-2026-0812-0042, url ?token=abcd1234secret, cookie patient_id=88, path storage_state.json"
        out = redact(text, self.REGISTRY)
        self.assertNotIn("张三", out)
        self.assertNotIn("ACC-2026-0812-0042", out)
        self.assertNotIn("abcd1234secret", out)
        self.assertNotIn("patient_id=88", out)
        self.assertNotIn("storage_state.json", out)
        self.assertEqual(out.count("[REDACTED]"), 5)

    def test_non_registry_text_untouched(self):
        text = "这是普通日志，含未登记的名字 李四"
        out = redact(text, self.REGISTRY)
        self.assertIn("李四", out)
        self.assertIn("普通日志", out)
        self.assertNotIn("[REDACTED]", out)

    def test_redact_idempotent_after_full_replacement(self):
        text = "患者 张三"
        once = redact(text, self.REGISTRY)
        twice = redact(once, self.REGISTRY)
        self.assertEqual(once, twice)

    def test_empty_registry_no_change(self):
        self.assertEqual(redact("anything", []), "anything")


class TestSeriesEventNamespace(unittest.TestCase):
    """Phase 8: series_* events must not collide with the orchestrator reserved
    names so that forwarded child events keep a clean top-level series_* name."""

    def test_series_names_disjoint_from_orchestrator_reserved_names(self):
        self.assertTrue(
            SERIES_EVENT_NAMES.isdisjoint(ORCHESTRATOR_EVENT_NAMES),
            f"collision: {SERIES_EVENT_NAMES & ORCHESTRATOR_EVENT_NAMES}",
        )
        self.assertTrue(
            SERIES_EVENT_NAMES.isdisjoint(RESERVED_TERMINAL_NAMES),
            f"collision with terminal: {SERIES_EVENT_NAMES & RESERVED_TERMINAL_NAMES}",
        )

    def test_forwarded_series_child_event_keeps_clean_top_level_name(self):
        child = {"event": "series_discovered", "reached_end": True, "discovered": 3}
        ev = normalize_child_event(child, "capturing_live", "run_1")
        # Not renamed to capture_series_*: keeps prefix-free top-level name.
        self.assertEqual(ev["event"], "series_discovered")
        self.assertEqual(ev["payload"], child)

    def test_series_capture_terminal_not_a_d4_business_terminal(self):
        # series_expansion_completed is a phase-level terminal only: it must NOT be
        # part of RESERVED_TERMINAL_NAMES and must not trip TerminalGuard as a
        # business terminal.
        self.assertNotIn("series_expansion_completed", RESERVED_TERMINAL_NAMES)
        guard = TerminalGuard()
        guard.note("series_discovery_started")
        guard.note("series_expansion_completed")
        guard.note("completed")  # the real business terminal still follows
        self.assertTrue(guard.certify())


class TestSeriesTracker(unittest.TestCase):
    # A realistic clipped series event stream; fields are branch id / ordinal /
    # counts / error type only — never patient text or full metadata.
    STREAM = [
        {"event": "series_discovery_started"},
        {"event": "series_discovered", "reached_end": True, "discovered": 3},
        {"event": "series_capture_started", "branch_id": "br0", "ordinal": 0},
        {"event": "series_capture_completed", "branch_id": "br0", "ordinal": 0},
        {"event": "series_capture_started", "branch_id": "br1", "ordinal": 1},
        {"event": "series_capture_completed", "branch_id": "br1", "ordinal": 1},
        {"event": "series_capture_started", "branch_id": "br2", "ordinal": 2},
        {"event": "series_capture_partial", "branch_id": "br2", "ordinal": 2,
         "error_type": "metadata_timeout"},
        {"event": "series_expansion_completed"},
    ]

    def _run(self, stream):
        tracker = SeriesTracker()
        for event in stream:
            tracker.note(event)
        return tracker

    def test_counts_reflect_captured_partial(self):
        tracker = self._run(self.STREAM)
        self.assertEqual(
            tracker.counts(),
            {"discovered": 3, "captured": 2, "partial": 1, "failed": 0},
        )

    def test_coverage_complete_when_reached_end_and_no_failures(self):
        tracker = SeriesTracker()
        for event in (
            {"event": "series_discovered", "reached_end": True, "discovered": 2},
            {"event": "series_capture_completed", "branch_id": "a", "ordinal": 0},
            {"event": "series_capture_completed", "branch_id": "b", "ordinal": 1},
            {"event": "series_expansion_completed"},
        ):
            tracker.note(event)
        self.assertEqual(tracker.coverage()["status"], "complete")
        self.assertTrue(tracker.coverage()["reached_end"])

    def test_coverage_partial_on_any_branch_partial(self):
        tracker = self._run(self.STREAM)
        self.assertEqual(tracker.coverage()["status"], "partial")

    def test_coverage_partial_when_list_not_reached_end(self):
        tracker = SeriesTracker()
        for event in (
            {"event": "series_discovered", "reached_end": False, "discovered": 4,
             "warning": "series_virtualized_partial"},
            {"event": "series_capture_completed", "branch_id": "a", "ordinal": 0},
        ):
            tracker.note(event)
        cov = tracker.coverage()
        self.assertEqual(cov["status"], "partial")
        self.assertEqual(cov["warning"], "series_virtualized_partial")

    def test_coverage_failed_when_no_usable_viewer(self):
        tracker = SeriesTracker()
        tracker.note({"event": "series_discovered", "reached_end": False, "discovered": 0})
        tracker.note({"event": "series_expansion_completed"})
        self.assertEqual(tracker.coverage()["status"], "failed")

    def test_coverage_failed_on_infrastructure_error(self):
        tracker = SeriesTracker()
        for event in (
            {"event": "series_discovered", "reached_end": True, "discovered": 1},
            {"event": "series_capture_failed", "branch_id": "a", "ordinal": 0,
             "error_type": "infra_reload_exhausted"},
        ):
            tracker.note(event)
        self.assertEqual(tracker.coverage()["status"], "failed")

    def test_not_requested_when_no_series_events(self):
        self.assertEqual(SeriesTracker().coverage()["status"], "not_requested")

    def test_branch_summary_never_carries_patient_text(self):
        tracker = self._run(self.STREAM)
        cov = tracker.coverage()
        for branch in cov["branches"]:
            self.assertEqual(set(branch), {"branch_id", "ordinal", "status", "stage"})
            serialized = json.dumps(branch)
            self.assertNotIn("张三", serialized)
            self.assertNotIn("PatientName", serialized)
            self.assertNotIn("SeriesInstanceUID", serialized)

    def test_note_ignores_unknown_event_kinds(self):
        tracker = SeriesTracker()
        tracker.note({"event": "progress", "stage": "capturing_live"})
        self.assertEqual(tracker.coverage()["status"], "not_requested")
        self.assertEqual(tracker.counts()["discovered"], 0)

    def test_empty_coverage_is_not_enabled(self):
        cov = SeriesTracker().coverage()
        self.assertFalse(cov["enabled"])
        self.assertFalse(cov["reached_end"])


if __name__ == "__main__":
    unittest.main()
