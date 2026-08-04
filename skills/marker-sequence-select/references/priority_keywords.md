# 序列优先级关键字表

viewer-agnostic 评分规则，适用于所有 DICOM viewer 的序列名称。

## 优先级关键字 → 分数

```python
PRIORITY_KEYWORDS = {
    # 最高：AIIR_Lung 综合序列（薄层+MPR+Lung 一次扫描，信息量最大）
    'AIIR_Lung': 100, 'AIIR Lung': 100,
    # 最高：薄层原始数据（信息量最大，可重建任意面）
    'Thin': 100, 'HRCT': 100, '0.625': 100,
    # 最高：MPR 多平面重建（诊断必需）
    'MPR': 100, 'Coronal': 95, 'Sagittal': 95, 'Axial': 90,
    # 高：VR / MIP
    'VR': 80, 'Volume': 80, '3D': 80, 'MIP': 80, 'MaxIP': 80,
    # 中：特定窗宽
    'Lung': 60, '肺': 60, 'Bone': 60, 'Soft': 60, 'Brain': 60,
    'Mediastinum': 55, 'Med': 55,
    # 中：MinIP
    'MinIP': 50,
    # 跳过项
    'Scout': -1, 'Localizer': -1, 'Surview': -1, 'Topogram': -1,
    'Dose': -1, 'Radiation': -1,
}
```

## 帧数加分

```python
# 帧数加分（最高 +50）
info['priority_score'] += min(info['frames'] // 10, 50)
```

## 厚层惩罚

```python
# 帧数 < 50 且无 MPR/VR/MIP 标记 → 跳过
if info['frames'] > 0 and info['frames'] < 50 and info['priority_score'] < 80:
    info['skip'] = True
```

## 序列名匹配（柔性）

不同 viewer 的序列名格式差异较大，提取时只匹配关键字：

| viewer | 序列名样例 | 提取策略 |
|---|---|---|
| uicloud | `x 1.0 AIIR_LungMPR205362幅` | 关键字匹配 + 数字提取 |
| 联影 | `AIIR_LungMPR 1.0mm` | 关键字匹配 |
| 放射沙龙 | `Lung MPR 1.0mm 205 imgs` | 关键字匹配 + 英文 imgs |
| 影联 | `序列3: AIIR_LungMPR` | 关键字匹配 + 中文序列号 |

通用解析函数（与 viewer 无关）：

```python
import re

def parse_series(name: str) -> dict:
    info = {
        'raw': name,
        'priority_score': 0,
        'frames': 0,
        'tags': [],
        'skip': False,
    }
    for kw, score in PRIORITY_KEYWORDS.items():
        if kw in name:
            info['tags'].append(kw)
            info['priority_score'] = max(info['priority_score'], score)
            if score == -1:
                info['skip'] = True
    # 提取帧数（支持中文「幅」/ 英文「images / frames」/ 裸数字）
    m = re.search(r'(\d{3,})\s*(?:幅|images|frames|张)', name)
    if not m:
        m = re.search(r'(?<!\d)(\d{4,})(?!\d)', name)
    if m:
        info['frames'] = int(m.group(1))
        info['priority_score'] += min(info['frames'] // 10, 50)
    # 厚层惩罚
    if info['frames'] > 0 and info['frames'] < 50 and info['priority_score'] < 80:
        info['skip'] = True
    return info
```

## 优先级总览（给 LLM 的 prompt 模板）

```
🥇 最高（100 分）：AIIR_Lung / AIIR Lung — 薄层+MPR+Lung 综合序列，信息量最大
🥇 最高（100 分）：Thin / HRCT / 0.625 / 1.0 — 薄层原始数据，信息量最大
🥇 最高（100 分）：MPR / Coronal / Sagittal — 多平面重建，诊断必需
🥈 高（80 分）：VR / Volume / 3D / MIP / MaxIP — 三维重建，血管/骨科首选
🥉 中（60 分）：Lung / 肺 / Bone / Soft / Brain / Mediastinum — 特定窗宽优化
🥉 中（50 分）：MinIP — 最小密度投影，气道结构
❌ 必须跳过（-1）：Scout / Localizer / Surview / Topogram / Dose / Radiation — 非诊断影像
❌ 必须跳过：帧数 < 50 且无 MPR/VR/MIP 标记的厚层序列
```
