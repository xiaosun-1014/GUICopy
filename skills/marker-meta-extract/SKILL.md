---
name: marker-meta-extract
description: 处理 Playwright 录制脚本中的「Meta 信息工具」marker 的完整提取流程：定位上下文→匹配 viewer 配置→打开 DICOM 信息面板→多策略 DOM 提取 tag 行→DOM 失败则 VL 截图识别→质量校验→输出 dicom_meta.json。viewer-agnostic，所有具体选择器从共享 viewers.yaml 读取。用于补全含 # [MARKER: Meta 信息工具 @ ...] 的录制脚本。
triggers:
  - Meta 信息工具
requires_viewer_config: true
---

# Meta 信息完整提取

把 `# [MARKER: Meta 信息工具 @ {ts}]` 标记替换为「viewer 适配 → DOM 多策略提取 → VL 回退 → 校验 → 落盘」的完整代码块。

**核心约束**：**不写死任何 viewer 特定选择器**。所有 iframe 选择器、按钮文案、面板容器、tag 格式正则都从 [`../_shared/viewers.yaml`](../_shared/viewers.yaml) 读取，skill 只规定「流程 + 提取策略」，不规定「具体 selector」。

## 何时使用

- 录制脚本中存在 `# [MARKER: Meta 信息工具 @ YYYYMMDD_HHMMSS]` 标记行
- 需要补全为：自动打开面板 → 抽取所有 DICOM tag → 落盘 dicom_meta.json
- 适用于任何 DICOM viewer（uicloud / 联影 / 放射沙龙 / 影联 / 自建 PACS 等），不限于 uicloud

## 快速流程

```
1. 读 viewers.yaml，按录制脚本 URL 匹配 viewer（未命中则 generic）
2. 从录制脚本已有 locator() 反推 page 变量与 iframe 选择器
3. 生成打开面板的代码（按钮按 open_button_names 依次尝试；无 accessible name 时回退 CSS 图标按钮兜底）
4. 按 viewer.tag_row_format 选择 DOM 提取策略 → 拿 rows
   **注意**：用 Python locator 实现（`fl.locator('table tr').all()`），不要用 `frame.evaluate()` 跑 JS。
   dom_strategies.md 里每种策略都提供了 Python 版。
5. rows < 10 → VL 截图回退
6. 调 marker-meta-info skill 校验
7. 落盘 dicom_meta.json
8. 替换 marker 块为完整代码
```

## 目录结构

```
marker-meta-extract/
├── SKILL.md              ← 本文件（入口）
├── scripts/
│   └── extract_dicom_meta.py    主入口：从录制脚本生成替换代码
└── references/
    ├── viewer_config.md         viewers.yaml 字段说明 + 如何新增 viewer
    ├── dom_strategies.md        3 种 tag_row_format 的 DOM 提取实现
    └── vl_prompt.md             VL 回退 prompt 模板
```

## 使用方式

### A. 命令行（推荐）

```bash
D:/Anaconda/envs/codegen-marker/python.exe \
    skills/marker-meta-extract/scripts/extract_dicom_meta.py \
    --script processed_script.py \
    --viewers skills/_shared/viewers.yaml \
    --output-dir out/
```

输出：
- `out/dicom_meta.json` — 提取结果（占位，真实抽取在浏览器中执行）
- `out/patched_script.py` — 替换 marker 后的完整脚本

### B. 程序化调用

```python
import sys
sys.path.insert(0, "skills/marker-meta-extract/scripts")
from extract_dicom_meta import patch_script_with_meta_extraction

patched = patch_script_with_meta_extraction(
    script_path="processed_script.py",
    viewers_path="skills/_shared/viewers.yaml",
    marker_ts="20260624_111426",
)
print(patched)
```

## 关键约定

### viewer 匹配（必读）

1. **URL 匹配优先**：解析录制脚本中 `page.goto("...")` 的 URL，按 `url_patterns` 命中 viewer
2. **未命中走 generic**：用 generic 配置 + 从录制脚本已有 `locator(...)` 调用反推 iframe/按钮
3. **匹配失败不报错**：generic 配置会尝试多种常见按钮文案，覆盖主流 viewer

### iframe 选择器优先级

1. 录制脚本 marker 前后 5 行内已出现的 `locator("...")` 内容（最可靠，是真实录制结果）
2. viewer.iframe_selectors 配置（按列表顺序尝试）
3. generic → 全部失败时报告并退出

### 打开面板

生成打开 DICOM 信息面板的代码，按以下优先级依次尝试：

**1. accessible name 匹配** — 最标准的方式
```python
page1.get_by_role("button", name="DICOM信息 F2").click()
```
`viewer.meta_panel.open_button_names` 列表中的文案依次尝试，命中即停。

**2. CSS 图标按钮兜底** — 当所有 accessible name 都失败时

部分 viewer 的 DICOM 按钮是**图标按钮**，无 text / accessible name（如 cxhospital 的 `.eworldwebfont.button-icon.ewfont-Dicom`）。

从录制脚本已有 `locator(...)` 调用中反推 CSS 选择器：

```python
# 策略 2a：从录制脚本已有的 locator() 中查找含 Dicom/info 的 CSS 类
# 例如录制脚本中有：
#   page1.locator(".ewworldwebfont.button-icon.ewfont-Dicom").click()
# → 直接用该选择器
css = _infer_dicom_button_selector(script_lines)
if css:
    page1.locator(css).click()

# 策略 2b：在面板所在 iframe 内搜索候选图标按钮
fl.locator("[class*='Dicom'], [class*='dicom'], [class*='info'], [class*='Info'], [title*='DICOM'], [aria-label*='DICOM']").first.click()
```

**判定逻辑**：
1. 先尝试所有 `open_button_names`（`get_by_role("button", name=...)`）
2. 全部失败 → 从录制脚本中查找含 `Dicom` / `info` / `information` 的 CSS locator
3. 仍未找到 → 在 iframe 内用 CSS 属性选择器搜索：`[class*='Dicom']`, `[title*='DICOM']`, `[aria-label*='DICOM']`
4. 全部失败 → 报告并退出

### DOM 提取策略

由 `viewer.meta_panel.tag_row_format` 决定，按 `references/dom_strategies.md` 中的实现选择：
- `table_tr_td`：每行 `<tr>` 含 3+ 个 `<td>`，取 tag/desc/value
- `flex_div`：每行一个 row 容器，子节点分别含 tag/desc/value
- `tree_node`：递归树形结构，扁平化叶子节点

`extract_dicom_meta.py` 生成**Python locator 版**提取代码，原因：
- 兼容 `FrameLocator`（`.content_frame` 返回的类型，没有 `evaluate()` 方法）
- 兼容 `Page`（没有 iframe 时直接用 `page` 变量）
- 无需额外获取 `Frame` 对象，代码路径统一

`dom_strategies.md` 中每种策略都提供了**两种实现**：
- **Python locator 版（生成代码使用）**——通过 `fl.locator('...').all()` / `fl.locator('body').inner_text()` 获取数据，Python 侧解析
- JS `evaluate()` 版（仅当你有 `Frame` 对象时可参考）

### VL 回退

DOM 提取行数 < 10（可配置）才走 VL。VL prompt 模板见 `references/vl_prompt.md`。

### 与 marker-meta-info 的关系

校验环节调用 `marker-meta-info` skill 的 `validate_metadata()`，跨 skill 软依赖：
- 若 marker-meta-info 不可用 → 跳过校验，只落盘原始 rows（warning 输出）
- 若可用 → 完整 5 件套校验

## 文件依赖

- `../_shared/viewers.yaml` — viewer 注册表（**必需**，缺失则脚本无法运行）
- `../marker-meta-info/` — 可选，用于校验
- `../marker-sequence-select/` — 不依赖，独立

## 添加新 viewer

见 [`references/viewer_config.md`](references/viewer_config.md)。一句话：在 `viewers.yaml` 的 `viewers` 下追加一个条目，填好 `url_patterns` + `iframe_selectors` + `open_button_names` + `tag_row_format` + `tag_pattern` 即可。
