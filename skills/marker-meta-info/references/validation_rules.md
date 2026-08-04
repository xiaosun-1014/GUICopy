# DICOM 元数据校验规则详解

本文件逐条描述 `scripts/validators.py` 中的全部校验规则，包括触发条件、严重程度、自动修复策略。

## 校验流水线

```
validate_metadata(rows)
  │
  ├─ merge_boundary_rows(rows)            # 跨 chunk 合并（VL partial 行）
  │
  ├─ validate_rows(merged)
  │   └─ for each row: validate_single_row
  │        ├─ 硬剔除（error 级直接 rejected）
  │        │   ├─ check_group_0002         # (0002,XXXX) File Meta Information
  │        │   ├─ check_sequence_vr        # VR=SQ 序列
  │        │   └─ check_non_dicom_text     # 浏览器 UI 残留
  │        ├─ check_tag_description_consistency  # warning，可后续修正
  │        ├─ try_auto_fix                # A 类自动修复
  │        ├─ check_vr_format
  │        ├─ check_uid
  │        ├─ check_value_range
  │        └─ check_partial_confidence
  │
  └─ validate_spatial_fields(accepted+rejected)
```

---

## 1. 硬剔除规则（error → rejected）

### 1.1 `group_0002` — 0002 组字段

**规则**：tag 形如 `(0002, XXXX)` 的字段属于 File Meta Information，不能出现在常规元数据中。
**严重度**：error
**示例**：
```
(0002,0010) TransferSyntaxUID 1.2.840.10008.1.2.1   → rejected
```

### 1.2 `sequence_vr` — SQ VR 字段

**规则**：VR 为 SQ（Sequence）的字段禁止作为元数据行（结构嵌套，需特殊处理）。
**严重度**：error
**触发**：`lookup_vr(tag) == "SQ"`。

### 1.3 `non_dicom_text` — 非 DICOM 自由文本

**规则**：value 匹配浏览器 UI 残留关键词时拒绝。
**严重度**：error
**触发模式**（任意一个）：
- `Login` / `登录` / `用户名` / `密码` / `Password`
- `Copyright` / `©` / `All Rights Reserved`
- `Page X of Y`
- `Close` / `关闭` / `OK` / `确定` / `Cancel` / `取消`
- `Loading` / `加载中` / `请稍候`

---

## 2. 自动修复规则（A 类）

`try_auto_fix(row)` 按 tag 查 `FIXERS` 字典映射到对应修复函数：

| Tag | 字段 | 修复函数 | 行为 |
|---|---|---|---|
| (0018, 1140) | RotationDirection | `_fix_rotation_direction` | 大写，提取字母到 `CW`/`CC` |
| (0018, 5100) | PatientPosition | `_fix_patient_position` | 大写，对照 6 个标准值 HFS/HFP/FFS/FFP/HFDR/HFDL |
| (0018, 0015) | BodyPartExamined | `_fix_cs_upper` | 全大写 |
| (0018, 0050) | SliceThickness | `_fix_cs_upper` | 备用（部分版本标记为 CS） |
| (0018, 1160) | ReconstructionAlgorithm | `_fix_cs_upper` | 全大写 |
| (0018, 1210) | ConvolutionKernel | `_fix_cs_upper` | 全大写 |
| (0008, 0060) | Modality | `_fix_cs_upper` | 全大写（CT/MR/MG/...） |
| (0018, 1190) | ReconstructionDiameter 等 | `_fix_ds_multi` | 多值 DS 标准化（去空格、合并连续分隔符） |

**修复成功**：产生 `Issue("auto_fixed", "warning", msg)`，最终 status 为 `fixed`。

**示例**：
```
PatientPosition: "hfs" → "HFS"  (auto_fixed, status=fixed)
PatientPosition: "Head First Supine" → "HFS"  (extract "HFS" via compact)
```

---

## 3. VR 格式校验

### 3.1 VR 字典（按 DICOM PS3.5 表 6.2-1）

| VR | max_len | pattern | 说明 |
|---|---|---|---|
| AE | 16 | — | Application Entity |
| AS | 4 | `^\d{3}[DWMY]$` | Age String（如 `045Y`） |
| CS | 16 | `^[A-Z0-9 _\\]*$` | Code String（全大写字母数字 + 空格下划线反斜杠） |
| DA | 10 | `^\d{8}$` | Date（如 `20240115`） |
| DS | 16 (×组件数) | — | Decimal String（可多值，每组件 ≤16） |
| DT | 54 | — | Date Time |
| IS | 12 | `^-?\d+(\.\d+)?$` | Integer String |
| LO | 64 | — | Long String |
| SH | 16 | — | Short String |
| ST | 1024 | — | Short Text |
| TM | 16 | — | Time |
| UI | 64 | `^[\d.]+$` | Unique Identifier |
| LT | 10240 | — | Long Text |
| PN | 64 | — | Person Name |

### 3.2 长度超限分级

| 字段 | 阈值 | 严重度 |
|---|---|---|
| LO | 64 < len ≤ 2000 | warning（可截断） |
| LO | len > 2000 | error（拒绝） |
| DS | 总长 ≤ 16 × 组件数 | 通过 |
| DS | 单组件 >16 | error（拒绝） |
| 其他 VR | 超过 max_len | error（拒绝） |

**DS 多值字段**：以 `,` `/` `\\` 分隔多个组件，每个组件独立校验 ≤16 字符。
```
ImagePositionPatient: "-156.277, -151.703, 81.892"
  组件数 = 3，有效 max_len = 48；总长 = 26 ≤ 48 → 通过
PixelSpacing: "0.59375, 0.59375"
  组件数 = 2，有效 max_len = 32；总长 = 17 ≤ 32 → 通过
```

### 3.3 `uid_too_short` / `uid_invalid_chars`

针对 VR=UI 或 keyword 含 "UID" 的字段：
- 长度 < 6 → `uid_too_short` (error)
- 含非数字非点字符 → `uid_invalid_chars` (error)

---

## 4. tag↔description 一致性

`check_tag_description_consistency`：根据 tag 反查 `STANDARD_KEYWORDS` 得到标准 keyword，与 row 的 keyword/description 比对。

**不一致 → warning（不拒绝）**。例：
```
tag = (0018, 0050)
STANDARD_KEYWORDS[(0018, 0050)] = ("SliceThickness", "Slice Thickness")
但 VL 输出 description = "Slice Thickness" keyword = "Slicethickness"
→ warning: tag (0018, 0050) 的标准 keyword 应为 SliceThickness
```

---

## 5. 值范围软警告

| Keyword | 规则 | 触发 |
|---|---|---|
| PatientAge | `^\d{3}[DWMY]$` | 不匹配 → warning |
| SliceThickness | 数值 ∈ (0, 10] | >10 → warning；≤0 → warning；非数值 → warning |

**严重度**：warning。不阻断通过。

---

## 6. VL 置信度

| 条件 | 触发 |
|---|---|
| `partial=true` | `partial_row` (warning) |
| `confidence < 0.6` | `low_confidence` (warning) |

---

## 7. 跨 chunk 合并

### 7.1 触发条件

`find_merge_pairs` 扫描全部行，配对满足以下任一条件的相邻行（距离 ≤4 行）：

**Case A**：`row[i].needs_merge_prev == true`
- 向前找 `row[j]`（j ∈ [i-4, i-1]），要求：
  - `_rows_match_for_merge(row[j], row[i])` 为真（同 tag、同 keyword 或同 description）
  - `row[j].needs_merge_next == true` 或 `row[j].partial == true`

**Case B**：`row[i].needs_merge_next == true` 或 `row[i].partial == true`
- 向后找 `row[j]`（j ∈ [i+1, i+4]），要求：
  - `_rows_match_for_merge(row[i], row[j])` 为真

### 7.2 合并行为

两行配对成功后，合并为新行：
```
value = row[j].value + row[i].value
tag = row[i].tag or row[j].tag
keyword/description = 标准化后的 result
source = "merged_cross_chunk:vl+vl"
confidence = min(row[j].confidence, row[i].confidence)
tile_id = "tile_2:tile_3"  # 合并多 tile
```

两行原行从结果中移除，新行插入到原 next 行位置。

### 7.3 示例

输入（VL 输出，间距 1）：
```json
[
  {"tag":"(0020,0013)","description":"InstanceNumber","value":"1",
   "partial":true,"needs_merge_next":true,"tile_id":"tile_2"},
  {"tag":"(0020,0013)","description":"InstanceNumber","value":"5",
   "tile_id":"tile_3"}
]
```

合并后：
```json
{
  "tag": "(0020, 0013)",
  "keyword": "InstanceNumber",
  "value": "15",
  "source": "merged_cross_chunk:vl+vl",
  "tile_id": "tile_2:tile_3",
  "confidence": 0.91
}
```

---

## 8. 空间字段专项校验

三个空间字段做单独检查，产出 `spatial_issues` 列表（不进入 accepted/rejected 计数）。

### 8.1 三个空间字段

| Keyword | Tag | 期望分量数 | 标签 |
|---|---|---|---|
| ImagePositionPatient | (0020, 0032) | 3 | X, Y, Z |
| ImageOrientationPatient | (0020, 0037) | 6 | 行X, 行Y, 行Z, 列X, 列Y, 列Z |
| PixelSpacing | (0028, 0030) | 2 | 行间距, 列间距 |

### 8.2 issue 状态

| 状态 | 触发 |
|---|---|
| `missing` | VL 完全未识别该字段 |
| `empty` | 字段存在但 value 为空 |
| `part_count_mismatch` | 分量数与期望不一致（如 ImageOrientationPatient 只有 4 个分量） |
| `non_numeric` | 某个分量无法转为 float |

每条 issue 包含 `hint`（人类可读的格式示例）。

---

## 9. 质量等级

`ValidationSummary.quality_grade()` 返回三档：

| 等级 | 条件 |
|---|---|
| `pass` | 必填字段全部命中 + rejected=0 + warning ≤5 |
| `warn` | 必填字段全部命中 + rejected>0 或 warning>5 |
| `fail` | 必填字段缺失（如 PatientName/StudyInstanceUID 等） |

**必填字段**（`REQUIRED_FIELDS`，见 `parse_inputs.py`）：
- PatientName
- PatientID
- StudyInstanceUID
- SeriesInstanceUID
- Modality

---

## 10. CLI 退出码

| 退出码 | 含义 |
|---|---|
| 0 | grade=pass（或 warn 且非 strict） |
| 1 | grade=fail，或 `--strict` 模式下 grade≠pass |
