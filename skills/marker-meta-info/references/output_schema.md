# 输出产物 Schema

`validate_metadata.py` 通过 `--output-dir` 写入 5 个文件到指定目录：

| 文件 | 内容 | 主要用途 |
|---|---|---|
| `validated_metadata_table.json` | 通过校验的行（含 `_fixed` 标记） | 下游流程输入 |
| `rejected_rows.json` | 校验失败被拒绝的行 | 错误排查 |
| `metadata_warnings.json` | 所有 issue 详情（含 warning 级） | 问题追踪 |
| `spatial_issues.json` | 空间字段不合规项 | UI 提示 / 数据完整性 |
| `validation_summary.json` | 顶层汇总 | 流程 gate（exit code） |

---

## 1. validated_metadata_table.json

`accepted` 行的列表（顺序：先 accepted，再 fixed；固定后的行带 `_fixed` 字段）。

```json
[
  {
    "tag": "(0010, 0010)",
    "keyword": "PatientName",
    "description": "Patient Name",
    "value": "张三",
    "source": "vl",
    "raw_label": "PatientName",
    "confidence": 0.95,
    "tile_id": "tile_0"
  },
  {
    "tag": "(0018, 5100)",
    "keyword": "PatientPosition",
    "description": "PatientPosition",
    "value": "HFS",
    "source": "vl",
    "raw_label": "PatientPosition",
    "confidence": 0.94,
    "tile_id": "tile_3",
    "_status": "fixed",
    "_issues": [
      {"code": "auto_fixed", "severity": "warning",
       "message": "tag (0018, 5100) 的值已自动修正: 'hfs' → 'HFS'",
       "tag": "(0018, 5100)", "field": "PatientPosition"}
    ],
    "_fixed": {
      "tag": "(0018, 5100)",
      "value": "HFS",
      "..."
    }
  }
]
```

**字段含义**：
- `tag` / `keyword` / `description`：见 [`input_formats.md`](input_formats.md)
- `value`：修复后的值（如果发生自动修复）
- `source`：原始来源（`vl` / `dom_table` / `text_dump` / `key_values` / `merged_cross_chunk:...`）
- `confidence`：VL 置信度，跨 chunk 合并时取 min
- `tile_id`：VL 分块标识，跨 chunk 合并时拼接为 `"tile_2:tile_3"`
- `_status`：`accepted` / `fixed` / `rejected`
- `_issues`：该行的全部 issue（含 warning 级）
- `_fixed`：自动修复后的行对象（仅在自动修复时出现）

---

## 2. rejected_rows.json

被硬剔除的行，结构同 accepted，但 `_status="rejected"`。

```json
[
  {
    "tag": "(0002, 0010)",
    "keyword": "TransferSyntaxUID",
    "description": "TransferSyntaxUID",
    "value": "1.2.840.10008.1.2.1",
    "source": "vl",
    "_status": "rejected",
    "_issues": [
      {"code": "group_0002", "severity": "error",
       "message": "tag (0002, 0010) 属于 0002 组 (File Meta Information)，应剔除",
       "tag": "(0002, 0010)", "field": "TransferSyntaxUID"}
    ]
  }
]
```

**常见拒绝原因**：
- `group_0002`：File Meta Information 字段泄漏
- `sequence_vr`：SQ 序列字段
- `non_dicom_text`：浏览器 UI 文本
- `vr_value_overlong`：LO 超 2000 字符 / DS 单组件 >16 / 其他 VR 超 max_len
- `vr_format_mismatch`：CS 非法字符 / DA 非 8 位 / AS 非 3 位数字 + D/W/M/Y 等
- `uid_too_short` / `uid_invalid_chars`：UID 格式错误
- `value_range_warning`（SliceThickness 非数值等）：仅当 SliceThickness 是 DS 但值非数值时算 error

---

## 3. metadata_warnings.json

所有 issue 的扁平列表（含 error 和 warning），每项关联到具体行：

```json
[
  {
    "row": { /* 完整 MetadataRow */ },
    "status": "accepted" | "fixed" | "rejected",
    "issues": [
      {"code": "...", "severity": "warning|error",
       "message": "...", "tag": "...", "field": "..."},
      ...
    ],
    "fixed": { /* 修复后的行，仅 fixed 时有 */ }
  },
  ...
]
```

**issue code 全集**：

| Code | 严重度 | 说明 |
|---|---|---|
| `group_0002` | error | 0002 组字段 |
| `sequence_vr` | error | SQ VR 字段 |
| `non_dicom_text` | error | UI 残留文本 |
| `tag_desc_mismatch` | warning | tag 与 description 不一致 |
| `auto_fixed` | warning | 值已自动修复 |
| `vr_value_overlong` | warning/error | VR 长度超限（按字段分级） |
| `vr_format_mismatch` | error | VR 格式不匹配（如 CS 含小写） |
| `uid_too_short` | error | UID 长度 <6 |
| `uid_invalid_chars` | error | UID 含非法字符 |
| `value_range_warning` | warning | 值范围异常（PatientAge/SliceThickness） |
| `partial_row` | warning | VL 标记 partial |
| `low_confidence` | warning | VL 置信度 <0.6 |

---

## 4. spatial_issues.json

三个空间字段（ImagePositionPatient / ImageOrientationPatient / PixelSpacing）的专项检查结果：

```json
[
  {
    "keyword": "ImagePositionPatient",
    "tag": "(0020, 0032)",
    "description": "Image Position (Patient)",
    "status": "part_count_mismatch",
    "value": "-100.5, -200.7",
    "expected_parts": 3,
    "actual_parts": 2,
    "labels": ["X", "Y", "Z"],
    "message": "Image Position (Patient) 应有 3 个分量 (X, Y, Z), 实际 2 个: -100.5, -200.7",
    "hint": "格式: X, Y, Z  (如 -156.277, -151.703, 81.892)"
  },
  {
    "keyword": "PixelSpacing",
    "tag": "(0028, 0030)",
    "description": "Pixel Spacing",
    "status": "non_numeric",
    "value": "0.5, 0.5mm",
    "invalid_indices": [1],
    "invalid_labels": "列间距",
    "message": "Pixel Spacing 中 列间距 不是合法数值",
    "hint": "格式: 行间距, 列间距  (如 0.59375, 0.59375)"
  }
]
```

**status 取值**：
- `missing`：VL 未识别
- `empty`：值为空
- `part_count_mismatch`：分量数与期望不一致
- `non_numeric`：某个分量非数值

**注意**：`spatial_issues` 不影响 `quality_grade`，是独立的数据完整性指标。下游流程通常用它来在 UI 上高亮提示。

---

## 5. validation_summary.json

顶层汇总，用于 CI/CD gate：

```json
{
  "accepted_count": 18,
  "rejected_count": 1,
  "warning_count": 2,
  "quality_grade": "warn",
  "missing_required": []
}
```

**字段说明**：
- `accepted_count` / `rejected_count` / `warning_count`：数字
- `quality_grade`：`pass` / `warn` / `fail`
- `missing_required`：必填字段缺失列表（无缺失时空数组）

**CLI 退出码判定**：

| 条件 | 退出码 |
|---|---|
| `quality_grade == "fail"` | 1 |
| `--strict` 且 `quality_grade != "pass"` | 1 |
| 其他 | 0 |

---

## 6. stdin 输入

`validate_metadata.py -` 从 stdin 读取 JSON 字符串（自动识别格式），输出与文件输入完全一致。
