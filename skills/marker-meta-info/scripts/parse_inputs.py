"""
多格式 DICOM 元数据解析器。

支持的输入格式（自动识别）：
1. 已校验格式 (canonical)    [{"tag": "(0010,0010)", "description": "...", "value": "..."}]
2. DOM 三列 (dom_table)      [{"tagText": "...", "descriptionText": "...", "valueText": "..."}]
3. VL 模型输出 (vl)          [{"tag": "...", "description": "...", "value": "...", "confidence": 0.95, "partial": true}]
4. 纯文本 dump (text_dump)   多行字符串
5. KV 字典 (key_values)      {"Rows": "512", "Columns": "512", ...}

所有格式统一解析为 MetadataRow 对象列表，供 validators 校验。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any


# ── 标准 DICOM keyword 映射表（与 dicom_tool/metadata_extractor.py 对齐） ──

STANDARD_KEYWORDS: dict[str, tuple[str, str]] = {
    "WindowCenter": ("(0028, 1050)", "Window Center"),
    "WindowWidth": ("(0028, 1051)", "Window Width"),
    "Rows": ("(0028, 0010)", "Rows"),
    "Columns": ("(0028, 0011)", "Columns"),
    "RescaleSlope": ("(0028, 1053)", "Rescale Slope"),
    "RescaleIntercept": ("(0028, 1052)", "Rescale Intercept"),
    "SliceThickness": ("(0018, 0050)", "Slice Thickness"),
    "InstanceNumber": ("(0020, 0013)", "Instance Number"),
    "ImagesInAcquisition": ("(0020, 1002)", "Images In Acquisition"),
    "PixelSpacing": ("(0028, 0030)", "Pixel Spacing"),
    "SOPInstanceUID": ("(0008, 0018)", "SOP Instance UID"),
    "StudyInstanceUID": ("(0020, 000D)", "Study Instance UID"),
    "SeriesInstanceUID": ("(0020, 000E)", "Series Instance UID"),
    "ImagePositionPatient": ("(0020, 0032)", "Image Position Patient"),
    "ImageOrientationPatient": ("(0020, 0037)", "Image Orientation Patient"),
    "SliceLocation": ("(0020, 1041)", "Slice Location"),
    "BodyPartExamined": ("(0018, 0015)", "Body Part Examined"),
    "ProtocolName": ("(0018, 1030)", "Protocol Name"),
    "SeriesDescription": ("(0008, 103E)", "Series Description"),
    "StudyDescription": ("(0008, 1030)", "Study Description"),
    "PatientName": ("(0010, 0010)", "Patient Name"),
    "PatientID": ("(0010, 0020)", "Patient ID"),
    "PatientAge": ("(0010, 1010)", "Patient Age"),
    "PatientSex": ("(0010, 0040)", "Patient Sex"),
    "Modality": ("(0008, 0060)", "Modality"),
    "BitsAllocated": ("(0028, 0100)", "Bits Allocated"),
    "PhotometricInterpretation": ("(0028, 0004)", "Photometric Interpretation"),
}

FIELD_ALIASES: dict[str, str] = {
    "window center": "WindowCenter",
    "window level": "WindowCenter",
    "wl": "WindowCenter",
    "窗位": "WindowCenter",
    "层级": "WindowCenter",
    "window width": "WindowWidth",
    "ww": "WindowWidth",
    "窗宽": "WindowWidth",
    "rows": "Rows",
    "行数": "Rows",
    "columns": "Columns",
    "cols": "Columns",
    "列数": "Columns",
    "rescale slope": "RescaleSlope",
    "slope": "RescaleSlope",
    "rescale intercept": "RescaleIntercept",
    "intercept": "RescaleIntercept",
    "slice thickness": "SliceThickness",
    "thickness": "SliceThickness",
    "pixel spacing": "PixelSpacing",
    "instance number": "InstanceNumber",
    "images in acquisition": "ImagesInAcquisition",
    "sop instance uid": "SOPInstanceUID",
    "study instance uid": "StudyInstanceUID",
    "series instance uid": "SeriesInstanceUID",
    "image position patient": "ImagePositionPatient",
    "image orientation patient": "ImageOrientationPatient",
    "slice location": "SliceLocation",
    "body part examined": "BodyPartExamined",
    "protocol name": "ProtocolName",
    "series description": "SeriesDescription",
    "study description": "StudyDescription",
    "patient name": "PatientName",
    "patient id": "PatientID",
    "patient age": "PatientAge",
    "patient sex": "PatientSex",
    "modality": "Modality",
    "bits allocated": "BitsAllocated",
    "photometric interpretation": "PhotometricInterpretation",
}


# ── 必填字段（校验摘要时使用） ──

REQUIRED_FIELDS = {
    "PatientName": "(0010, 0010)",
    "PatientID": "(0010, 0020)",
    "StudyInstanceUID": "(0020, 000D)",
    "SeriesInstanceUID": "(0020, 000E)",
    "Modality": "(0008, 0060)",
}

SPATIAL_FIELD_KEYWORDS = {"ImagePositionPatient", "ImageOrientationPatient", "PixelSpacing"}


# ── 统一数据模型 ──


@dataclass
class MetadataRow:
    """DICOM 元数据行的统一表示。所有输入格式都被解析为该类型。"""
    tag: str = ""
    keyword: str = ""
    description: str = ""
    value: str = ""
    source: str = ""
    raw_label: str | None = None
    confidence: float | None = None
    tile_id: str | None = None
    partial: bool = False
    partial_side: str | None = None
    needs_merge_prev: bool = False
    needs_merge_next: bool = False

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "tag": self.tag,
            "keyword": self.keyword,
            "description": self.description,
            "value": self.value,
            "source": self.source,
            "raw_label": self.raw_label,
            "confidence": self.confidence,
            "tile_id": self.tile_id,
        }
        if self.partial:
            d["partial"] = True
            if self.partial_side:
                d["partial_side"] = self.partial_side
        if self.needs_merge_prev:
            d["needs_merge_prev"] = True
        if self.needs_merge_next:
            d["needs_merge_next"] = True
        return d


# ── 工具函数 ──


def normalize_dicom_tag(text: str | None) -> str:
    """把任意形式的 tag 字符串规范化为 `(GGGG, EEEE)` 格式。
    支持：(0010,0010) / 0x00100010 / (0010, 0010) / 0010,0010。
    """
    if not text:
        return ""
    raw = str(text).strip()
    # 1. 标准 (GGGG, EEEE) / GGGG,EEEE 格式
    match = re.search(r"\(?([0-9A-Fa-f]{4})\s*,\s*([0-9A-Fa-f]{4})\)?", raw)
    if match:
        return f"({match.group(1).upper()}, {match.group(2).upper()})"
    # 2. 合并十六进制 0xGGGGEEEE
    match = re.search(r"(?:0x)?([0-9A-Fa-f]{4})([0-9A-Fa-f]{4})\b", raw)
    if match:
        return f"({match.group(1).upper()}, {match.group(2).upper()})"
    return ""


def normalize_metadata_label(label: str | None) -> str:
    if not label:
        return ""
    return re.sub(r"\s+", " ", label.strip()).casefold().rstrip(":：")


def compact_metadata_label(label: str | None) -> str:
    return re.sub(r"[^\w一-鿿]+", "", normalize_metadata_label(label))


def resolve_standard_field(label: str | None) -> tuple[str, str, str]:
    """通过别名映射找到标准 (tag, keyword, description)。失败时返回空 tag/keyword。"""
    alias_key = normalize_metadata_label(label)
    keyword = FIELD_ALIASES.get(alias_key, "")
    if not keyword:
        compact_key = compact_metadata_label(label)
        keyword = FIELD_ALIASES.get(compact_key, "")
        if not keyword:
            for alias, candidate in FIELD_ALIASES.items():
                if compact_metadata_label(alias) == compact_key:
                    keyword = candidate
                    break
        if not keyword:
            for candidate in STANDARD_KEYWORDS:
                if compact_metadata_label(candidate) == compact_key:
                    keyword = candidate
                    break
    if not keyword:
        return "", "", (label or "").strip()
    tag, description = STANDARD_KEYWORDS.get(keyword, ("", keyword))
    return tag, keyword, description


# ── 各格式解析器 ──


def parse_canonical(payload: list[dict]) -> list[MetadataRow]:
    """格式 1：已校验 canonical 格式（已知 tag + description + value）。"""
    rows: list[MetadataRow] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        tag = normalize_dicom_tag(item.get("tag", ""))
        description = str(item.get("description") or item.get("desc") or "").strip()
        value = str(item.get("value") or "").strip()
        if not description or not value:
            continue
        _, keyword, std_desc = resolve_standard_field(description)
        rows.append(MetadataRow(
            tag=tag,
            keyword=keyword or std_desc or description,
            description=std_desc or description,
            value=value,
            source="canonical",
            raw_label=description,
        ))
    return rows


def parse_dom_table(payload: list[dict]) -> list[MetadataRow]:
    """格式 2：DOM 三列格式（tagText / descriptionText / valueText）。"""
    rows: list[MetadataRow] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        tag_text = str(item.get("tagText") or "").strip()
        desc_text = str(item.get("descriptionText") or item.get("description") or "").strip()
        value_text = str(item.get("valueText") or item.get("value") or "").strip()
        if not desc_text or not value_text:
            continue
        tag = normalize_dicom_tag(tag_text)
        _, keyword, std_desc = resolve_standard_field(desc_text)
        rows.append(MetadataRow(
            tag=tag,
            keyword=keyword or std_desc or desc_text,
            description=std_desc or desc_text,
            value=value_text,
            source="dom_table",
            raw_label=desc_text,
        ))
    return rows


def parse_vl(payload: list[dict]) -> list[MetadataRow]:
    """格式 3：VL 模型输出（含 confidence、partial 等扩展状态）。"""
    rows: list[MetadataRow] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        tag_text = str(item.get("tag") or "").strip()
        desc_text = str(item.get("description") or item.get("label") or "").strip()
        value_text = str(item.get("value") or "").strip()
        if not desc_text or not value_text:
            continue
        tag = normalize_dicom_tag(tag_text)
        _, keyword, std_desc = resolve_standard_field(desc_text)
        confidence_raw = item.get("confidence")
        try:
            confidence = None if confidence_raw in (None, "") else float(confidence_raw)
        except (TypeError, ValueError):
            confidence = None
        rows.append(MetadataRow(
            tag=tag,
            keyword=keyword or std_desc or desc_text,
            description=std_desc or desc_text,
            value=value_text,
            source="vl",
            raw_label=desc_text,
            confidence=confidence,
            tile_id=item.get("tile_id"),
            partial=bool(item.get("partial", False)),
            partial_side=str(item.get("partial_side") or "").strip() or None,
            needs_merge_prev=bool(item.get("needs_merge_prev", False)),
            needs_merge_next=bool(item.get("needs_merge_next", False)),
        ))
    return rows


def parse_text_dump(text: str) -> list[MetadataRow]:
    """格式 4：纯文本 dump（页面文本或 VL 多次输出拼接）。

    适配两类布局：
    A) 三行堆叠：Tag / Description / Value
    B) 单行列式：Tag Description Value 或 Description(xGGGGEEEE)\\tValue
    """
    normalized_text = str(text or "")
    if not normalized_text.strip():
        return []

    def _is_tag_line(line: str) -> bool:
        return bool(re.fullmatch(r"(?:\(?[0-9A-Fa-f]{4}\s*,\s*[0-9A-Fa-f]{4}\)?|0x[0-9A-Fa-f]{8})", line.strip()))

    def _starts_with_tag_record(line: str) -> bool:
        return bool(re.match(r"^(?:\(?[0-9A-Fa-f]{4}\s*,\s*[0-9A-Fa-f]{4}\)?|0x[0-9A-Fa-f]{8})\b", line.strip()))

    def _split_by_known_field(line: str) -> tuple[str, str]:
        tokens = [tok for tok in line.strip().split(" ") if tok]
        if len(tokens) < 2:
            return line.strip(), ""
        # 优先更长前缀
        for i in range(len(tokens) - 1, 0, -1):
            label_candidate = " ".join(tokens[:i]).strip()
            _, keyword, std_desc = resolve_standard_field(label_candidate)
            if keyword and i < len(tokens):
                value_candidate = " ".join(tokens[i:]).strip()
                if value_candidate:
                    return (std_desc or label_candidate), value_candidate
        return line.strip(), ""

    def _split_desc_value(line: str) -> tuple[str, str]:
        parts = [part.strip() for part in re.split(r"\t+|\s{2,}", line.strip()) if part.strip()]
        if len(parts) >= 2:
            return parts[0], " ".join(parts[1:])
        return line.strip(), ""

    raw_lines = normalized_text.splitlines()
    lines = [line.strip() for line in raw_lines if line.strip()]
    if not lines:
        return []

    rows: list[MetadataRow] = []
    seen: set[tuple[str, str, str]] = set()
    index = 0
    while index < len(lines):
        line = lines[index]

        # 适配 "Pixel Spacing(x00280030)\\t0.78125/0.78125" 格式
        paren_tag_match = re.match(r"^(.+?)\(x?([0-9A-Fa-f]{8})\)\s+(.+)$", line)
        if paren_tag_match:
            desc_raw = paren_tag_match.group(1).strip()
            tag_hex = paren_tag_match.group(2)
            value = paren_tag_match.group(3).strip()
            tag_str = f"({tag_hex[:4].upper()}, {tag_hex[4:].upper()})"
            if desc_raw and value:
                _, keyword, std_desc = resolve_standard_field(desc_raw)
                row = MetadataRow(
                    tag=tag_str,
                    keyword=keyword or std_desc or desc_raw,
                    description=std_desc or desc_raw,
                    value=value,
                    source="text_dump",
                )
                key = (row.tag or "", row.keyword or "", row.value or "")
                if key not in seen:
                    seen.add(key)
                    rows.append(row)
                index += 1
                continue

        # 标准 Tag 开头的行
        tag_match = re.match(r"^(\(?[0-9A-Fa-f]{4}\s*,\s*[0-9A-Fa-f]{4}\)?|0x[0-9A-Fa-f]{8})(.*)$", line)
        if not tag_match:
            index += 1
            continue

        tag_text = normalize_dicom_tag(tag_match.group(1))
        trailing = (tag_match.group(2) or "").strip(" :-|\t")
        description_text = ""
        value_text = ""

        # 同行解析
        if trailing:
            if "\t" in trailing:
                parts = [p.strip() for p in trailing.split("\t") if p.strip()]
                description_text = parts[0] if parts else ""
                value_text = "\t".join(parts[1:]).strip() if len(parts) > 1 else ""
            else:
                description_text, value_text = _split_desc_value(trailing)
            if not value_text:
                description_text, value_text = _split_by_known_field(trailing)

        # 跨行解析
        if not value_text and index + 1 < len(lines):
            next_line = lines[index + 1]
            if _is_tag_line(next_line) or _starts_with_tag_record(next_line):
                index += 1
                continue
            cand_desc, cand_value = _split_desc_value(next_line)
            if cand_value:
                description_text = cand_desc
                value_text = cand_value
                index += 1
            elif index + 2 < len(lines):
                value_line = lines[index + 2]
                if not _is_tag_line(value_line) and not _starts_with_tag_record(value_line):
                    description_text = next_line.strip()
                    value_text = value_line.strip()
                    index += 2

        description_text = description_text.strip()
        value_text = value_text.strip()
        if description_text and value_text:
            _, keyword, std_desc = resolve_standard_field(description_text)
            dedupe_key = (tag_text, description_text, value_text)
            if dedupe_key not in seen:
                seen.add(dedupe_key)
                rows.append(MetadataRow(
                    tag=tag_text,
                    keyword=keyword or std_desc or description_text,
                    description=std_desc or description_text,
                    value=value_text,
                    source="text_dump",
                    raw_label=description_text,
                ))

        index += 1

    return rows


def parse_key_values(payload: dict) -> list[MetadataRow]:
    """格式 5：KV 字典（直接给字段名 → 值）。"""
    rows: list[MetadataRow] = []
    for label, value in payload.items():
        value_text = str(value or "").strip()
        if not value_text:
            continue
        tag, keyword, description = resolve_standard_field(str(label))
        rows.append(MetadataRow(
            tag=tag,
            keyword=keyword or str(label).strip(),
            description=description or str(label).strip(),
            value=value_text,
            source="key_values",
            raw_label=str(label),
        ))
    return rows


# ── 顶层入口：自动识别输入格式 ──


def detect_and_parse(payload: Any) -> tuple[list[MetadataRow], str]:
    """自动识别输入格式并解析。

    返回: (rows, detected_format)
    detected_format ∈ {canonical, dom_table, vl, text_dump, key_values, unknown}
    """
    if isinstance(payload, list):
        if not payload:
            return [], "canonical"
        first = payload[0]
        if isinstance(first, dict):
            # 优先级：vl > dom_table > canonical
            # vl 通常含 confidence / partial 字段
            if any(isinstance(it, dict) and ("confidence" in it or "partial" in it) for it in payload):
                return parse_vl(payload), "vl"
            if any(isinstance(it, dict) and ("tagText" in it or "descriptionText" in it or "valueText" in it) for it in payload):
                return parse_dom_table(payload), "dom_table"
            if any(isinstance(it, dict) and ("tag" in it or "description" in it or "desc" in it) for it in payload):
                return parse_canonical(payload), "canonical"
        return [], "unknown"

    if isinstance(payload, dict):
        return parse_key_values(payload), "key_values"

    if isinstance(payload, str):
        text = payload.strip()
        if not text:
            return [], "text_dump"
        # 尝试按 JSON 解析
        if text.startswith("{") or text.startswith("["):
            try:
                parsed = json.loads(text)
                return detect_and_parse(parsed)
            except json.JSONDecodeError:
                pass
        # 否则当作纯文本 dump
        return parse_text_dump(text), "text_dump"

    return [], "unknown"


def load_payload(path: str | Path) -> Any:
    """从文件读取输入。自动识别扩展名。"""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"输入文件不存在: {p}")
    if p.suffix.lower() == ".json":
        return json.loads(p.read_text(encoding="utf-8"))
    return p.read_text(encoding="utf-8")


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("用法: python parse_inputs.py <input_file>")
        sys.exit(1)
    payload = load_payload(sys.argv[1])
    rows, fmt = detect_and_parse(payload)
    print(f"识别格式: {fmt}")
    print(f"解析行数: {len(rows)}")
    for row in rows:
        print(json.dumps(row.as_dict(), ensure_ascii=False))
