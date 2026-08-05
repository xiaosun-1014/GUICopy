from __future__ import annotations

import ast
import re
from dataclasses import dataclass

from agent import MARKER_MAP, parse_markers
from batch_capture_replicate import validate_annotations
from pipeline_models import PipelineConfig
from runtime_python import codegen_python_executable
from rewrite_script import parse_action_plan


@dataclass(frozen=True)
class PreflightResult:
    ok: bool
    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    marker_names: tuple[str, ...]


def run_preflight(config: PipelineConfig) -> PreflightResult:
    errors: list[str] = []
    warnings: list[str] = []
    marker_names: list[str] = []
    if not config.source_script.is_file():
        errors.append("source_script_missing")
        return PreflightResult(False, tuple(errors), (), ())
    source = config.source_script.read_text(encoding="utf-8")
    try:
        ast.parse(source)
        parse_action_plan(source)
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
            parse_action_plan(source, annotation_payload["markers"])
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
    config.output_root.mkdir(parents=True, exist_ok=True)
    return PreflightResult(
        not errors, tuple(errors), tuple(warnings), tuple(marker_names)
    )
