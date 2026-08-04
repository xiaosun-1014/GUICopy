"""
DICOM 元数据校验规则集。

每个校验函数：
  输入：单个 MetadataRow（来自 parse_inputs.MetadataRow）
  输出：list[Issue]  （空 list = 通过）

完整校验流程：validate_single_row → validate_rows → validate_spatial_fields
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from typing import Any

try:
    from parse_inputs import (
        MetadataRow,
        REQUIRED_FIELDS,
        SPATIAL_FIELD_KEYWORDS,
        STANDARD_KEYWORDS,
        normalize_dicom_tag,
        resolve_standard_field,
    )
except ImportError:
    # 当作为独立脚本运行时
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent))
    from parse_inputs import (
        MetadataRow,
        REQUIRED_FIELDS,
        SPATIAL_FIELD_KEYWORDS,
        STANDARD_KEYWORDS,
        normalize_dicom_tag,
        resolve_standard_field,
    )


# ── 结果类型 ──


@dataclass
class Issue:
    code: str
    severity: str  # "error" | "warning"
    message: str
    tag: str = ""
    field: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ValidationResult:
    row: MetadataRow
    status: str  # "accepted" | "rejected" | "fixed"
    issues: list[Issue] = field(default_factory=list)
    fixed_row: MetadataRow | None = None

    def as_dict(self) -> dict[str, Any]:
        d = self.row.as_dict()
        d["_status"] = self.status
        if self.issues:
            d["_issues"] = [i.as_dict() for i in self.issues]
        if self.fixed_row is not None:
            d["_fixed"] = self.fixed_row.as_dict()
        return d


@dataclass
class ValidationSummary:
    accepted_rows: list[MetadataRow] = field(default_factory=list)
    rejected_rows: list[MetadataRow] = field(default_factory=list)
    issues: list[dict[str, Any]] = field(default_factory=list)
    spatial_issues: list[dict[str, Any]] = field(default_factory=list)

    @property
    def accepted_count(self) -> int:
        return len(self.accepted_rows)

    @property
    def rejected_count(self) -> int:
        return len(self.rejected_rows)

    @property
    def warning_count(self) -> int:
        return len(self.issues)

    def quality_grade(self) -> str:
        """基于 accepted/rejected/必填字段缺失情况得出质量等级。"""
        required_missing = self.missing_required_fields()
        if required_missing:
            return "fail"
        if self.rejected_count > 0 or self.warning_count > 5:
            return "warn"
        return "pass"

    def missing_required_fields(self) -> list[str]:
        accepted_keywords = {(r.keyword or "").lower() for r in self.accepted_rows}
        missing = []
        for keyword in REQUIRED_FIELDS:
            if keyword.lower() not in accepted_keywords:
                missing.append(keyword)
        return missing

    def as_dict(self) -> dict[str, Any]:
        return {
            "accepted_count": self.accepted_count,
            "rejected_count": self.rejected_count,
            "warning_count": self.warning_count,
            "quality_grade": self.quality_grade(),
            "missing_required": self.missing_required_fields(),
            "accepted": [r.as_dict() for r in self.accepted_rows],
            "rejected": [r.as_dict() for r in self.rejected_rows],
            "issues": self.issues,
            "spatial_issues": self.spatial_issues,
        }


# ── VR 字典（按 DICOM PS3.5 表 6.2-1） ──


VR_VALIDATORS: dict[str, dict[str, Any]] = {
    "AE": {"max_len": 16},
    "AS": {"max_len": 4, "pattern": r"^\d{3}[DWMY]$"},
    "CS": {"max_len": 16, "pattern": r"^[A-Z0-9 _\\]*$"},
    "DA": {"max_len": 10, "pattern": r"^\d{8}$"},
    "DS": {"max_len": 16},
    "DT": {"max_len": 54},
    "IS": {"max_len": 12, "pattern": r"^-?\d+(\.\d+)?$"},
    "LO": {"max_len": 64},
    "SH": {"max_len": 16},
    "ST": {"max_len": 1024},
    "TM": {"max_len": 16},
    "UI": {"max_len": 64, "pattern": r"^[\d.]+$"},
    "LT": {"max_len": 10240},
    "PN": {"max_len": 64},
}

LO_WARN_LENGTH = 64
LO_REJECT_LENGTH = 2000
MIN_UID_LENGTH = 6

GROUP_0002_TAG_PATTERN = re.compile(r"^\(0002,\s*[0-9A-Fa-f]{4}\)$")

# 内置 tag→VR 映射（pydicom 不可用时的降级表）
BUILTIN_TAG_VR: dict[str, str] = {
    "(0008, 0005)": "SH", "(0008, 0008)": "CS", "(0008, 0016)": "UI",
    "(0008, 0018)": "UI", "(0008, 0020)": "DA", "(0008, 0030)": "TM",
    "(0008, 0050)": "SH", "(0008, 0060)": "CS", "(0008, 0070)": "LO",
    "(0008, 0080)": "LO", "(0008, 0090)": "PN", "(0008, 1010)": "SH",
    "(0008, 1030)": "LO", "(0008, 103E)": "LO", "(0008, 1040)": "LO",
    "(0008, 1050)": "LO", "(0010, 0010)": "PN", "(0010, 0020)": "LO",
    "(0010, 0021)": "LO", "(0010, 0030)": "DA", "(0010, 0032)": "TM",
    "(0010, 0040)": "CS", "(0010, 1010)": "AS", "(0010, 1030)": "DS",
    "(0018, 0015)": "CS", "(0018, 0050)": "DS", "(0018, 0060)": "DS",
    "(0018, 0088)": "DS", "(0018, 1030)": "LO", "(0018, 1050)": "DS",
    "(0018, 1140)": "CS", "(0018, 1150)": "IS", "(0018, 1160)": "SH",
    "(0018, 5100)": "CS", "(0020, 000D)": "UI", "(0020, 000E)": "UI",
    "(0020, 0010)": "SH", "(0020, 0011)": "IS", "(0020, 0012)": "IS",
    "(0020, 0013)": "IS", "(0020, 0032)": "DS", "(0020, 0037)": "DS",
    "(0020, 0052)": "UI", "(0020, 1002)": "IS", "(0020, 1040)": "LO",
    "(0020, 1041)": "DS", "(0028, 0002)": "US", "(0028, 0004)": "CS",
    "(0028, 0010)": "US", "(0028, 0011)": "US", "(0028, 0030)": "DS",
    "(0028, 0100)": "US", "(0028, 0101)": "US", "(0028, 0102)": "US",
    "(0028, 0103)": "US", "(0028, 0106)": "US", "(0028, 0120)": "US",
    "(0028, 1050)": "DS", "(0028, 1051)": "DS", "(0028, 1052)": "DS",
    "(0028, 1053)": "DS", "(0028, 1054)": "DS",
}


def lookup_vr(tag_str: str) -> str | None:
    """查询 tag 对应的 VR，优先 pydicom.datadict，降级内置表。"""
    if not tag_str:
        return None
    try:
        import pydicom.datadict as pdd  # type: ignore

        match = re.match(r"\(([0-9A-Fa-f]{4}),\s*([0-9A-Fa-f]{4})\)", tag_str)
        if match:
            dicom_tag = (int(match.group(1), 16), int(match.group(2), 16))
            vr = pdd.get_vr(dicom_tag)
            if vr:
                return vr
    except (ImportError, Exception):
        pass
    return BUILTIN_TAG_VR.get(tag_str)


# ── 各级校验函数 ──


def check_group_0002(row: MetadataRow) -> list[Issue]:
    if row.tag and GROUP_0002_TAG_PATTERN.match(row.tag):
        return [Issue("group_0002", "error",
                      f"tag {row.tag} 属于 0002 组 (File Meta Information)，应剔除",
                      tag=row.tag, field=row.keyword)]
    return []


def check_sequence_vr(row: MetadataRow) -> list[Issue]:
    vr = lookup_vr(row.tag)
    if vr == "SQ":
        return [Issue("sequence_vr", "error",
                      f"tag {row.tag} 的 VR 为 SQ (Sequence)，禁止作为元数据行",
                      tag=row.tag, field=row.keyword)]
    return []


def check_non_dicom_text(row: MetadataRow) -> list[Issue]:
    """启发式判断 value 是否是非 DICOM 自由文本（浏览器 UI 残留）。"""
    ui_patterns = [
        r"^\s*(?:Login|登录|用户名|密码|Password)\s*$",
        r"^\s*(?:Copyright|©|All\s+[Rr]ights\s+[Rr]eserved)",
        r"^\s*(?:Page\s+\d+\s+of\s+\d+)",
        r"^\s*(?:Close|关闭|OK|确定|Cancel|取消)\s*$",
        r"^\s*(?:Loading|加载中|请稍候)\s*$",
    ]
    for pattern in ui_patterns:
        if re.match(pattern, row.value):
            return [Issue("non_dicom_text", "error",
                          f"value 看似非 DICOM 文本 (UI 残留): {row.value[:40]!r}",
                          tag=row.tag, field=row.keyword)]
    return []


def check_tag_description_consistency(row: MetadataRow) -> list[Issue]:
    """检查 row.tag 与 description/keyword 是否一致（按 STANDARD_KEYWORDS 反向映射）。"""
    if not row.tag:
        return []
    tag_to_kw: dict[str, str] = {}
    for keyword, (tag, _) in STANDARD_KEYWORDS.items():
        tag_to_kw[tag] = keyword
    std_keyword = tag_to_kw.get(row.tag)
    if std_keyword is None:
        return []

    row_desc = (row.keyword or row.description or "").strip().lower()
    if row_desc == std_keyword.lower():
        return []
    return [Issue("tag_desc_mismatch", "warning",
                  f"tag {row.tag} 的标准 keyword 应为 {std_keyword}，"
                  f"实际为 {row.keyword or row.description}",
                  tag=row.tag, field=row.keyword)]


def check_vr_format(row: MetadataRow) -> list[Issue]:
    """按 VR 规则校验 value 格式。"""
    if not row.value or not row.tag:
        return []
    vr = lookup_vr(row.tag)
    if not vr or vr not in VR_VALIDATORS:
        return []
    info = VR_VALIDATORS[vr]
    issues: list[Issue] = []

    # 长度校验（DS 多值字段：每个组件≤16，总长按组件数扩展）
    max_len = info["max_len"]
    parts = re.split(r"[\\,/]", row.value)
    effective_max = max_len * len(parts) if vr == "DS" else max_len
    if len(row.value) > effective_max:
        if vr == "LO" and len(row.value) > LO_REJECT_LENGTH:
            issues.append(Issue("vr_value_overlong", "error",
                                f"LO 字段超长 (len={len(row.value)}>{LO_REJECT_LENGTH})，已拒绝",
                                tag=row.tag, field=row.keyword))
        elif vr == "LO" and len(row.value) > LO_WARN_LENGTH:
            issues.append(Issue("vr_value_overlong", "warning",
                                f"LO 字段超长 (len={len(row.value)}>{max_len})，可截断",
                                tag=row.tag, field=row.keyword))
        elif vr == "DS":
            # DS 多值仍超长：检查每个组件
            bad_parts = [(i, p) for i, p in enumerate(parts) if len(p) > max_len]
            if bad_parts:
                issues.append(Issue("vr_value_overlong", "error",
                                    f"DS 字段第 {[i+1 for i,_ in bad_parts]} 个组件超长 "
                                    f"(>16 字符): {row.value[:60]!r}",
                                    tag=row.tag, field=row.keyword))
        else:
            issues.append(Issue("vr_value_overlong", "error",
                                f"{vr} 字段超长 (len={len(row.value)}>{max_len})",
                                tag=row.tag, field=row.keyword))

    # 格式校验（pattern）
    pattern = info.get("pattern")
    if pattern and row.value and not re.match(pattern, row.value):
        issues.append(Issue("vr_format_mismatch", "error",
                            f"{vr} 格式不匹配 (value={row.value[:60]!r})",
                            tag=row.tag, field=row.keyword))
    return issues


def check_uid(row: MetadataRow) -> list[Issue]:
    """UID 专项校验：长度≥6、纯数字+点。"""
    if not row.value:
        return []
    vr = lookup_vr(row.tag)
    is_uid = vr == "UI" or (row.keyword and "UID" in (row.keyword or ""))
    if not is_uid:
        return []
    issues: list[Issue] = []
    stripped = row.value.strip()
    if len(stripped) < MIN_UID_LENGTH:
        issues.append(Issue("uid_too_short", "error",
                            f"UID 过短 (len={len(stripped)}<{MIN_UID_LENGTH})",
                            tag=row.tag, field=row.keyword))
    if not re.match(r"^[\d.]+$", stripped):
        issues.append(Issue("uid_invalid_chars", "error",
                            f"UID 包含非法字符 (value={stripped[:60]!r})",
                            tag=row.tag, field=row.keyword))
    return issues


def check_value_range(row: MetadataRow) -> list[Issue]:
    """值范围软警告：PatientAge 格式异常、SliceThickness 异常等。"""
    issues: list[Issue] = []
    kw = (row.keyword or "").lower()
    if kw == "patientage" and row.value:
        # 期望 3 位数字 + 单位 (045Y)
        if not re.match(r"^\d{3}[DWMY]$", row.value):
            issues.append(Issue("value_range_warning", "warning",
                                f"PatientAge 格式异常 (期望 3 位数字 + D/W/M/Y): {row.value!r}",
                                tag=row.tag, field=row.keyword))
    if kw == "slicethickness" and row.value:
        try:
            thickness = float(row.value)
            if thickness > 10:
                issues.append(Issue("value_range_warning", "warning",
                                    f"SliceThickness 异常大 (>10): {thickness}",
                                    tag=row.tag, field=row.keyword))
            elif thickness <= 0:
                issues.append(Issue("value_range_warning", "warning",
                                    f"SliceThickness 非正数: {thickness}",
                                    tag=row.tag, field=row.keyword))
        except (ValueError, TypeError):
            issues.append(Issue("value_range_warning", "warning",
                                f"SliceThickness 非数值: {row.value!r}",
                                tag=row.tag, field=row.keyword))
    return issues


def check_partial_confidence(row: MetadataRow) -> list[Issue]:
    """VL 输出专项：partial=true 或 confidence<0.6 时给出降级警告。"""
    issues: list[Issue] = []
    if row.partial:
        issues.append(Issue("partial_row", "warning",
                            f"行被 VL 标记为 partial (side={row.partial_side})，需人工复核",
                            tag=row.tag, field=row.keyword))
    if row.confidence is not None and row.confidence < 0.6:
        issues.append(Issue("low_confidence", "warning",
                            f"VL 置信度低 (confidence={row.confidence:.2f})",
                            tag=row.tag, field=row.keyword))
    return issues


# ── 自动修复（A 类：值格式标准化） ──


def _fix_rotation_direction(value: str) -> str | None:
    cleaned = value.strip().upper()
    if cleaned in ("CW", "CC"):
        return cleaned
    compact = re.sub(r"[^A-Za-z]", "", value).upper()
    if compact in ("CW", "CC"):
        return compact
    return None


def _fix_patient_position(value: str) -> str | None:
    standard = {"HFS", "HFP", "FFS", "FFP", "HFDR", "HFDL"}
    cleaned = value.strip().upper()
    if cleaned in standard:
        return cleaned
    compact = re.sub(r"[^A-Z]", "", value).upper()
    if compact in standard:
        return compact
    return None


def _fix_cs_upper(value: str) -> str | None:
    cleaned = value.strip().upper()
    if cleaned and cleaned != value:
        return cleaned
    return None


def _fix_ds_multi(value: str) -> str | None:
    parts = [p.strip() for p in value.split(",") if p.strip()]
    if len(parts) < 2:
        return None
    raw_parts = value.split(",")
    if any(p != p2 for p, p2 in zip(raw_parts, parts)):
        return ",".join(parts)
    return None


FIXERS = {
    "(0018, 1140)": _fix_rotation_direction,  # RotationDirection
    "(0018, 5100)": _fix_patient_position,     # PatientPosition
    "(0018, 0015)": _fix_cs_upper,             # BodyPartExamined
    "(0018, 0050)": _fix_cs_upper,             # SliceThickness (在某些版本里是 DS 但会被标 CS)
    "(0018, 1160)": _fix_cs_upper,             # ReconstructionAlgorithm
    "(0018, 1210)": _fix_cs_upper,             # ConvolutionKernel
    "(0008, 0060)": _fix_cs_upper,             # Modality
    "(0018, 1190)": _fix_ds_multi,             # ReconstructionDiameter 等多值 DS
}


def try_auto_fix(row: MetadataRow) -> tuple[MetadataRow | None, str | None]:
    """对已知格式敏感字段做值标准化。返回 (fixed_row, message) 或 (None, None)。"""
    fixer = FIXERS.get(row.tag)
    if fixer is None or not row.value:
        return None, None
    fixed = fixer(row.value)
    if fixed is None or fixed == row.value:
        return None, None
    fixed_row = MetadataRow(
        tag=row.tag, keyword=row.keyword, description=row.description,
        value=fixed, source=row.source, raw_label=row.raw_label,
        confidence=row.confidence, tile_id=row.tile_id,
        partial=row.partial, partial_side=row.partial_side,
        needs_merge_prev=row.needs_merge_prev, needs_merge_next=row.needs_merge_next,
    )
    return fixed_row, f"tag {row.tag} 的值已自动修正: {row.value!r} → {fixed!r}"


# ── 单行完整校验 ──


def validate_single_row(row: MetadataRow) -> ValidationResult:
    """对单条 MetadataRow 执行完整校验。"""
    issues: list[Issue] = []

    # 1. 硬剔除（0002 组、SQ、非 DICOM 文本）
    for check in (check_group_0002, check_sequence_vr, check_non_dicom_text):
        issues.extend(check(row))
        if any(i.severity == "error" and i.code in ("group_0002", "sequence_vr", "non_dicom_text") for i in issues):
            return ValidationResult(row=row, status="rejected", issues=issues)

    # 2. tag↔description 一致性（warning，可自动修正）
    issues.extend(check_tag_description_consistency(row))

    # 3. A 类自动修复
    fixed_row, fix_msg = try_auto_fix(row)
    working_row = fixed_row if fixed_row is not None else row
    if fix_msg:
        # 自动修复会在工作行生效，触发 warning
        issues.append(Issue("auto_fixed", "warning", fix_msg, tag=row.tag, field=row.keyword))

    # 4. VR/UID/值范围
    issues.extend(check_vr_format(working_row))
    issues.extend(check_uid(working_row))
    issues.extend(check_value_range(working_row))
    issues.extend(check_partial_confidence(working_row))

    # 5. 状态判定
    has_error = any(i.severity == "error" for i in issues)
    has_warning = any(i.severity == "warning" for i in issues)
    if has_error:
        status = "rejected"
    elif fixed_row is not None or has_warning:
        status = "fixed"
    else:
        status = "accepted"

    return ValidationResult(row=row, status=status, issues=issues, fixed_row=fixed_row)


# ── 批量校验 ──


def validate_rows(rows: list[MetadataRow]) -> ValidationSummary:
    summary = ValidationSummary()
    for row in rows:
        result = validate_single_row(row)
        final_row = result.fixed_row if result.fixed_row is not None else result.row
        if result.status == "rejected":
            summary.rejected_rows.append(final_row)
        else:
            summary.accepted_rows.append(final_row)
        if result.issues:
            summary.issues.append({
                "row": result.row.as_dict(),
                "status": result.status,
                "issues": [i.as_dict() for i in result.issues],
                "fixed": result.fixed_row.as_dict() if result.fixed_row else None,
            })
    return summary


# ── 跨 chunk 边界合并 ──


def _rows_match_for_merge(a: MetadataRow, b: MetadataRow) -> bool:
    """判断两行是否可视为同一字段的跨 chunk 延续。"""
    if a.tag and b.tag:
        return normalize_dicom_tag(a.tag) == normalize_dicom_tag(b.tag)
    if a.keyword and b.keyword:
        return a.keyword.lower() == b.keyword.lower()
    if a.description and b.description:
        return a.description.lower() == b.description.lower()
    return False


def find_merge_pairs(rows: list[MetadataRow]) -> list[tuple[int, int, MetadataRow]]:
    """在全部行中查找需要跨 chunk 合并的相邻行对。

    触发条件（任一）：
      - 前一行 needs_merge_next=true（自身完整但声明"我后面还有续行"）
      - 后一行 needs_merge_prev=true（前一行不完整，我是续行）
      - 前一行 partial=true 且 同行内字段在下方继续出现

    配对成功后两行都会被合并为一个新行，新行进入后行位置。
    """
    pairs: list[tuple[int, int, MetadataRow]] = []
    used: set[int] = set()

    for i, current in enumerate(rows):
        if i in used:
            continue
        # Case A: 当前行是续行（needs_merge_prev）
        if current.needs_merge_prev:
            for j in range(i - 1, max(-1, i - 5), -1):
                if j in used:
                    continue
                prev = rows[j]
                if not _rows_match_for_merge(prev, current):
                    continue
                if not (prev.needs_merge_next or prev.partial):
                    continue
                pairs.append((j, i, _make_merged_row(prev, current)))
                used.add(j); used.add(i)
                break
            continue
        # Case B: 当前行声明后面有续行（needs_merge_next）
        if current.needs_merge_next or current.partial:
            for j in range(i + 1, min(len(rows), i + 5)):
                if j in used:
                    continue
                nxt = rows[j]
                if not _rows_match_for_merge(current, nxt):
                    continue
                # 找到第一个匹配的同行字段就合并
                pairs.append((i, j, _make_merged_row(current, nxt)))
                used.add(i); used.add(j)
                break
    return pairs


def _make_merged_row(prev: MetadataRow, nxt: MetadataRow) -> MetadataRow:
    """构造合并后的新行：值拼接，confidence 取 min。"""
    merged_value = (prev.value or "") + (nxt.value or "")
    _, merged_keyword, merged_description = resolve_standard_field(
        nxt.description or prev.description
    )
    return MetadataRow(
        tag=nxt.tag or prev.tag,
        keyword=merged_keyword or nxt.keyword or prev.keyword,
        description=merged_description or nxt.description or prev.description,
        value=merged_value,
        source=f"merged_cross_chunk:{prev.source}+{nxt.source}",
        raw_label=f"{prev.raw_label} + {nxt.raw_label}" if prev.raw_label else nxt.raw_label,
        confidence=min(prev.confidence or 1.0, nxt.confidence or 1.0),
        tile_id=f"{prev.tile_id}:{nxt.tile_id}" if prev.tile_id and nxt.tile_id else None,
    )


def apply_merge_pairs(
    rows: list[MetadataRow],
    pairs: list[tuple[int, int, MetadataRow]],
) -> list[MetadataRow]:
    if not pairs:
        return list(rows)
    merge_targets: set[int] = set()
    for prev_idx, next_idx, _ in pairs:
        merge_targets.add(prev_idx)
        merge_targets.add(next_idx)
    merge_map: dict[int, MetadataRow] = {}
    for prev_idx, next_idx, merged in pairs:
        if next_idx not in merge_map:
            merge_map[next_idx] = merged
    result: list[MetadataRow] = []
    for idx, row in enumerate(rows):
        if idx in merge_targets:
            if idx in merge_map:
                result.append(merge_map.pop(idx))
            continue
        result.append(row)
    return result


def merge_boundary_rows(rows: list[MetadataRow]) -> list[MetadataRow]:
    pairs = find_merge_pairs(rows)
    if not pairs:
        return list(rows)
    return apply_merge_pairs(rows, pairs)


# ── 空间字段专项校验 ──


SPATIAL_FIELDS = {
    "ImagePositionPatient": {
        "tag": "(0020, 0032)",
        "description": "Image Position (Patient)",
        "expected_parts": 3,
        "labels": ("X", "Y", "Z"),
        "hint": "格式: X, Y, Z  (如 -156.277, -151.703, 81.892)",
    },
    "ImageOrientationPatient": {
        "tag": "(0020, 0037)",
        "description": "Image Orientation (Patient)",
        "expected_parts": 6,
        "labels": ("行X", "行Y", "行Z", "列X", "列Y", "列Z"),
        "hint": "格式: 行X,行Y,行Z, 列X,列Y,列Z  (如 1,0,0, 0,1,0)",
    },
    "PixelSpacing": {
        "tag": "(0028, 0030)",
        "description": "Pixel Spacing",
        "expected_parts": 2,
        "labels": ("行间距", "列间距"),
        "hint": "格式: 行间距, 列间距  (如 0.59375, 0.59375)",
    },
}


def validate_spatial_fields(rows: list[MetadataRow]) -> list[dict[str, Any]]:
    """校验三个空间字段（ImagePositionPatient / ImageOrientationPatient / PixelSpacing）。

    返回不合规项列表，每项形如：
    {
        "keyword": "...",
        "tag": "(0020, 0032)",
        "description": "Image Position (Patient)",
        "status": "missing" | "empty" | "part_count_mismatch" | "non_numeric",
        "message": "...",
        "hint": "...",
        ...
    }
    """
    issues: list[dict[str, Any]] = []
    row_by_keyword: dict[str, MetadataRow] = {}
    for row in rows:
        if row.keyword in SPATIAL_FIELDS:
            row_by_keyword[row.keyword] = row

    for keyword, spec in SPATIAL_FIELDS.items():
        row = row_by_keyword.get(keyword)
        if row is None:
            issues.append({
                "keyword": keyword, "tag": spec["tag"], "description": spec["description"],
                "status": "missing",
                "message": f"缺少 {spec['description']}，VL 未识别到该字段",
            })
            continue
        value = str(row.value or "").strip()
        if not value:
            issues.append({
                "keyword": keyword, "tag": spec["tag"], "description": spec["description"],
                "status": "empty",
                "message": f"{spec['description']} 的值为空",
            })
            continue
        parts = [p.strip() for p in re.split(r"[,/\\s]+", value) if p.strip()]
        expected = spec["expected_parts"]
        if len(parts) != expected:
            issues.append({
                "keyword": keyword, "tag": spec["tag"], "description": spec["description"],
                "status": "part_count_mismatch",
                "value": value,
                "expected_parts": expected,
                "actual_parts": len(parts),
                "labels": spec["labels"],
                "message": (
                    f"{spec['description']} 应有 {expected} 个分量 "
                    f"({', '.join(spec['labels'])}), 实际 {len(parts)} 个: {value}"
                ),
                "hint": spec["hint"],
            })
            continue
        invalid_indices: list[int] = []
        for i, p in enumerate(parts):
            try:
                float(p)
            except ValueError:
                invalid_indices.append(i)
        if invalid_indices:
            label_str = ", ".join(spec["labels"][i] for i in invalid_indices)
            issues.append({
                "keyword": keyword, "tag": spec["tag"], "description": spec["description"],
                "status": "non_numeric",
                "value": value,
                "invalid_indices": invalid_indices,
                "invalid_labels": label_str,
                "message": f"{spec['description']} 中 {label_str} 不是合法数值",
                "hint": spec["hint"],
            })
    return issues


# ── 顶层入口 ──


def validate_metadata(rows: list[MetadataRow]) -> ValidationSummary:
    """完整的校验流程：跨 chunk 合并 → 批量校验 → 空间字段校验。"""
    merged = merge_boundary_rows(rows)
    summary = validate_rows(merged)
    summary.spatial_issues = validate_spatial_fields(summary.accepted_rows + summary.rejected_rows)
    return summary


if __name__ == "__main__":
    import json
    import sys
    from parse_inputs import load_payload, detect_and_parse

    if len(sys.argv) < 2:
        print("用法: python validators.py <input_file>")
        sys.exit(1)
    payload = load_payload(sys.argv[1])
    rows, fmt = detect_and_parse(payload)
    print(f"# 识别格式: {fmt} | 行数: {len(rows)}", file=sys.stderr)
    summary = validate_metadata(rows)
    print(json.dumps(summary.as_dict(), ensure_ascii=False, indent=2))
