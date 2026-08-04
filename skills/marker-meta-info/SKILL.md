---
name: marker-meta-info
description: 对 DICOM 元数据进行质量校验。自动识别 5 种输入格式（VL 输出 / DOM 表格 / canonical / key_values / text_dump），执行硬剔除、VR 校验、UID 校验、A 类自动修复、跨 chunk 合并、空间字段专项检查，输出 5 件套产物（validated/rejected/warnings/spatial_issues/summary）。适用于 DICOM viewer 录屏识别、PDF 头抽取、DOM 表格抽取等异构来源。
---

# DICOM 元数据质量校验

对已提取的 DICOM 元数据进行可执行的质量校验。校验流程完整、可重现，输出可作为下游 CI/CD gate。

## 何时使用本 skill

- 从 DICOM viewer（如 uicloud/film）的录屏中识别 metadata，需要校验质量。
- 从 PDF 报告头部抽取 DICOM 头，需要自动剔除杂质。
- 把 VL（视觉模型）输出合并为标准 DICOM JSON，需要做格式校验 + 自动修复。
- 异构 metadata 来源（DOM 表格 + 文本 dump + 视觉模型）的统一校验入口。

## 快速使用

```bash
D:/Anaconda/envs/codegen-marker/python.exe scripts/validate_metadata.py <input> [-o output_dir] [--strict] [--quiet]
```

- `<input>`：文件路径（`.json` / `.txt`），或 `-` 表示 stdin
- `-o`：写入 5 件套产物到指定目录
- `--strict`：warning 等级也算失败（CI gate 用）
- `--quiet`：仅打印最终汇总

**示例**：
```bash
# 校验 VL 视觉模型输出
python scripts/validate_metadata.py test_data/vl_output.json -o out/

# 校验 DICOM dump 文本
python scripts/validate_metadata.py test_data/text_dump.txt -o out/

# 校验 key-values JSON
python scripts/validate_metadata.py test_data/key_values.json -o out/
```

## 5 种输入格式（自动识别）

| 格式 | 特征 | 典型来源 |
|---|---|---|
| `vl` | JSON 数组，每项含 `confidence`/`tile_id`，可能有 `partial`/`needs_merge_*` | Qwen-VL/GPT-4V 视觉模型 |
| `dom_table` | JSON 数组，每项含 `keyword`/`description`/`value`，keyword 可能有空格 | DOM 表格抽取 |
| `canonical` | JSON 数组，每项含 `tag`/`keyword`/`description`/`value` | 规范化中间表示 |
| `key_values` | JSON 字典（顶层是 `{}`），key=标准 keyword | 人工标注、键值映射 |
| `text_dump` | 多行 tab 分隔：`(tag)\t<desc>\t<value>` | dcmdump、PDF 复制 |

**详细格式说明**：见 [`references/input_formats.md`](references/input_formats.md)。

## 5 件套输出产物

| 文件 | 内容 |
|---|---|
| `validated_metadata_table.json` | 通过校验的行（含 `_fixed` 标记） |
| `rejected_rows.json` | 校验失败被拒绝的行 |
| `metadata_warnings.json` | 所有 issue 详情 |
| `spatial_issues.json` | 空间字段不合规项 |
| `validation_summary.json` | 顶层汇总（accepted/rejected/grade/missing_required） |

**Schema 详细说明**：见 [`references/output_schema.md`](references/output_schema.md)。

## 校验规则一览

| 类别 | 规则 | 严重度 |
|---|---|---|
| 硬剔除 | 0002 组 / SQ VR / 非 DICOM 文本 | error |
| 自动修复 | PatientPosition/Modality/Kernel → 大写 | warning + 修复 |
| VR 长度 | LO 64/2000 两档；DS 多值字段按 16×组件数计算 | warning/error |
| VR 格式 | CS=大写字母数字+空格下划线；DA=8 位数字；AS=3 位数字+DWMY | error |
| UID | 长度≥6 且只含数字+点 | error |
| tag↔description | tag 号必须与标准 keyword 匹配 | warning |
| 值范围 | PatientAge 格式、SliceThickness ∈ (0,10] | warning |
| VL 置信度 | `partial=true` 或 `confidence<0.6` | warning |
| 跨 chunk 合并 | `needs_merge_next`/`partial` 相邻行配对合并 | — |
| 空间字段 | ImagePositionPatient 3 / ImageOrientationPatient 6 / PixelSpacing 2 | 独立报告 |

**完整规则文档**：见 [`references/validation_rules.md`](references/validation_rules.md)。

## 质量等级与退出码

| 等级 | 触发条件 |
|---|---|
| `pass` | 必填字段齐全 + 0 rejected + warning≤5 |
| `warn` | 必填字段齐全 + rejected>0 或 warning>5 |
| `fail` | 缺失 PatientName/PatientID/StudyInstanceUID/SeriesInstanceUID/Modality |

**退出码**：
- `0`：grade=pass（或 grade=warn 且非 --strict）
- `1`：grade=fail，或 --strict 下 grade≠pass

## 模块结构

```
marker-meta-info/
├── SKILL.md                       # 本文件（导航入口）
├── scripts/
│   ├── parse_inputs.py            # 5 种格式解析 → MetadataRow
│   ├── validators.py              # 校验规则 + 自动修复 + 跨 chunk 合并 + 空间字段
│   └── validate_metadata.py       # CLI 主入口（参数解析、产物写入）
└── references/
    ├── input_formats.md           # 输入格式详细说明
    ├── validation_rules.md        # 校验规则详细说明
    └── output_schema.md           # 输出产物 Schema
```

## 编程式调用

```python
import sys
sys.path.insert(0, "scripts")
from parse_inputs import load_payload, detect_and_parse
from validators import validate_metadata

payload = load_payload("input.json")           # 自动读 JSON / 文本
rows, fmt = detect_and_parse(payload)          # 自动识别格式
summary = validate_metadata(rows)

print(summary.quality_grade())                 # 'pass' / 'warn' / 'fail'
print(summary.accepted_count, summary.rejected_count)
print(summary.missing_required_fields())
print(summary.spatial_issues)                  # 空间字段问题
```

## 依赖

- Python 3.11+（与项目其他工具保持一致）
- 仅标准库（`json` / `re` / `dataclasses`）
- 可选：`pydicom`（仅用于升级内置 VR 表，缺失时降级到 60+ 条目内置表）

## 参考实现

设计参考：`F:\18_dicomReader\dicom_tool\metadata_validator.py` + `metadata_extractor.py`。
本 skill 在其基础上做了：5 格式统一抽象、跨 chunk 合并逻辑修正（改为正向扫描）、DS 多值字段按组件数计算 max_len、产物拆分为 5 件套。
