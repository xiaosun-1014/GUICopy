# DICOM 元数据输入格式

`scripts/parse_inputs.detect_and_parse()` 自动识别以下 5 种输入格式。识别优先级：
**vl > dom_table > canonical > key_values > text_dump**（VL 字段名最具区分度，优先匹配）。

所有格式最终都被规整为统一的 `MetadataRow` 数据结构（见 [`scripts/parse_inputs.py`](../scripts/parse_inputs.py)）。

---

## 1. canonical — VL/DOM 规范化列表

**特征**：JSON 数组，每项必有 `tag`/`keyword`/`description`/`value`，**不含** `confidence`/`tile_id`/`partial` 字段。

```json
[
  {
    "tag": "(0010, 0010)",
    "keyword": "PatientName",
    "description": "Patient Name",
    "value": "张三"
  },
  {
    "tag": "(0010, 0020)",
    "keyword": "PatientID",
    "description": "Patient ID",
    "value": "P-2024-001"
  }
]
```

**典型来源**：DOM 表格抽取后的中间表示、人工标注。

---

## 2. dom_table — DOM 表格行

**特征**：JSON 数组，每项有 `tag`/`keyword`/`description`/`value`，**不含** `confidence`/`tile_id`，但 keyword 可能带空格或大小写不规范（如 `"Patient  Name"`）。

识别条件：每项含 `keyword`/`description`，且**任何**一项存在 `partial`/`confidence`/`tile_id` 才走 VL 分支，否则走 dom_table/canonical。

```json
[
  {"tag": "(0010, 0010)", "keyword": "Patient Name", "description": "Patient Name", "value": "张三"},
  {"tag": "(0010, 0020)", "keyword": "Patient  ID", "description": "Patient ID", "value": "P-2024-001"}
]
```

**处理**：`compact_metadata_label()` 压缩 keyword 中的多余空格与下划线后查 `STANDARD_KEYWORDS`。

---

## 3. vl — 视觉模型输出

**特征**：JSON 数组，每项含 `tag`/`description`/`value`/**`confidence`**/**`tile_id`**，可能含 `partial`/`partial_side`/`needs_merge_prev`/`needs_merge_next`。

```json
{
  "tag": "(0020, 0032)",
  "description": "ImagePositionPatient",
  "value": "-156.277, -151.703, 81.892",
  "confidence": 0.88,
  "tile_id": "tile_2"
}
```

**partial 行**（关键！跨 chunk 合并的依据）：

```json
{
  "tag": "(0020, 0013)",
  "description": "InstanceNumber",
  "value": "1",
  "confidence": 0.95,
  "tile_id": "tile_2",
  "partial": true,
  "partial_side": "right",
  "needs_merge_next": true
}
```

**典型来源**：Qwen-VL / GPT-4V 等视觉模型从医学影像 UI 截图中识别的元数据。

---

## 4. key_values — 字段映射字典

**特征**：JSON 字典（不是数组），key 为标准 keyword（如 `PatientName`/`PatientID`），value 为字符串。

```json
{
  "PatientName": "李四",
  "PatientID": "P-2024-003",
  "StudyInstanceUID": "1.2.840.113619.2.55.3.604688119.971.1734567890.020",
  "Modality": "CT",
  "Rows": "1024"
}
```

**处理**：用 key 直接查 `STANDARD_KEYWORDS` 取 (tag, description)。

**注意**：JSON 字典被识别为 key_values **优先于** canonical — 如果你的输入是顶层字典，必然走 key_values。

---

## 5. text_dump — 多行纯文本

**特征**：多行字符串，每行用 **tab 分隔**，格式为：

```
(tag) <TAB> description <TAB> value
```

示例：

```
(0010,0010)	Patient Name	测试患者
(0010,0020)	Patient ID	P-2024-002
(0028,0010)	Rows	256
(0028,1053)	Rescale Slope	1
(0020,0032)	Image Position (Patient)	-100.5, -200.7, 50.3
```

**处理**：
- 用 `\t` 分隔（≥3 段才算有效行）。
- 段数为 2 时（无 description）退化为 `key<TAB>value`，按 key 查 `STANDARD_KEYWORDS`。
- 段数 <2 视为无效跳过。

**典型来源**：`dcmdump` / `pydicom` 的文本导出、PDF 复制的 DICOM 头。

---

## 统一数据结构

无论输入哪种格式，最终输出都是 `MetadataRow`：

```python
@dataclass
class MetadataRow:
    tag: str = ""           # 规范化 (XXXX,XXXX)，如 "(0010, 0010)"
    keyword: str = ""       # 标准 keyword，如 "PatientName"
    description: str = ""   # 英文描述，如 "Patient Name"
    value: str = ""         # 原始 value 字符串
    source: str = ""        # "vl" / "dom_table" / "text_dump" / "key_values"
    raw_label: str = ""     # 原始输入的字段名（用于追溯）
    confidence: float = 1.0 # VL 置信度，其他格式默认 1.0
    tile_id: str = ""       # VL 分块标识，如 "tile_2"
    partial: bool = False
    partial_side: str = ""  # "left"/"right"/"top"/"bottom"
    needs_merge_prev: bool = False
    needs_merge_next: bool = False
```

下游校验只看 `tag`/`keyword`/`value`/`partial`/`needs_merge_*`，其余字段用于追溯与调试。
