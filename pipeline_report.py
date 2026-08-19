"""Deterministic pipeline report aggregation and serialization.

The JSON report is the single source of truth. The HTML report renders only the
escaped, redacted JSON fields plus local relative artifact links; it never embeds
patient payloads or base64 images.
"""

from __future__ import annotations

import html
import json
from dataclasses import asdict
from pathlib import Path

from pipeline_io import RunLayout, redact_payload
from pipeline_models import PipelineConfig, PipelineStage, PipelineStatus, StageResult
from rewrite_script import parse_action_plan

# Maps a validation stage value to the ``drivers`` report key.
DRIVER_STAGE_KEYS = {
    "validating_replica": "replica_validation",
    "validating_adapter": "adapter_validation",
}


def aggregate_status(results: list[StageResult]) -> PipelineStatus:
    """Collapse stage outcomes into one deterministic run status.

    Precedence: failed > cancelled > partial > success.
    """
    if any(result.status == PipelineStatus.FAILED for result in results):
        return PipelineStatus.FAILED
    if any(result.status == PipelineStatus.CANCELLED for result in results):
        return PipelineStatus.CANCELLED
    if any(result.status == PipelineStatus.PARTIAL for result in results):
        return PipelineStatus.PARTIAL
    return PipelineStatus.SUCCESS


def _extract_drivers_and_capabilities(results: list[StageResult]) -> tuple[dict, dict]:
    """Pull the local offline drivers and the adapter capability matrix from
    stage metrics so the report carries them without re-evaluating anything."""
    drivers: dict[str, str] = {}
    capabilities: dict[str, object] = {}
    for result in results:
        metrics = result.metrics or {}
        stage = result.stage.value
        if stage in DRIVER_STAGE_KEYS and metrics.get("driver"):
            drivers[DRIVER_STAGE_KEYS[stage]] = metrics["driver"]
        if metrics.get("capabilities"):
            capabilities = metrics["capabilities"]
    return drivers, capabilities


def extract_series_coverage(results: list[StageResult]) -> dict | None:
    """Return the series-coverage summary attached to the LIVE_CAPTURE stage, or
    ``None`` when no run carried one (e.g. expansion disabled / untouched)."""
    for result in results:
        if result.stage == PipelineStage.LIVE_CAPTURE:
            coverage = (result.metrics or {}).get("series_coverage")
            if isinstance(coverage, dict):
                return coverage
    return None


def _script_contains_series_selection(config: PipelineConfig) -> bool:
    """Return whether the recorded source has a series-selection marker."""
    try:
        source = config.source_script.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False
    try:
        plan = parse_action_plan(source)
    except (SyntaxError, ValueError):
        # Keep the report useful even when preflight already recorded a syntax
        # error; the marker text is enough to explain missing series coverage.
        return "# [MARKER: 序列选择" in source
    return any(
        " ".join(str(group.marker_label).split()).casefold() == "序列选择"
        for group in plan.marker_groups
    )


def _empty_coverage(enabled: bool) -> dict:
    """Default coverage payload when a run performed no series exploration."""
    return {
        "enabled": bool(enabled),
        "status": "not_requested",
        "discovered": 0,
        "captured": 0,
        "partial": 0,
        "failed": 0,
        "count_conserved": False,
        "reached_end": False,
        "expansion_completed": False,
        "warning": None,
        "branches": [],
    }


def _coverage_from_capture_artifact(layout: RunLayout) -> dict | None:
    """Recover authoritative series coverage for offline resume operations.

    ``replica-build`` intentionally has no LIVE_CAPTURE stage, but it must not
    erase the coverage already persisted by that run. The exploration manifest
    is the durable source of truth and contains only safe branch ids in the
    summary emitted here; UID hashes and metadata bodies are never copied.
    """
    path = layout.capture_dir / "series_branches" / "series_capture_manifest.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(payload, dict) or "discovered_count" not in payload:
        return None

    discovered = int(payload.get("discovered_count") or 0)
    captured = int(payload.get("captured_count") or 0)
    partial = int(payload.get("partial_count") or 0)
    failed = int(payload.get("failed_count") or 0)
    skipped = int(payload.get("skipped_count") or 0)
    # Skipped branches are replayed-terminals (budget/duplicate/hub tails) that
    # the live event stream folds into ``partial``, so the resume-side payload
    # must fold them the same way: a skipped branch is a partial capture, never
    # a separate public bucket. The raw skip reason is preserved per-branch in
    # ``stage`` so operators still see *why* it was skipped.
    partial += skipped
    count_conserved = bool(payload.get("count_conserved")) and (
        captured + partial + failed == discovered
    )
    reached_end = bool(payload.get("reached_end"))
    if bool(payload.get("overall_ok")) and count_conserved and reached_end and captured > 0:
        status = "complete"
    elif discovered == 0 or captured + partial == 0:
        status = "failed"
    else:
        status = "partial"

    branches = []
    for branch in payload.get("branches") or []:
        if not isinstance(branch, dict):
            continue
        raw_status = str(branch.get("capture_status") or "failed")
        branches.append({
            "branch_id": str(branch.get("branch_id") or ""),
            "ordinal": int(branch.get("ordinal") or 0),
            # Normalize skipped_* terminals to the public ``partial`` status so
            # the summary and branch rows agree (live tracker reports partial).
            "status": "partial" if raw_status.startswith("skipped_") else raw_status,
            # Preserve the original skip/failure cause for auditability.
            "stage": str(
                branch.get("error_type")
                or branch.get("fail_stage")
                or (raw_status if raw_status.startswith("skipped_") else "")
            ),
        })
    return {
        "enabled": True,
        "status": status,
        "discovered": discovered,
        "captured": captured,
        "partial": partial,
        "failed": failed,
        "count_conserved": count_conserved,
        "reached_end": reached_end,
        "expansion_completed": True,
        "warning": payload.get("warning"),
        "branches": branches,
    }


def write_pipeline_report(
    layout: RunLayout,
    config: PipelineConfig,
    results: list[StageResult],
) -> tuple[Path, Path]:
    """Write ``pipeline_report.json`` (source of truth) and its escaped HTML render.

    The JSON payload is redacted before serialization. The HTML file is nothing
    more than the escaped JSON text nested inside a minimal document.
    """
    status = aggregate_status(results)
    drivers, capabilities = _extract_drivers_and_capabilities(results)
    series_coverage = (
        extract_series_coverage(results)
        or _coverage_from_capture_artifact(layout)
        or _empty_coverage(config.expand_all_series)
    )
    report_warnings: list[str] = []
    if not config.expand_all_series and _script_contains_series_selection(config):
        report_warnings.append("series_expansion_not_requested")
        # Keep an existing capture warning intact, but never let the default
        # ``None`` hide the explicit scope warning in the report.
        if not series_coverage.get("warning"):
            series_coverage = {
                **series_coverage,
                "warning": "series_expansion_not_requested",
            }
    payload = redact_payload(
        {
            "schema_version": 1,
            "hospital": config.hospital,
            "source_script": config.source_script.name,
            "status": status.value,
            "drivers": drivers,
            "capabilities": capabilities,
            "warnings": report_warnings,
            "series_coverage": series_coverage,
            "stages": [
                {
                    **asdict(result),
                    "stage": result.stage.value,
                    "status": result.status.value,
                }
                for result in results
            ],
        }
    )
    json_text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    json_tmp = layout.report_json.with_suffix(".json.tmp")
    json_tmp.write_text(json_text, encoding="utf-8", newline="\n")
    json_tmp.replace(layout.report_json)
    html_text = (
        '<!doctype html><meta charset="utf-8">'
        "<title>Pipeline report</title><h1>Pipeline report</h1><pre>"
        + html.escape(json_text)
        + "</pre>"
    )
    html_tmp = layout.report_html.with_suffix(".html.tmp")
    html_tmp.write_text(html_text, encoding="utf-8", newline="\n")
    html_tmp.replace(layout.report_html)
    return layout.report_json, layout.report_html
