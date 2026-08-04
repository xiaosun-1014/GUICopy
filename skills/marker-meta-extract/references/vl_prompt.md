# VL 回退 Prompt 模板

当 DOM 提取行数 < 10（可配置）时，进入 VL 回退：截图 → 发给视觉模型 → 解析 JSON。

## 调用流程

```python
page.screenshot(path='dicom_panel.jpeg', full_page=True)

# 1. 把截图发给 VL 模型
vl_result = call_vl_model(image_path='dicom_panel.jpeg', prompt=VL_PROMPT)

# 2. 解析 VL 输出为 rows 列表
rows = parse_vl_response(vl_result)
```

## Prompt 模板（中文）

```
这是 DICOM 影像查看器的 Meta 信息面板截图。请提取所有可见的 DICOM tag 行。

每行包含三列：
1. tag 号（格式 (GGGG,EEEE) 或 GGGG,EEEE 或 GGGG-EEEE）
2. tag 描述（如 PatientName、StudyDate、Modality）
3. tag 值（病人姓名、检查日期、影像模态等真实数据）

要求：
- 只输出 JSON，不要任何解释文字
- 即使某些行字段缺失，也要尽量提取能看到的部分
- 跳过非 DICOM tag 的行（如标题、按钮文字、版权信息）
- tag 号严格匹配 DICOM 标准格式

输出格式：
{
  "rows": [
    {"tag": "(0010,0010)", "desc": "PatientName", "value": "张三"},
    {"tag": "(0010,0020)", "desc": "PatientID", "value": "P12345"},
    ...
  ]
}
```

## Prompt 模板（英文，更适合 GPT-4V 等）

```
This is a screenshot of a DICOM viewer's Meta Information panel.
Extract all visible DICOM tag rows.

Each row has three columns:
1. tag ID (format: (GGGG,EEEE) or GGGG,EEEE or GGGG-EEEE)
2. tag description (e.g., PatientName, StudyDate, Modality)
3. tag value (real data like patient name, study date, modality code)

Requirements:
- Output JSON only, no explanation
- Even if some rows have missing fields, extract what you can see
- Skip non-DICOM rows (titles, buttons, copyright)
- tag IDs must strictly match DICOM standard format

Output:
{
  "rows": [
    {"tag": "(0010,0010)", "desc": "PatientName", "value": "John Doe"},
    ...
  ]
}
```

## 解析 VL 输出

VL 模型输出通常是 markdown 包 JSON，需要剥掉 ```json 围栏：

```python
import re
import json

def parse_vl_response(vl_text: str) -> list:
    """从 VL 输出中提取 rows 列表。"""
    # 1. 尝试直接 parse
    try:
        data = json.loads(vl_text)
        if isinstance(data, dict) and "rows" in data:
            return data["rows"]
        if isinstance(data, list):
            return data
    except json.JSONDecodeError:
        pass

    # 2. 尝试从 markdown ```json ... ``` 围栏中提取
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", vl_text, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group(1))
            return data.get("rows", [])
        except json.JSONDecodeError:
            pass

    # 3. 兜底：从文本中匹配第一个 {...}
    m = re.search(r"\{.*\}", vl_text, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group(0))
            return data.get("rows", [])
        except json.JSONDecodeError:
            pass

    return []
```

## VL 模型选择建议

| 模型 | 准确率 | 速度 | 成本 | 适用 |
|---|---|---|---|---|
| GPT-4V | 高 | 中 | 高 | 生产环境，需要高准确率 |
| Claude 3.5 Sonnet | 高 | 中 | 中 | 生产环境，需要结构化输出 |
| Qwen-VL-Max | 中 | 快 | 低 | 中文场景优先 |
| 本地 PaddleOCR + 规则 | 低 | 极快 | 极低 | 隐私敏感 / 离线 |

## 截图技巧

- `full_page=True` 比 `clip=` 更稳，能拿到整个 meta 面板
- 截图前 `page.wait_for_timeout(2000)` 等面板完全渲染
- 如果面板是独立 iframe，需要先 `frame.screenshot()` 再传给 VL
- 高分辨率（device_scale_factor=2）能提升小字识别率，但增加 token 消耗

## 与 marker-meta-info 校验的衔接

VL 输出的 rows 直接进入校验环节。注意 VL 输出常见问题：

- `value` 字段截断（长字符串被切掉）→ marker-meta-info 会标 warning
- `tag` 格式不规范（缺括号、缺逗号）→ 自动修复或 rejected
- 多行 value（如多行文本字段）→ `needs_merge_next` 标记，跨 chunk 合并

完整校验规则见 [`../../marker-meta-info/references/validation_rules.md`](../../marker-meta-info/references/validation_rules.md)。
