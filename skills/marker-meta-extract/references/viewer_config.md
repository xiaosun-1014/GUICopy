# Viewer 注册表字段说明

[`viewers.yaml`](../../_shared/viewers.yaml) 是所有 marker-* skill 的共享 viewer 适配配置。本文档说明每个字段的含义与新增 viewer 的步骤。

## 文件位置

- 主文件：`skills/_shared/viewers.yaml`（项目根，跨所有 skill 共享）
- 由 `marker-meta-extract/scripts/extract_dicom_meta.py` 加载
- 由 `marker-sequence-select` 通过同样的相对路径引用

## 字段含义

```yaml
viewers:
  <viewer-name>:
    # ---- viewer 识别 ----
    url_patterns:           # 列表，字符串包含匹配
      - "uicloud.com"

    iframe_selectors:       # 列表，依次尝试；第一个命中的就用
      - '[id="2d-iframe"]'

    # ---- Meta 信息面板（marker-meta-extract 用）----
    meta_panel:
      open_button_names:    # 按钮 accessible name，依次尝试
        - "DICOM信息 F2"
        - "DICOM信息"
      panel_container_selectors:   # 面板 DOM 容器，限定 DOM 扫描范围
        - "table"
      tag_row_format:       # 见 dom_strategies.md
        - "table_tr_td" | "flex_div" | "tree_node"
      tag_pattern:          # JS 正则字符串
        - '^\(?\d{4}[,\)\s]\s*\d{4}\)?$'
      close_button_selectors:   # 可选；用于不污染录制时优雅关闭面板
        - "[aria-label='Close']"

    # ---- 序列选择（marker-sequence-select 用）----
    sequence_select:
      canvas_selectors:     # 影像画布选择器
        - "#overlaycanvas-0_0"
      text_pattern:         # 序列文本匹配正则
        - '\d{3,}.*?(?:幅|images|frames)'
      item_container_selectors:   # 序列项容器
        - ".series-item"
```

## 匹配顺序

1. **URL 匹配**：解析录制脚本中 `page.goto("...")` 的 URL，按 `url_patterns` 字符串包含匹配
2. **未命中 → generic**：generic 兜底，所有字段都是「最宽松」配置（按钮别名多、容器选择器空）
3. **从录制脚本反推**：iframe 选择器、按钮别名优先从录制脚本已有 `locator(...)` 中识别

## 新增 viewer 的步骤

### 1. 录制一个真实脚本

启动浏览器，录制打开 Meta 信息面板的完整流程，导出 codegen 脚本。

### 2. 观察录制脚本中的关键选择器

```python
# 这些就是 viewer 特征
page1.locator("[id=\"2d-iframe\"]").content_frame.get_by_role("button", name="DICOM信息 F2").click()
page1.locator("[id=\"2d-iframe\"]").content_frame.locator("table").screenshot(...)
```

提取：
- iframe 选择器
- 按钮 accessible name
- 面板容器（table / div / 自定义）

### 3. 实测 DOM 提取

用浏览器 devtools 看 meta 面板的 DOM 结构：

```javascript
// 在 console 里跑
document.querySelectorAll('table tr').length
// 或者
Array.from(document.querySelectorAll('div')).filter(d => d.children.length >= 3).length
```

确认 `tag_row_format` 选哪个策略。

### 4. 在 viewers.yaml 追加条目

```yaml
viewers:
  <新 viewer 名>:
    url_patterns:
      - "<域名>"
    iframe_selectors:
      - '<实际 selector>'
    meta_panel:
      open_button_names:
        - "<实际按钮名>"
      panel_container_selectors:
        - "<实际容器>"
      tag_row_format: "<table_tr_td|flex_div|tree_node>"
      tag_pattern: '<实际匹配 tag 号的正则>'
    sequence_select:
      ...
```

### 5. 用 skill 跑一遍验证

```bash
D:/Anaconda/envs/codegen-marker/python.exe \
    skills/marker-meta-extract/scripts/extract_dicom_meta.py \
    --script your_recording.py \
    --viewers skills/_shared/viewers.yaml \
    --output-dir out/
```

检查 `out/patch_report.json`，确认：
- `viewer` 字段匹配到新条目（不是 generic）
- `iframe` 是真实 selector

## 已知 viewer 列表

| viewer 名 | URL 特征 | iframe | 按钮 | 备注 |
|---|---|---|---|---|
| `uicloud` | uicloud.com | `[id="2d-iframe"]` | DICOM信息 F2 | 已适配 |
| `generic` | （兜底） | 从录制脚本反推 | 多别名依次尝试 | 永远最后匹配 |

## 不在 viewers.yaml 里的字段

- **校验规则**：属于 marker-meta-info skill 的职责，独立配置
- **VL prompt**：见 `vl_prompt.md`
- **输出路径**：脚本默认写到 cwd，可用 `--output-dir` 覆盖
