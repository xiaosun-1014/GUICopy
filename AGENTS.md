# AGENTS.md — 脚本补全指南

本文档总结将 `processed_script_{hospital}.py`（含 marker 占位注释）补全为可执行 `completed_{hospital}.py` 的全部知识。供 agent 和开发者参照。

---

## 目录

- [1. 核心概念](#1-核心概念)
- [2. Marker 总览](#2-marker-总览)
- [3. 各 Skill 补全策略详解](#3-各-skill-补全策略详解)
  - [3.1 报告截图 (marker-report-screenshot)](#31-报告截图)
  - [3.2 序列选择 (marker-sequence-select)](#32-序列选择)
  - [3.3 影像画布交互 (marker-canvas-capture)](#33-影像画布交互)
  - [3.4 Meta 信息工具 (marker-meta-extract)](#34-meta-信息工具)
  - [3.5 保留原始操作](#35-保留原始操作)
- [4. 三策略降级模式 (A→B→C)](#4-三策略降级模式)
- [5. Viewer 适配要点](#5-viewer-适配要点)
- [6. 输出文件命名规范](#6-输出文件命名规范)
- [7. 常见故障与修复](#7-常见故障与修复)
- [8. 调试工作流](#8-调试工作流)

---

## 1. 核心概念

### 什么是脚本补全？

GUI 录制工具 `main_gui.py` 将用户在浏览器中的操作录制成 Python 脚本（Playwright codegen 格式）。用户在关键位置通过右键菜单插入「标记」（marker）—— 形如 `# [MARKER: 报告截图 @ 20260624_172442]` 的注释行。

这些 marker 表示「在这里插入一段有意义的自动化逻辑」，但录制只产生占位注释，不产生真正的代码。补全就是将 marker 替换为可执行的代码。

### 工作流

```
GUI 录制 → processed_script.py（含 marker 占位）
                            ↓
              agent.py 读取 + skills/ 知识库
                            ↓
              completed_script.py（marker → 完整代码）
```

### 产出物层级

```
out/{hospital}/
├── processed_script_{hospital}.py    ← 输入（含 marker）
├── completed_{hospital}.py           ← 输出（可执行）
├── report.jpeg                       ← 报告截图
├── patient_info.json                 ← 页面病人信息（非 DICOM）
├── dicom_meta.json                   ← DICOM tag 提取结果
├── viewer_cx.jpeg                    ← viewer 全页快照
├── canvas_frames/                    ← 逐帧截图
│   ├── canvas_frame_0001_*.jpeg
│   └── ...
└── series_select_fallback.jpeg       ← 序列选择 VL 回退截图
```

---

## 2. Marker 总览

当前支持的 marker 类型：

| Marker 格式 | 含义 | 所属 Skill | 补全方式 |
|---|---|---|---|
| `# [MARKER: 报告截图 @ {ts}]` | 截图当前页面报告 | marker-report-screenshot | 固定代码替换 |
| `# [MARKER: 序列布局切换]` | 切换 viewer 布局 | 无（手编操作） | **保留原始录制代码** |
| `# [MARKER: 序列选择]` | 选择最优诊断序列 | marker-sequence-select | A→B→C 三策略降级 |
| `# [MARKER: 窗宽窗位 WL/WW]` | 调整窗宽窗位 | 无（手编操作） | **保留原始录制代码** |
| `# [MARKER: 影像画布交互]` | 全量帧翻页截图 | marker-canvas-capture | 完整管线替换 |
| `# [MARKER: Meta 信息工具 @ {ts}]` | 提取 DICOM tag | marker-meta-extract | Viewer 适配 + 多策略提取 |

> **关键规则**：「序列布局切换」和「窗宽窗位 WL/WW」没有对应的 skill，因为这些操作在录制时已经产生了完整的 Playwright 代码（按钮点击、输入填充等），marker 只是一个标签注释，**不要替换它们**。

> **关键规则**：`{ts}` 时间戳仅作为 marker 标识，**输出文件名不要带时间戳**。使用固定文件名（`report.jpeg`、`dicom_meta.json`、`patient_info.json` 等）。

---

## 3. 各 Skill 补全策略详解

### 3.1 报告截图

**Skill**：`skills/marker-report-screenshot/`

**替换目标**：
```python
# [MARKER: 报告截图 @ 20260624_172442]
# page.screenshot(path='report_*.png', full_page=True)
```

**替换为**：
```python
# [MARKER: 报告截图 @ 20260624_172442]
try:
    page.wait_for_load_state("networkidle", timeout=10000)
except Exception:
    print("[截图] networkidle 超时，降级继续")
page.wait_for_timeout(2000)
page.screenshot(path="report.jpeg", type="jpeg", quality=95, full_page=True)
```

**要点**：
- `networkidle` 必须加 timeout（DICOM viewer 有 WebSocket 长连接，可能永远不 idle）
- 超时降级后靠 `wait_for_timeout(2000)` 兜底
- 统一用 JPEG quality=95（非 PNG），文件小 80% 且肉眼无损
- 路径用 `SCRIPT_DIR / "report.jpeg"`（脚本同级目录）

---

### 3.2 序列选择

**Skill**：`skills/marker-sequence-select/`

**核心思想**：从 viewer 的序列列表中自动选择帧数最多、诊断价值最高的序列。

**策略 A（JS DOM 全量遍历）**—— 主策略：

```
1. 用 _find_viewer_frame(page) 定位含 canvas 的 frame（page.frames 中找）
2. frame.evaluate() 遍历 body 下所有可见元素
3. 每元素: _parse_slice_count(文本) → 有帧数则加入候选
4. 无帧数时: 厚度推断 → 有厚度则推断帧数
5. 仍然无帧数: _is_series_name(文本) → 是序列名也加入（slice_count=占位）
6. 去重（近似位置 key = round(x/10), round(y/10)）
7. 评分: (帧数, 关键字偏好分, -y坐标) → 选最高分
8. 计算 iframe 偏移补偿 → page.mouse.dblclick(目标中心)
```

> **⚠️ 必读：DOM 扫描只能用 `frame.evaluate()`，不要用 `page.evaluate` + `contentDocument`**
>
> 错误的做法：
> ```python
> # ❌ 这会在 Playwright 中返回空——contentDocument 经常为 null
> page.evaluate("document.querySelector('#iframe').contentDocument...")
> ```
> 正确的做法：
> ```python
> frame = _find_viewer_frame(page)  # 从 page.frames 中找到 Frame 对象
> nodes = frame.evaluate("""() => { /* 遍历 body * */ }""")
> ```
> `frame.evaluate()` 在 Frame 的 JavaScript 上下文中执行，不会受 contentDocument 限制。

> **⚠️ 必读：DOM 扫描需要重试循环（最长等 15 秒）**
>
> Viewer 页面加载后，序列列表 DOM 可能还没渲染完成。一次扫描可能返回空。
> 必须加 while 循环重试：
> ```python
> deadline = time.monotonic() + 15
> while time.monotonic() < deadline:
>     nodes = frame.evaluate("""() => { ... }""")
>     if nodes and _extract_candidates(nodes):
>         break
>     time.sleep(0.8)
> ```
> 没有重试循环 → 序列列表还没渲染就被跳过 → 策略 A 失效。

**`_parse_slice_count` 的宽松匹配**：

序列项文本可能不含帧数（如 "Body 1.0 CE"），此时 `_parse_slice_count` 返回 None。必须按以下优先级处理：

**第一步：厚度推断** — 从 "Body 1.0 CE" 中的 "1.0" 推断帧数

```python
# 厚度 → 估算帧数映射（CT 胸部/腹部扫描）
_THICKNESS_TO_FRAMES = [(0.6, 500), (1.0, 400), (1.25, 320),
                        (2.0, 200), (3.0, 130), (5.0, 80), (10.0, 30)]

def _infer_frames_from_thickness(text: str) -> int | None:
    """从文本中的层厚数字推断帧数。"""
    if len(text) < 8:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)\s*mm", text, re.I)
    if not m:
        return None
    thickness = float(m.group(1))
    for limit, frames in _THICKNESS_TO_FRAMES:
        if thickness <= limit:
            return frames
    return None
```

> **为什么要有厚度推断**：cxhospital 等 viewer 的序列列表不显示帧数（如 "Body 1.0 CE"），
> 不推断的话这类序列只能得 1 分（靠 `_is_series_name` 兜底），
> 会被各种病人信息文本（也匹配了 "CT"/"Body" 关键字，也得 1 分）淹没。
> 推断出 ~400 帧后按帧数排序自然胜出，**不需要加排除规则**。

**第二步：关键字兜底** — 仍无帧数时用 `_is_series_name` 二次判断

```python
_SERIES_KW = re.compile(
    r"(AIIR|Lung|MPR|CT|Thin|HRCT|VR|MIP|Coronal|Sagittal|Axial|"
    r"Bone|Brain|Mediastinum|Body|Chest|Abdomen|Head|CE|肺|骨|脑)", re.I
)

def _is_series_name(text: str) -> bool:
    if not text or len(text) > 120:
        return False
    if _SERIES_KW.search(text):
        return True
    if re.search(r"\d+\.?\d*\s*(mm|CE|MPR|VR)", text, re.I):
        return True
    return False
```

> **不要轻易加排除规则**：如果病人信息文本被误选为序列，不要着急加 `_is_excluded_text` 之类
> 的排除函数来把某些文本"过滤掉"。这往往是帧数解析不足的副作用——
> 正确解析帧数后真实序列的评分自然更高，不需要排除。排除规则会误杀真实序列。

**策略 B（文本块解析 + get_by_text 点击）**—— 回退：

```
1. 取 body.inner_text() → 双换行拆分块
2. 每个块首行 = 序列名，用 (帧数×10 + 关键字分) 评分
3. 选最高分 → get_by_text(名) → bounding_box → 坐标点击
```

**不限定关键字**：策略 B 必须尝试所有文本块，不要只匹配 AIIR|Lung|MPR 等。

**策略 C（VL 截图回退）**—— 最后手段：

```
1. iframe body.screenshot() 或 full_page screenshot
2. subprocess.run call_vl.py --task series_extract --image screenshot.jpeg
3. 解析 VL 返回的 sequences 列表
4. 选帧数最多的：page.mouse.dblclick(x + 偏移, y + 偏移)
```

**嵌套 iframe 坐标偏移补偿**：

```python
def _get_iframe_offset(page, outer_selectors, inner_selector):
    """累加每层 iframe 的 bounding_box 偏移。"""
    ox, oy = 0.0, 0.0
    for selector in outer_selectors:
        box = page.locator(selector).bounding_box()
        if box: ox += box["x"]; oy += box["y"]
    if inner_selector and outer_selectors:
        outer = page.locator(outer_selectors[0])
        inner = outer.content_frame.locator(inner_selector).bounding_box()
        if inner: ox += inner["x"]; oy += inner["y"]
    return ox, oy
```

---

### 3.3 影像画布交互

**Skill**：`skills/marker-canvas-capture/`

**替换目标**：
```python
# [MARKER: 影像画布交互]
# TODO: 调用 VL 模型对当前帧做判定 / 切帧
page.locator(...).click(position={"x":819,"y":318})
page.screenshot(path='viewer_cx.png', full_page=True)
```

**替换为**——完整管线：

```
capture_canvas_interaction(page, click_x, click_y, total_frames, output_dir)
│
├─ 1. _find_viewer_frame(page)    ← 定位含 canvas 的 frame（同序列选择）
│
├─ 2. 帧数：优先用 select_series 返回的 seq_frames
│     回退：parse_frame_numbers(scope) ← body.text 中找 "/总帧数" 或最大数字
│
├─ 3. _detect_hash_usable(scope)  ← 检测 drawImage 缩略图 hash 是否可用
│
├─ 4. 帧1：capture_current_frame() ← 不翻页，直接截
│
└─ 5. 帧2..N：循环：
     ├─ goto_frame(scope, page, idx)  ← 4 策略降级
     │   ├─ JS API: window.setImageIndex(idx)
     │   ├─ 键盘: page.keyboard.press('ArrowDown')  ← 最通用
     │   ├─ 滚轮: mouse.move + mouse.wheel(0, 120)
     │   └─ (无更多)
     ├─ wait_frame_ready(scope, prev_hash)  ← drawImage 缩略图 hash 轮询（1.5s 超时）
     │   降级：page.wait_for_timeout(300)
     └─ capture_current_frame()  ← JS toDataURL JPEG + 中心裁剪
```

**分帧等待机制**（关键性能优化）：

```javascript
// drawImage 到 2D scratch canvas → toDataURL 长度变化检测
// 绕过 WebGL preserveDrawingBuffer=false 限制
const scratch = document.createElement('canvas');
scratch.width = 80; scratch.height = 60;
const ctx = scratch.getContext('2d');
ctx.drawImage(sourceCanvas, 0, 0, sourceW, sourceH, 0, 0, 80, 60);
return scratch.toDataURL('image/jpeg', 0.3).length;
```

- 轮询间隔 150ms，超时 1.5s
- 比固定 `wait_for_timeout(1500)` 快 3-5 倍
- 不可用时（跨域 iframe）降级到 300ms 固定等待

---

### 3.4 Meta 信息工具

**Skill**：`skills/marker-meta-extract/`

**替换目标**：
```python
# [MARKER: Meta 信息工具 @ 20260624_173301]
# TODO: 提取当前检查的 Meta 信息 (Patient / Study / Series)
page.locator("#iframe").content_frame.locator("iframe[name=\"imageFrame\"]").content_frame.locator(".eworldwebfont.button-icon.ewfont-Dicom").click()
```

**替换为**——完整提取管线：

```
extract_meta_from_panel(page)
│
├─ 1. 打开 DICOM 信息面板
│   ├─ 先尝试 get_by_role("button", name=...) ← open_button_names 列表
│   ├─ 全失败 → 从录制脚本反推 CSS selector（含 Dicom/info 类）
│   └─ 仍失败 → CSS 属性选择器兜底 [class*='Dicom']
│
├─ 2. page.wait_for_timeout(1500)
│
├─ 3. DOM 提取（按 tag_row_format 选策略）
│   ├─ table_tr_td: fl.locator('table tr') → td 链式取
│   └─ flex_div/tree_node: fl.locator('body').inner_text() → 正则匹配
│
├─ 4. 行数 < 10 → VL 截图回退（dicom_panel_fallback.jpeg）
│
└─ 5. 校验 + 落盘：json.dump 到 dicom_meta.json
```

**特殊情形——Marker 在进入 viewer 之前**：

第一个 Meta marker 通常在登录/报告页面，此时 viewer iframe 不可用，DICOM 信息面板不存在。此时**不调用 `extract_meta_from_panel`**，改为从当前页面 `body.inner_text()` 中正则匹配可见的病人/检查信息：

```python
for line in body_text.split("\n"):
    m = re.match(
        r"(?:姓名|PatientName|性别|Sex|年龄|Age|"
        r"检查号|Accession|检查类型|ExamType|"
        r"PatientID|StudyDate|Modality)\s*[:：]\s*(.+)",
        line, re.I,
    )
    if m:
        key = re.split(r"[:：]", line, maxsplit=1)[0].strip()
        patient_info[key] = m.group(1).strip()
```

输出为 `patient_info.json`（非 DICOM 标准 tag）。

---

### 3.5 保留原始操作

以下 marker **不替换**，保留录制产生的原始 Playwright 代码：

| Marker | 原因 |
|---|---|
| `# [MARKER: 序列布局切换]` | 录制时已产生 `.eworldwebfont.button-icon` 点击和 `.ui-layout-item` 点击，代码完整 |
| `# [MARKER: 窗宽窗位 WL/WW]` | 录制时已产生全部 spinbutton 输入和确定按钮点击，代码完整 |

处理方式：保持 marker 作为标签注释，原有代码不动。

---

## 4. 三策略降级模式

序列选择和 Meta 提取都使用 A→B→C 三级降级架构：

```
策略 A（主策略）：JS DOM 操作
  ├─ 需要: Frame 对象（非 FrameLocator）
  ├─ 优点: 全量 DOM 遍历，不限 viewer 类型
  └─ 缺点: 跨域 iframe 不可用
       ↓ 失败
策略 B（回退）：FrameLocator + 文本解析
  ├─ 需要: FrameLocator（兼容跨域）
  ├─ 优点: 不依赖 evaluate()
  └─ 缺点: 文本块解析精度不如 JS 遍历
       ↓ 失败
策略 C（最后手段）：VL 视觉模型
  ├─ 需要: VL_API_KEY 环境变量
  ├─ 优点: 对任何 viewer 都有效
  └─ 缺点: 慢（~5s），依赖外部 API
```

**策略 C 的前置条件**：
- `skills/vl-config/scripts/call_vl.py` 必须存在
- 环境变量 `VL_API_KEY` 必须设置
- `skills/vl-config/vl_config.json` 配置 `series_extract` task

---

## 5. Viewer 适配要点

### 已知 Viewer 对比

| 维度 | uicloud (uicloud.com) | cxhospital (cxss.zjcxph.com) |
|---|---|---|
| iframe 结构 | 单层 `[id="2d-iframe"]` | 嵌套 `#iframe` → `iframe[name="imageFrame"]` |
| 页面变量 | `page1`（popup） | `page`（同页面 iframe） |
| 画布选择器 | `#overlaycanvas-0_0` | `.cornerstone-canvas` |
| DICOM 按钮 | `get_by_role("button", name="DICOM信息 F2")` | CSS 图标 `.eworldwebfont.button-icon.ewfont-Dicom` |
| 序列文本格式 | "Body 1.0 CE 362幅"（有帧数） | "Body 1.0 CE"（无帧数） |
| Meta 面板格式 | table_tr_td | flex_div（多格式通用） |

### 适配 checklist

```
[ ] URL 匹配 → 确认 viewer 类型（cxhospital / uicloud / generic）
[ ] iframe 结构:
    [ ] 单层 / 嵌套 / 无 iframe（popup）
    [ ] 坐标偏移补偿公式
[ ] 页面变量名: page / page1
[ ] 画布选择器: 先查 viewers.yaml，没有则用 querySelectorAll + 面积排序
[ ] DICOM 按钮: 优先 accessible name，失败则 CSS 图标 + 属性选择器兜底
[ ] 序列文本格式: 有没有帧数数字 → 决定 _parse_slice_count 的兜底策略
```

### cxhospital 的坐标偏移

```
主页面 (0, 0)
  └─ #iframe (ox1, oy1)          ← page.locator('#iframe').bounding_box()
       └─ iframe[name="imageFrame"] (ox2, oy2)
            └─ 目标元素 (x, y)    ← frame.evaluate 返回的坐标
```

点击时：`page.mouse.dblclick(x + ox1 + ox2, y + oy1 + oy2)`

---

## 6. 输出文件命名规范

| 产物 | 文件名 | 格式 | 说明 |
|---|---|---|---|
| 处理后脚本 | `processed_script_{hospital}.py` | — | 含 marker 的录制脚本 |
| 补全脚本 | `completed_{hospital}.py` | — | 可执行脚本（本指南产出） |
| 报告截图 | `report.jpeg` | JPEG q95 | 报告页面全页截图 |
| Viewer 快照 | `viewer_cx.jpeg` | JPEG q95 | 进入 viewer 后的全页截图 |
| DICOM Meta | `dicom_meta.json` | JSON | DICOM tag 提取结果 |
| 页面信息 | `patient_info.json` | JSON | 非 DICOM 页面文本提取 |
| 画布帧 | `canvas_frame_{序号:04d}_{时间戳}.jpeg` | JPEG q95 | 影像逐帧截图 |
| 序列选择回退 | `series_select_fallback.jpeg` | JPEG | VL 回退截图 |

### 命名规则

- **不带时间戳**：报告、meta、页面信息等单次产出去掉时间戳
- **带时间戳**：canvas 帧可能有数百张，用序号 + 时间戳防重
- **目录**：所有输出放在 `SCRIPT_DIR / {文件名}`，即 `out/{hospital}/` 目录下

---

## 7. 常见故障与修复

### 7.1 路径错误

```
FileNotFoundError: 'out/cxhospital/dicom_meta_*.json'
```

**原因**：脚本从不同目录运行，相对路径失效。

**修复**：使用 `SCRIPT_DIR = Path(__file__).resolve().parent`，所有文件/路径用 `SCRIPT_DIR / filename` 构造。

### 7.2 序列选择全部失败（无候选或选了病人信息）

```
[序列选择] 策略 A: JS DOM 全量遍历...
[序列选择] A策略: 获取到 0 个可见元素
[序列选择] 策略 B: 文本块解析...
[序列选择] 策略 C: VL 截图回退...
[序列选择] ✓ C策略命中: 1
```

**原因 1：DOM 扫描用了 `page.evaluate` + `contentDocument`**（最常见）

```python
# ❌ 错误做法 — contentDocument 在 Playwright 中经常为 null
page.evaluate("document.querySelector('#iframe').contentDocument...")
```

`contentDocument` 在 Playwright 操作的同源 iframe 中也可能返回 null，而且没有异常抛出。
函数静默返回空列表，策略 A 直接失效。

**修复**：必须用 `frame.evaluate()`：
```python
frame = _find_viewer_frame(page)      # 从 page.frames 找到 Frame 对象
nodes = frame.evaluate("""() => {      # 在 Frame 的 JS 上下文中执行
    return Array.from(document.querySelectorAll('body *'))...
}""")
```

**原因 2：无重试循环** — DOM 还没加载完就扫描，序列列表未渲染

**修复**：加 15 秒重试循环，反复扫描直到找到候选。

**原因 3：选了病人信息而非序列**（如 "RM5413344 CT Hu Ping (1幅)"）

```
# 实际运行输出
[序列选择] ✓ A策略命中: RM5413344 CT Hu Ping (1幅)
```

**根因**：`_parse_slice_count` 找不到真实序列（如 "Body 1.0 CE"）的帧数 → slice_count=1；
病人信息文本也命中关键字得 slice_count=1 → 两者评分相同，靠 y 坐标分胜负。

**不要加排除规则！** 正确修复是加**厚度推断**。注意 cxhospital 的序列文本 "Body 1.0 CE" 不含 "mm" 后缀，
需在厚度推断中增加**裸数字匹配模式**（仅匹配带小数点的浮点数如 `1.0`，避免误抓整数帧数如 `(1帧)`）：
```python
# 模式 A：带 "mm" 后缀（标准格式，如 "1.0 mm"）
m = re.search(r"(\d+(?:\.\d+)?)\s*mm", text, re.I)
# 模式 B：裸数字（cxhospital 格式 "Body 1.0 CE" 无单位）
# 仅匹配带小数点的浮点数，避免误抓 "(1帧)" 中的整数
m = re.search(r"(?:\b|(?<=\D))(\d+\.\d+)(?:\b|(?=\D))", text)
```
推断出 400 帧后的真实序列评分远高于病人信息（1 帧），自然胜出，无需排除规则。

### 7.3 策略 C 不调用 VL

**原因**：只写了截图 + print，没有调用 `call_vl.py`。

**修复**：
```python
subprocess.run([sys.executable, str(vl_script), "--task", "series_extract", "--image", screenshot_path])
```

### 7.4 截图默认 PNG 不是 JPEG

**原因**：Playwright `page.screenshot()` 默认 `type="png"`。

**修复**：加参数 `type="jpeg", quality=95`。

### 7.5 坐标偏移导致点击不准

**原因**：嵌套 iframe 场景下 `frame.evaluate()` 返回的坐标是 iframe 视口坐标，不是主页面坐标。

**修复**：用 `_get_iframe_offset()` 累加每层 iframe 的 `bounding_box` 偏移。

### 7.6 画布截图为黑色

**原因**：
1. canvas 未聚焦：先用 `canvas.click()` 点击
2. WebGL preserveDrawingBuffer=false：用 drawImage 到 2D scratch canvas

### 7.7 VL 回退不支持嵌套 iframe

**原因**：`fl.locator("body").screenshot()` 在跨域/嵌套 iframe 中可能失败。

**修复**：try body.screenshot → except page.screenshot(full_page=True) 兜底。

### 7.8 `page.evaluate` + `contentDocument` 返回空

```
[序列选择] A策略: 获取到 0 个可见元素
```

**原因**：`iframe.contentDocument || iframe.contentWindow?.document` 在 Playwright 控制的浏览器中
经常返回 null（即使同源），导致 JS 函数 `return []`。没有异常抛出，调用方感知不到。

**修复**：永远用 `frame.evaluate()` 访问 iframe 的 DOM：
```python
# ✅ 正确
frame = _find_viewer_frame(page)  # 或 page.frames 中找到目标 frame
nodes = frame.evaluate("""() => { ... }""")

# ❌ 错误 — 不要用
page.evaluate("document.querySelector('#iframe').contentDocument...")
```

### 7.9 cornerstone 翻页不生效（帧 1-4 全是 52KB）

```
[画布] ✓ 帧 1 保存: canvas_frame_0001_....jpeg (52KB)
[画布] ✓ 帧 2 保存: canvas_frame_0002_....jpeg (52KB)
[画布] ✓ 帧 3 保存: canvas_frame_0003_....jpeg (52KB)
# 所有帧文件大小完全相同 → 翻页未生效
```

**原因**：键盘 `page.keyboard.press('ArrowDown')` 没有正确路由到 canvas frame。
cxhospital 等 cornerstone viewer 对键盘事件的需要 canvas 先聚焦。

**修复**：优先使用 cornerstone 原生 API 翻页：
```python
# 策略 1: JS API（cornerstone scrollToIndex）
frame.evaluate("""(index) => {
    const el = document.querySelector('.cornerstone-canvas');
    if (el && window.cornerstone?.scrollToIndex)
        window.cornerstone.scrollToIndex(el, index);
}""", target_index - 1)

# 策略 2: 键盘（需先聚焦 canvas）
canvas.click(position={"x": 1, "y": 1})
page.keyboard.press('ArrowDown')
```
不要先键盘后 API——cornerstone viewer 优先用原生 scroll API。

---

## 8. 调试工作流

### 补全脚本生成流程

```bash
D:/Anaconda/envs/codegen-marker/python.exe agent.py \
    out/{hospital}/processed_script_{hospital}.py \
    -o out/{hospital}/completed_{hospital}.py
```

### 当 complete.py 不合理时

```
发现 complete.py 代码不合理
  → 定位对应的 skill（如 marker-sequence-select）
  → 分析问题根因（选择器差异 / 等待策略 / 布局结构）
  → 更新 skill 内容（SKILL.md / references）
  → 重新运行 agent.py 生成 complete.py
  → 验证修复效果
  → 重复直到合理
```

**不要直接手改 complete.py** —— skill 是所有医院的共享知识库。

### 常见调试错误及正确做法

| 错误做法 | 为什么错 | 正确做法 |
|---------|---------|---------|
| 序列选不中 → 加排除规则过滤病人信息 | 帧数解析不足导致评分拉不开 | 加**厚度推断**让真实序列评分自然胜出 |
| DOM 扫不到序列 → 用 `page.evaluate` + `contentDocument` | contentDocument 在 Playwright 不可靠 | 用 `_find_viewer_frame` 取 Frame 后 `frame.evaluate()` |
| 一次扫描返回空 → 直接降级到 B/C | DOM 还没渲染完 | 加 15 秒重试循环 |
| 翻页不生效 → 改用 `mouse.wheel` | 键盘事件没路由到 canvas | 先试 cornerstone API，再聚焦 canvas 后键盘 |
| 策略 C 返回了值但没点击 | VL 只返回名字，没做双击 | 策略 C 获取到数据后必须执行 `page.mouse.dblclick()` |

### 手动运行验证

```bash
# 从 out/{hospital}/ 目录运行
D:/Anaconda/envs/codegen-marker/python.exe completed_{hospital}.py

# 或从项目根运行
D:/Anaconda/envs/codegen-marker/python.exe out/{hospital}/completed_{hospital}.py
```

两种方式都应工作，因为路径已用 `SCRIPT_DIR` 做脚本同级相对路径。

### 单元测试

```bash
D:/Anaconda/envs/codegen-marker/python.exe -m unittest discover -s test -v
```

---

## 附录：完整函数签名

```python
# 序列选择
def select_series(page: Page) -> tuple[str | None, int | None]
# 入口函数
def capture_canvas_interaction(
    page: Page, click_x: float, click_y: float,
    total_frames: int | None = None,
    output_dir: str = "canvas_frames",
) -> list[str]
# Meta 提取
def extract_meta_from_panel(page: Page) -> list[dict]
# 坐标偏移补偿（嵌套 iframe）
def _get_iframe_offset(page, outer_selectors, inner_selector) -> tuple[float, float]
# 定位含 canvas 的 frame
def _find_viewer_frame(page: Page) -> Frame | None
# 帧数解析
def _parse_slice_count(text: str) -> int | None
# 序列名判断（无帧数时降级）
def _is_series_name(text: str) -> bool
```
