# -*- coding: utf-8 -*-
"""DICOM Meta 信息提取 — 固化共享模块。

从 DICOM viewer 的 meta 面板提取全部 tag 行。
面板已在调用前由录制脚本打开（用户点了 DICOM 按钮），
本模块只负责导航到正确 iframe → 提取 tag → 返回。

使用方式:
    from skills._shared.meta_extract import extract_meta_from_frame
    rows = extract_meta_from_frame(
        page,
        iframe_selectors=["#iframe", 'iframe[name="imageFrame"]'],
    )
"""
import re

_TAG_RE = re.compile(
    r"\((?:0?x)?([0-9a-f]{4})\s*,?\s*([0-9a-f]{4})\)"
    r"|(?<!\w)([0-9a-f]{4})\s*[,\s-]\s*([0-9a-f]{4})(?!\w)",
    re.IGNORECASE,
)
_TAG_DESCRIPTIONS = {
    "(0002,0016)": "AE Title",
    "(0008,0008)": "Image Type",
    "(0008,0016)": "SOP Class UID",
    "(0008,0018)": "Instance UID",
    "(0008,0020)": "Study Date",
    "(0008,0021)": "Series Date",
    "(0008,0022)": "Acquisition Date",
    "(0008,0023)": "Content Date",
    "(0008,0030)": "Study Time",
    "(0008,0031)": "Series Time",
    "(0008,0032)": "Acquisition Time",
    "(0008,0033)": "Content Time",
    "(0008,0050)": "Accession #",
    "(0008,0060)": "Modality",
    "(0008,0070)": "Manufacturer",
    "(0008,0080)": "Institution Name",
    "(0008,1010)": "Station Name",
    "(0008,1030)": "Study Description",
    "(0008,103E)": "Series Description",
    "(0008,1090)": "Model",
    "(0010,0010)": "Patient Name",
    "(0010,0020)": "Patient ID",
    "(0010,0030)": "Patient Birth Date",
    "(0010,0040)": "Patient Sex",
    "(0018,0015)": "Body Part",
    "(0018,1030)": "Protocol Name",
    "(0018,1020)": "Software Version",
    "(0002,0010)": "Transfer Syntax UID",
    "(0002,0013)": "Implementation Version Name",
    "(0020,0010)": "Study Id",
    "(0020,0011)": "Series #",
    "(0020,0012)": "Acquisition #",
    "(0020,0013)": "Instance #",
    "(0020,0032)": "Image Position Patient",
    "(0020,0037)": "Image Orientation Patient",
    "(0020,000D)": "Study UID",
    "(0020,000E)": "Series UID",
    "(0020,0052)": "Frame of Reference UID",
    "(0028,0002)": "Samples Per Pixel",
    "(0028,0004)": "Photometric Interpretation",
    "(0028,0010)": "Rows",
    "(0028,0011)": "Columns",
    "(0028,0030)": "Pixel Spacing",
    "(0028,0100)": "Bits Allocated",
    "(0028,0101)": "Bits Stored",
    "(0028,0102)": "HighBit",
    "(0028,0103)": "Pixel Representation",
    "(0028,1052)": "Rescale Intercept",
    "(0028,1053)": "Rescale Slope",
}


def _canonical_tag(match: re.Match) -> str:
    group, element = (match.group(1), match.group(2)) if match.group(1) else (
        match.group(3),
        match.group(4),
    )
    return f"({group.upper()},{element.upper()})"


def _resolve_frame_locator(page, iframe_selectors: list[str]):
    """沿 iframe 路径导航到目标 frame，返回 FrameLocator。

    参数 iframe_selectors 由 auto_gen.py 从录制脚本自动提取。
    例如 cxhospital: ["#iframe", 'iframe[name="imageFrame"]']
    """
    if not iframe_selectors:
        return page

    fl = page.locator(iframe_selectors[0]).content_frame
    for sel in iframe_selectors[1:]:
        fl = fl.locator(sel).content_frame
    return fl


def extract_meta_from_frame(page, iframe_selectors: list[str] | None = None) -> list[dict]:
    """从 DICOM 信息面板提取所有 tag 行。

    前置条件：面板已被录制脚本点击打开。

    Args:
        page: Playwright page 对象。
        iframe_selectors: iframe 嵌套路径，
            如 ["#iframe", 'iframe[name="imageFrame"]']。

    Returns:
        tag 行列表，每项 {"tag": str, "desc": str, "value": str}
    """
    # 尝试从 iframe 内读取
    body_text = ""
    if iframe_selectors:
        try:
            fl = _resolve_frame_locator(page, iframe_selectors)
            body_text = fl.locator("body").inner_text()
        except Exception:
            pass

    # 如果 iframe 内 < 5 个 tag 行，回退到 page 级读取
    # （有些 viewer 的 DICOM 面板是 page 级弹窗，不在 iframe 内）
    iframe_rows = _parse_tag_lines(body_text)
    if len(iframe_rows) < 5:
        try:
            page_text = page.locator("body").inner_text()
            page_rows = _parse_tag_lines(page_text)
            if len(page_rows) > len(iframe_rows):
                return page_rows
        except Exception:
            pass
    return iframe_rows


def _parse_tag_lines(body_text: str) -> list[dict]:
    """从纯文本中用正则提取 DICOM tag 行。"""
    rows: list[dict] = []
    seen: set[str] = set()

    for line in body_text.split("\n"):
        line = line.strip()
        if not line or len(line) < 10:
            continue
        matches = list(_TAG_RE.finditer(line))
        if not matches:
            continue

        next_description = line[:matches[0].start()].strip().rstrip(":：").strip()
        for index, match in enumerate(matches):
            tag = _canonical_tag(match)
            description = next_description or _TAG_DESCRIPTIONS.get(tag, "")
            segment_end = (
                matches[index + 1].start()
                if index + 1 < len(matches)
                else len(line)
            )
            remainder = line[match.end():segment_end].strip().lstrip(":：").strip()

            next_description = ""
            if index + 1 < len(matches):
                next_tag = _canonical_tag(matches[index + 1])
                expected_next = _TAG_DESCRIPTIONS.get(next_tag, "")
                if expected_next and remainder.casefold().endswith(
                    expected_next.casefold()
                ):
                    next_description = remainder[-len(expected_next):]
                    remainder = remainder[:-len(expected_next)].rstrip()

            if match.start() == 0 and "\t" in remainder:
                parts = [part.strip() for part in remainder.split("\t") if part.strip()]
                if len(parts) >= 2:
                    description = parts[0]
                    remainder = "\t".join(parts[1:])
            elif not description:
                parts = [
                    part.strip()
                    for part in re.split(r"[\t]", remainder, maxsplit=2)
                    if part.strip()
                ]
                description = parts[0] if len(parts) > 0 else ""
                remainder = parts[1] if len(parts) > 1 else ""

            if tag in seen:
                continue
            seen.add(tag)
            rows.append({
                "tag": tag,
                "desc": description,
                "value": remainder,
            })

    return rows
