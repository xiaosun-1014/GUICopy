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
from pipeline_models import PipelineConfig, PipelineStatus, StageResult

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
    payload = redact_payload(
        {
            "schema_version": 1,
            "hospital": config.hospital,
            "source_script": config.source_script.name,
            "status": status.value,
            "drivers": drivers,
            "capabilities": capabilities,
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
