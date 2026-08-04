# DOM 选择器策略

zscloud viewer 的 DOM 结构在不同 layout / 不同协议模板下会变化。下面是按优先级排列的 selector 策略,每一步都有兜底。

## 1. iframe 选择器

**zscloud 顶层 page 只有一个 iframe**(viewer 整个嵌在里面)。

```python
frame = page.locator("iframe").content_frame
```

兜底:

```python
# 自动找含 canvas 的 frame
for f in page.frames:
    if f != page.main_frame:
        try:
            if f.locator("canvas").count() > 0:
                frame = f
                break
        except Exception:
            continue
```

## 2. 协议项

**协议列表通常在左侧 panel**,每个协议是一个 `<li>` 或 `<div>`,文本形如 `5*5` / `5×5` / `Axial`。

### 选择器策略(按优先级)

```javascript
// 策略 1: 容器 + 子项
'.hp-list .hp-item'
'.protocol-list .protocol-item'
'.series-protocol-list > li'

// 策略 2: 文本匹配(更通用)
Array.from(document.querySelectorAll('li, div, span')).filter(el => {
    const t = (el.innerText || '').trim();
    return /^1?\d\s*[*xX×]\s*\d$/.test(t);  // 形如 "5*5"
});
```

### 交互方式

**必须用 `dblclick`**,不是 `click`。单击是 "预览",双击才是 "应用"。

```python
# Playwright dblclick(以坐标方式,最稳)
page.mouse.dblclick(x + w / 2, y + h / 2)
```

为什么不用 `el.dblclick()`:viewer 协议项经常被其他元素覆盖或在滚动容器外,Playwright 自动滚动 + click 可能点不到。手动计算坐标 + `mouse.dblclick` 最稳。

### 可见性

协议面板可能在折叠状态,需要先展开。判断:

```javascript
const panel = document.querySelector('.hp-list');
if (panel && panel.offsetParent === null) {
    // 不可见,找展开按钮
    document.querySelector('[class*="protocol-toggle"], [class*="hp-toggle"]')?.click();
}
```

## 3. 布局切换按钮

布局按钮通常在顶部 toolbar,文本形如 `1x1` / `2x2` / `1×1` / `1 X 1`。

### 文本正则

```javascript
/^1\s*[xX×*]\s*1$/
```

### 选择器

```javascript
// 策略 1: 按钮元素
Array.from(document.querySelectorAll('button, [role="button"]')).filter(...)

// 策略 2: 所有可点击元素
Array.from(document.querySelectorAll('button, div, span, li, a')).filter(...)
```

**优先选按钮**(tagName 是 BUTTON 或有 role),不是大块面板(面积太大可能误中)。

### 交互方式

布局按钮单击即可(不像协议要双击)。

```python
el.click()
```

## 4. WW / WL 输入框

这是**最容易踩坑**的部分。

### 位置特征

- **右下角** viewport-corner 区域
- 两个**小的**输入框(典型 < 60px 宽)
- 旁边通常有 "WW" / "WL" 文字标签

### 元素类型

三种可能:

| 类型 | 特征 | 设值方式 |
|---|---|---|
| `<input type="number">` | 标准 | `el.value = '2000'; dispatchEvent('input')` |
| `<input type="text">` | 标准 | 同上 |
| `<div contenteditable="true">` | 自定义控件 | `el.innerText = '2000'; dispatchEvent('input')` |

### 选择器策略(组合)

```javascript
const inputs = Array.from(document.querySelectorAll('input, [contenteditable="true"]'));

// 1. 找在右下角的(vw/2, vh/2 之后)
const rightBottom = inputs.filter(el => {
    const r = el.getBoundingClientRect();
    return r.x > vw * 0.5 && r.y > vh * 0.5 && r.width < 100 && r.width > 0;
});

// 2. 通过祖先节点的文本找 WW/WL 标签
function findByLabel(area, keywords) {
    for (const el of area) {
        let parent = el;
        for (let i = 0; i < 4 && parent; i++) {
            const txt = (parent.innerText || '').toLowerCase();
            if (keywords.every(k => txt.includes(k.toLowerCase()))) {
                return el;
            }
            parent = parent.parentElement;
        }
        // placeholder / title / aria-label
        const ph = ((el.placeholder || '') + ' ' + (el.title || '') + ' ' +
                    (el.getAttribute('aria-label') || '')).toLowerCase();
        if (keywords.every(k => ph.includes(k.toLowerCase()))) {
            return el;
        }
    }
    return null;
}

const wwEl = findByLabel(rightBottom, ['ww', 'window width']) ||
             findByLabel(inputs, ['ww', 'window width']);
const wlEl = findByLabel(rightBottom, ['wl', 'window level']) ||
             findByLabel(inputs, ['wl', 'window level']);
```

### 设值 + 触发

```javascript
function setVal(el, val) {
    if (!el) return false;
    el.focus();
    if (el.tagName === 'INPUT') {
        el.value = String(val);
        el.dispatchEvent(new Event('input', {bubbles: true}));
        el.dispatchEvent(new Event('change', {bubbles: true}));
    } else {
        // contenteditable
        el.innerText = String(val);
        el.dispatchEvent(new Event('input', {bubbles: true}));
    }
    el.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true}));
    return true;
}
```

### 兜底方案

如果上述全失败,直接调 viewer 内部 API:

```javascript
const v = window.mainview.getViewports()[0];
// 不同 viewer 内部 API 不一样,常见的有:
// v.setWWWL(ww, wl);
// v.windowWidth = ww; v.windowLevel = wl;
// v.imageManager.setWindow(ww, wl);
```

> ⚠ 这是 viewer 私有 API,版本变化可能 break。仅在 DOM 设值失败时用。

## 5. 帧数 / 总层数

### 可靠来源

```javascript
const v = window.mainview.getViewports()[0];
const total = v.imageManager.availableImagesIndex.length;
```

### 兜底:DOM 文本解析

```javascript
const text = document.body.innerText;
// "1/68 张" 或 "/68" 或 "共68幅"
const m = text.match(/\/\s*(\d{2,4})\s*(?:张|幅|层|帧)?/);
if (m) total = parseInt(m[1]);
```

## 6. canvas 抓图

```javascript
// 1×1 布局下固定 id
const c = document.getElementById('0_0');

// 兜底:面积排序
const cs = document.querySelectorAll('canvas');
let best = cs[0], bestArea = 0;
for (const x of cs) {
    const a = x.width * x.height;
    if (a > bestArea) { bestArea = a; best = x; }
}
```

### 懒加载检测

```javascript
if (best.width === 0 || best.height === 0) {
    // 帧未加载,等待
}
```

## 调试:把 DOM dump 出来

如果某步失败,先把 viewer 当前 DOM 结构 dump 出来分析:

```python
html = frame.evaluate("() => document.body.innerHTML")
# 写到文件,grep 关键字
Path("debug_dom.html").write_text(html)
```

常用 grep:

```bash
grep -o 'class="[^"]*protocol[^"]*"' debug_dom.html
grep -o 'class="[^"]*layout[^"]*"' debug_dom.html
grep -o 'contenteditable="true"' debug_dom.html | head -5
```
