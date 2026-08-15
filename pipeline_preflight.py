from __future__ import annotations

import ast
import re
from dataclasses import dataclass

from agent import MARKER_MAP, parse_markers
from batch_capture_replicate import validate_annotations, classify_recording_template
from pipeline_models import PipelineConfig
from runtime_python import codegen_python_executable
from rewrite_script import parse_action_plan


@dataclass(frozen=True)
class PreflightResult:
    ok: bool
    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    marker_names: tuple[str, ...]


_EXPANSION_KNOWN_MODES = {"first_stable_frame"}


def _validate_expansion_plan(plan, config: PipelineConfig, errors: list[str], warnings: list[str]) -> None:
    """Validate the recording template and budgets required for all-series expansion.

    Invoked only when ``config.expand_all_series`` is true. A missing template
    piece is a hard preflight error (never silently guessed); budget violations
    are also hard errors so the explorer cannot run unwisely.
    """
    template = classify_recording_template(plan)
    if template.series_action is None:
        errors.append("expansion_missing_series_select")
    if template.metadata_open is None:
        errors.append("expansion_missing_metadata_open")
    if template.metadata_close is None:
        errors.append("expansion_missing_metadata_close")
    max_series = config.max_series
    per_series = config.per_series_timeout_s
    total = config.total_series_timeout_s
    if not isinstance(max_series, int) or not 1 <= max_series <= 100:
        errors.append("expansion_max_series_invalid")
    if not isinstance(per_series, int) or per_series <= 0:
        errors.append("expansion_per_series_timeout_invalid")
    if not isinstance(total, int) or total <= 0 or total > 3600:
        errors.append("expansion_total_series_timeout_invalid")
    elif isinstance(max_series, int) and isinstance(per_series, int) and max_series * per_series > total:
        errors.append("expansion_budget_product_exceeds_total")
    if config.viewer_capture_mode not in _EXPANSION_KNOWN_MODES:
        warnings.append(f"expansion_viewer_capture_mode_unsupported:{config.viewer_capture_mode}")


def run_preflight(config: PipelineConfig) -> PreflightResult:
    errors: list[str] = []
    warnings: list[str] = []
    marker_names: list[str] = []
    if not config.source_script.is_file():
        errors.append("source_script_missing")
        return PreflightResult(False, tuple(errors), (), ())
    source = config.source_script.read_text(encoding="utf-8")
    plan = None
    try:
        ast.parse(source)
        plan = parse_action_plan(source)
    except SyntaxError:
        errors.append("source_syntax_error")
    markers = parse_markers(source)
    marker_names = [marker["name"] for marker in markers]
    if not markers:
        errors.append("no_supported_markers")
    unsupported = sorted({
        name for name in marker_names
        if name not in MARKER_MAP
    })
    warnings.extend(f"unsupported_marker:{name}" for name in unsupported)
    try:
        annotation_payload = validate_annotations(
            config.source_script, config.annotations_path
        )
    except FileNotFoundError:
        errors.append("annotations_missing")
    except ValueError:
        errors.append("annotations_hash_mismatch")
    else:
        try:
            plan = parse_action_plan(source, annotation_payload["markers"])
        except ValueError:
            errors.append("marker_identity_mismatch")
    if config.auth_mode not in {"scripted", "interactive", "storage-state"}:
        errors.append("auth_mode_invalid")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", config.hospital):
        errors.append("hospital_name_invalid")
    try:
        codegen_python_executable()
    except RuntimeError:
        errors.append("interpreter_missing")
    if config.auth_mode == "storage-state" and (
        config.storage_state is None or not config.storage_state.is_file()
    ):
        errors.append("storage_state_missing")
    if config.expand_all_series and plan is not None:
        _validate_expansion_plan(plan, config, errors, warnings)
    config.output_root.mkdir(parents=True, exist_ok=True)
    return PreflightResult(
        not errors, tuple(errors), tuple(warnings), tuple(marker_names)
    )
