# DOM 提取策略

`viewers.yaml` 中 `meta_panel.tag_row_format` 决定用哪种策略提取 DICOM tag。本文档说明三种策略的实现细节与适用场景。

## 策略 1：`table_tr_td`

**适用**：meta 面板用 `<table>` 渲染，每行 `<tr>` 含 3 个 `<td>`（tag / desc / value）。

**代表 viewer**：uicloud、多数传统 PACS web viewer。

**DOM 示例**：
```html
<table>
  <tr><td>(0010,0010)</td><td>PatientName</td><td>张三</td></tr>
  <tr><td>(0010,0020)</td><td>PatientID</td><td>P12345</td></tr>
  ...
</table>
```

**提取脚本（JS evaluate 版 — 需 `Frame` 对象）：**
```javascript
() => {
    const containers = ["table"];  // panel_container_selectors
    const roots = containers.length
        ? containers.flatMap(s => Array.from(document.querySelectorAll(s)))
        : [document];
    const results = [];
    const tagRe = /^\(?\d{4}[,\)\s]\s*\d{4}\)?$/;
    for (const root of roots) {
        root.querySelectorAll("tr").forEach(tr => {
            const cells = tr.querySelectorAll("td, th");
            if (cells.length >= 3) {
                const tag = cells[0].textContent.trim();
                if (tagRe.test(tag)) {
                    results.push({
                        tag: tag,
                        desc: cells[1].textContent.trim(),
                        value: cells[2].textContent.trim(),
                    });
                }
            }
        });
    }
    return results;
}
```

**Python 实现（推荐，`FrameLocator` 兼容）：**
```python
import re

TAG_RE_STRICT = re.compile(r'^\(?\d{4}[,\)\s]\s*\d{4}\)?$')

def extract_table_tags(fl):
    """通过 FrameLocator.locator('table tr') 链式取行，Python 侧解析。"""
    results = []
    trs = fl.locator('table tr').all()
    for tr in trs:
        cells = tr.locator('td').all()
        if len(cells) >= 3:
            tag = cells[0].text_content().strip()
            if TAG_RE_STRICT.match(tag):
                results.append({
                    'tag': tag,
                    'desc': cells[1].text_content().strip(),
                    'value': cells[2].text_content().strip(),
                })
    return results
```

**说明**：`fl.locator('table tr').all()` 返回 `Locator` 列表，每个 `Locator` 可继续调用 `.locator('td').all()` 取子元素。所有操作都不需要 `evaluate()`。

## 策略 2：`flex_div`

**适用**：meta 面板用 `<div>` flex/grid 布局，每个 tag 行是一个 row 容器，子节点分别是 tag/desc/value。

**代表 viewer**：现代 web viewer（React/Vue 实现的）。

**DOM 示例**：
```html
<div class="meta-panel">
  <div class="meta-row">
    <span class="tag">(0010,0010)</span>
    <span class="desc">PatientName</span>
    <span class="value">张三</span>
  </div>
  <div class="meta-row">
    <span class="tag">(0010,0020)</span>
    <span class="desc">PatientID</span>
    <span class="value">P12345</span>
  </div>
  ...
</div>
```

**提取脚本（JS evaluate 版 — 需 `Frame` 对象）：**
```javascript
() => {
    const tagRe = /\(?\d{4}[,\)\s-]\s*\d{4}\)?/;
    const results = [];
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
    let node;
    while ((node = walker.nextNode())) {
        const children = Array.from(node.children);
        if (children.length >= 3) {
            const text0 = children[0].textContent.trim();
            if (tagRe.test(text0)) {
                results.push({
                    tag: text0,
                    desc: children[1].textContent.trim(),
                    value: children[2].textContent.trim(),
                });
            }
        }
    }
    return results;
}
```

**Python 实现（推荐，`FrameLocator` 兼容，格式无关）：**

此方案不依赖特定 DOM 结构，直接取 iframe 全部可见文本后用正则匹配 tag 行。
对 flex_div / tree_node / 任意未知结构都有效。

```python
import re

TAG_RE_LOOSE = re.compile(r'\(?\d{4}[,\)\s-]\s*\d{4}\)?')

def extract_tags_from_body(fl):
    """格式无关：取 iframe 全部可见文本，正则匹配 DICOM tag 行。"""
    body_text = fl.locator('body').inner_text()
    results = []
    seen = set()
    for line in body_text.split('\n'):
        line = line.strip()
        if not line or len(line) < 10:
            continue
        m = TAG_RE_LOOSE.search(line)
        if not m:
            continue
        tag = m.group(0)
        if tag in seen:
            continue
        seen.add(tag)
        remainder = line.replace(tag, '', 1).strip().lstrip(':').strip()
        parts = [p.strip() for p in re.split(r'[\t]', remainder, maxsplit=2) if p.strip()]
        results.append({
            'tag': tag,
            'desc': parts[0] if len(parts) > 0 else '',
            'value': parts[1] if len(parts) > 1 else '',
        })
    return results
```

**优点**：对任何 DOM 布局（table/flex/tree）都有效，不需要知道 viewer 的具体结构。
**缺点**：tag/desc/value 的列边界靠正则和分隔符推断，不如结构化遍历精确。

**策略选择建议**：先用 `extract_table_tags()`（如果页面上有 table），
如果返回行数 < 10，回退到 `extract_tags_from_body()`。

## 策略 3：`tree_node`

**适用**：meta 面板用树形结构（可展开/折叠），tag 在叶子节点，desc 在父节点路径中。

**代表 viewer**：少数功能复杂的 viewer（如某些放射科工作站 web 版）。

**DOM 示例**：
```html
<div class="tree">
  <div class="tree-node">
    <span class="node-label">Patient</span>
    <div class="tree-children">
      <div class="tree-leaf">(0010,0010)</div>
      <div class="tree-leaf">(0010,0020)</div>
    </div>
  </div>
  <div class="tree-node">
    <span class="node-label">Study</span>
    <div class="tree-children">
      <div class="tree-leaf">(0020,000D)</div>
    </div>
  </div>
</div>
```

**提取脚本（JS evaluate 版 — 需 `Frame` 对象）：**
```javascript
() => {
    const tagRe = /\(?\d{4}[,\)\s-]\s*\d{4}\)?/;
    const results = [];
    function walk(el, path) {
        if (!el.children || el.children.length === 0) {
            const text = el.textContent.trim();
            const m = text.match(tagRe);
            if (m) {
                results.push({
                    tag: text,
                    desc: path.slice(-2, -1).join(" / ") || "",
                    value: path.slice(-1)[0] || "",
                });
            }
            return;
        }
        for (const child of el.children) {
            walk(child, [...path, el.textContent.trim().slice(0, 50)]);
        }
    }
    walk(document.body, []);
    return results;
}
```

**Python 实现（推荐，`FrameLocator` 兼容）：**

tree_node 结构没有标准的选择器模式，最可靠的方案仍然是取 iframe 全部可见文本后正则匹配。
与 flex_div 的 Python 实现相同——因为不依赖 DOM 结构。

```python
# 同 flex_div 的 extract_tags_from_body()
# tree_node 的层级信息（分组）可通过 inner_text 中的缩进或空行推断
```

如果确实需要保留树形分组信息，可以尝试利用缩进来判断层级：

```python
def extract_tree_tags(fl):
    """尝试从 body 文本缩进推断 tree 层级。"""
    body_text = fl.locator('body').inner_text()
    results = []
    current_group = ""
    for line in body_text.split('\n'):
        stripped = line.strip()
        if not stripped:
            continue
        indent = len(line) - len(line.lstrip())
        # 缩进小 → 可能是分组名称（Patient / Study / Series）
        if indent < 2 and not TAG_RE_LOOSE.search(stripped):
            current_group = stripped
        m = TAG_RE_LOOSE.search(stripped)
        if m:
            tag = m.group(0)
            remainder = stripped.replace(tag, '', 1).strip().lstrip(':').strip()
            results.append({
                'tag': tag,
                'desc': current_group,
                'value': remainder,
            })
    return results
```

## 策略选择决策树

```
meta 面板是 <table> 渲染？
├── 是 → extract_table_tags()         （Python，结构化取 tr > td）
└── 否 → extract_tags_from_body()     （Python，格式无关，body inner_text + 正则）
         ├── 如果文本有明显缩进 → extract_tree_tags()（尝试保留分组信息）
         └── 否则 → extract_tags_from_body() 已足够
```

> Python locator 版本与 `FrameLocator` 兼容，不需要 `evaluate()` 或 `Frame` 对象。
> JS evaluate 版仅在已有 `Frame` 对象时可供参考。

## 提取失败的常见原因

| 现象 | 可能原因 | 处理 |
|---|---|---|
| `fl.locator('body').inner_text()` 返回空 | iframe 跨域 | 只能在截图后走 VL 回退 |
| 文本中含 tag 但 `TAG_RE_LOOSE` 没匹配 | 正则太严格 | 放宽或去掉正则首尾锚点 |
| rows 缺 desc/value | 文本中的列分隔符不是制表符 | 尝试用空格 `re.split(r'\s{2,}', ...)` 拆分 |
| rows 重复 | tag 行有重复（一些 viewer 会重复展示 Patient 模块） | 提取后做 `dedup by tag` |
| iframe 内容跨域，无法读 inner_text | meta 面板在独立 origin 的 iframe 中 | 回退到 VL 截图识别 |

## 自定义策略

如果三种都不匹配，可以：

1. 使用 `extract_tags_from_body()` 作为通用回退——它对所有 DOM 结构都有效，是格式无关的最后保障
2. 在 `extract_dicom_meta.py` 的 `_dom_extraction_code()` 里追加分支
3. 在 `viewers.yaml` 用新名字（如 `custom_xxx`），保持可扩展
4. 在本文档追加对应章节
