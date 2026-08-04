"""
DICOM 元数据质量校验 CLI 主入口。

用法：
  python validate_metadata.py <input> [--output-dir <dir>] [--strict] [--quiet]

参数：
  input           输入文件 (.json) 或字符串（按自动识别解析）
  --output-dir    写入 validated_metadata_table.json / rejected_rows.json / metadata_warnings.json
  --strict        严格模式：warning 也算失败
  --quiet         仅输出 summary（不打印 accepted/rejected 明细）

支持的输入格式（自动识别）：
  - JSON 列表 (canonical / dom_table / vl 三种按字段名自动识别)
  - JSON 字典 (key_values 格式)
  - 多行纯文本 (text_dump 格式)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 同目录导入
sys.path.insert(0, str(Path(__file__).parent))

from parse_inputs import load_payload, detect_and_parse  # noqa: E402
from validators import validate_metadata, ValidationSummary  # noqa: E402


def write_outputs(output_dir: Path, summary: ValidationSummary) -> dict[str, Path]:
    """写入三件套产物：
    - validated_metadata_table.json: accepted 行（含 _fixed 标记）
    - rejected_rows.json: rejected 行
    - metadata_warnings.json: 所有 issues 详情
    - spatial_issues.json: 空间字段不合规项
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {}

    validated_path = output_dir / "validated_metadata_table.json"
    validated_path.write_text(
        json.dumps([r.as_dict() for r in summary.accepted_rows], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    paths["validated"] = validated_path

    rejected_path = output_dir / "rejected_rows.json"
    rejected_path.write_text(
        json.dumps([r.as_dict() for r in summary.rejected_rows], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    paths["rejected"] = rejected_path

    warnings_path = output_dir / "metadata_warnings.json"
    warnings_path.write_text(
        json.dumps(summary.issues, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    paths["warnings"] = warnings_path

    spatial_path = output_dir / "spatial_issues.json"
    spatial_path.write_text(
        json.dumps(summary.spatial_issues, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    paths["spatial_issues"] = spatial_path

    summary_path = output_dir / "validation_summary.json"
    summary_path.write_text(
        json.dumps({
            "accepted_count": summary.accepted_count,
            "rejected_count": summary.rejected_count,
            "warning_count": summary.warning_count,
            "quality_grade": summary.quality_grade(),
            "missing_required": summary.missing_required_fields(),
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    paths["summary"] = summary_path

    return paths


def run(input_path: str | None, output_dir: str | None, strict: bool, quiet: bool) -> int:
    if input_path is None or input_path == "-":
        raw = sys.stdin.read()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = raw
    else:
        payload = load_payload(input_path)

    rows, fmt = detect_and_parse(payload)
    summary = validate_metadata(rows)
    summary_dict = summary.as_dict()

    grade = summary.quality_grade()
    failed = (grade == "fail") or (strict and grade != "pass")

    if not quiet:
        print(f"# 输入格式: {fmt}")
        print(f"# 解析行数: {len(rows)}")
        print(f"# accepted={summary.accepted_count} rejected={summary.rejected_count} "
              f"warnings={summary.warning_count} grade={grade}")
        if summary.missing_required_fields():
            print(f"# 缺失必填字段: {summary.missing_required_fields()}")
        if summary.spatial_issues:
            print(f"# 空间字段问题: {len(summary.spatial_issues)} 项")
            for s in summary.spatial_issues:
                print(f"   - [{s['status']}] {s['keyword']}: {s['message']}")
        print()
        print(json.dumps(summary_dict, ensure_ascii=False, indent=2))

    if output_dir:
        paths = write_outputs(Path(output_dir), summary)
        if not quiet:
            print()
            print(f"# 已写入产物到: {output_dir}")
            for key, p in paths.items():
                print(f"   - {key}: {p}")

    return 1 if failed else 0


def main():
    parser = argparse.ArgumentParser(
        description="DICOM 元数据质量校验",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("input", nargs="?", default=None,
                        help="输入文件路径，'-' 表示从 stdin 读取")
    parser.add_argument("--output-dir", "-o", default=None,
                        help="写入产物的目录（validated/rejected/warnings/spatial_issues/summary）")
    parser.add_argument("--strict", action="store_true",
                        help="严格模式：warning 等级也算失败")
    parser.add_argument("--quiet", "-q", action="store_true",
                        help="仅输出最终汇总")
    args = parser.parse_args()
    sys.exit(run(args.input, args.output_dir, args.strict, args.quiet))


if __name__ == "__main__":
    main()
