# -*- coding: utf-8 -*-
"""DICOM Meta 质量校验 — 固化共享模块。

封装 marker-meta-info 的校验管线，供 auto_gen.py 生成的脚本直接调用。

使用方式:
    from skills._shared.meta_validate import validate_and_save
    summary = validate_and_save(rows, output_dir=SCRIPT_DIR, project_root=_PROJECT)
"""
import json
import sys
from pathlib import Path


def validate_and_save(rows: list[dict], output_dir: Path,
                      project_root: Path | None = None) -> dict:
    """校验 DICOM meta 行并保存全部产物。

    Args:
        rows: extract_meta_from_frame 返回的原始行列表
        output_dir: 产物输出目录（通常是 SCRIPT_DIR）
        project_root: 项目根目录（用于定位 marker-meta-info skill）

    Returns:
        summary dict，含 accepted_count / rejected_count / warning_count / quality_grade
    """
    if not rows:
        print("[Meta] 未提取到任何 tag，跳过校验")
        return {}

    # ── 定位 marker-meta-info 脚本 ──
    if project_root is None:
        project_root = output_dir.parent.parent
    meta_scripts = project_root / "skills" / "marker-meta-info" / "scripts"
    sys.path.insert(0, str(meta_scripts))

    from validators import validate_metadata
    from parse_inputs import detect_and_parse

    # ── 校验 ──
    parsed, fmt = detect_and_parse(rows)
    print(f"[Meta] 校验格式: {fmt}, {len(parsed)} 行")
    summary = validate_metadata(parsed)
    print(f"[Meta] 校验结果: 通过{summary.accepted_count}/"
          f"拒绝{summary.rejected_count}/"
          f"警告{summary.warning_count}/"
          f"等级={summary.quality_grade()}")

    # ── 保存 ──
    # 原始提取
    meta_dir = output_dir / "meta_validation"
    meta_dir.mkdir(parents=True, exist_ok=True)
    with open(meta_dir / "dicom_meta.json", "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    print("[Meta] 已保存: meta_validation/dicom_meta.json")

    # 校验产物
    _dump([r.as_dict() for r in summary.rejected_rows],
          meta_dir / "rejected_rows.json")
    _dump(summary.issues, meta_dir / "metadata_warnings.json")
    _dump(summary.spatial_issues, meta_dir / "spatial_issues.json")
    _dump({
        "accepted_count": summary.accepted_count,
        "rejected_count": summary.rejected_count,
        "warning_count": summary.warning_count,
        "quality_grade": summary.quality_grade(),
    }, meta_dir / "validation_summary.json")

    # 主输出：完整校验结果
    _dump(summary.as_dict(), output_dir / "dicom_info.json")
    print("[Meta] 已保存: dicom_info.json (含警告详情)")

    return summary.as_dict()


def _dump(data, path: Path) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
