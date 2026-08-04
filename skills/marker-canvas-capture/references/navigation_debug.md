# 导航故障诊断与 viewer 适配

## 诊断 Viewer 环境

当翻页不生效时，优先在 iframe 内执行以下 JS 检测 viewer 类型：

```javascript
// 在 scope.evaluate() 中执行
const info = {
    cornerstone3D: !!window.cornerstone3D,
    cornerstone: !!window.cornerstone,
    cornerstoneTools: !!window.cornerstoneTools,
    // 自研引擎
    dapengViewer: !!(window.setImageIndex || window.viewer?.setImageIndex),
    // canvas 类型
    canvasContext: (() => {
        const c = document.querySelector('canvas');
        if (!c) return 'none';
        try { return c.getContext('webgl2') ? 'webgl2' : c.getContext('webgl') ? 'webgl' : '2d'; } catch(e) { return 'error'; }
    })(),
};
```

## Viewer 类型对应策略

| viewer | `cornerstone3D` | `cornerstone` | 推荐导航策略 |
|---|---|---|---|
| **uicloud Dapeng** | `false` | `false` | **键盘方向键** ⭐ |
| cornerstone3D 新版 | `true` | `false` | JS API → 滚轮 |
| cornerstone 旧版 | `false` | `true` | JS API → 滚轮 |
| 联影 | `false` | `false` | 键盘 / 查找滚动条 |
| 放射沙龙 | `false` | `false` | 键盘 / 滚轮 |

## 导航策略详解

### 策略 1：viewer JS API（最快，精确跳转）

```javascript
// Dapeng 自研
window.setImageIndex(index)
window.viewer?.setImageIndex(index)
// cornerstone3D
window.cornerstone3D?.rendering?.getRenderingEngine?.('default')?.getViewports()?.[0]?.setImageIdIndex(index)
// cornerstone v1/v2
window.cornerstone?.scrollToIndex(element, index)
```

**特点**：直接跳到目标帧，不需要逐帧翻页。但依赖于 viewer 暴露 API。

### 策略 2：键盘方向键 ⭐（实测最通用）

```python
page1.keyboard.press('ArrowDown')   # 下一帧
page1.keyboard.press('ArrowUp')     # 上一帧
```

**适用**：所有支持键盘导航的 DICOM viewer（99% 都支持）。

**特点**：
- 不依赖 DOM 选择器
- 在 iframe 内也有效（Playwright 自动路由键盘事件到焦点 frame）
- 逐帧触发，速度约 60ms/帧

### 策略 3：鼠标滚轮

```python
# 先用 JS 找到主 canvas，再用 bounding_box 定位
canvas_box = scope.evaluate('''() => {
    const canvases = document.querySelectorAll("canvas");
    if (!canvases.length) return null;
    // 按面积排序取最大的（主画布）
    const sorted = [...canvases].sort((a, b) => b.width * b.height - a.width * a.height);
    const r = sorted[0].getBoundingClientRect();
    return {x: r.x, y: r.y, width: r.width, height: r.height};
}''')
if canvas_box:
    page1.mouse.move(canvas_box['x'] + canvas_box['width'] / 2, canvas_box['y'] + canvas_box['height'] / 2)
    page1.mouse.wheel(0, 120)  # 下一帧
```

**适用**：cornerstone3D 标准方案（`StackScrollMouseWheelTool`）。

**注意**：
- 必须先 `mouse.move` 到 canvas 上，否则 wheel 事件发给页面
- **不要硬编码 canvas ID**（如 uicloud 的 `#overlaycanvas-0_0`），不同 viewer ID 不同

### 策略 4：滑块/翻页按钮（viewer 专用，需先识别 viewer 类型）

部分 viewer 提供显式翻页按钮，**选择器因 viewer 而异**，不能直接套用：

```python
# ⚠️ 以下选择器是 uicloud 专用，其他 viewer 需要先 DOM 探查再确定
# uicloud Dapeng:
page1.locator('BUTTON#thumnailRow').click()  # 下一页
page1.locator('BUTTON#thumnailCol').click()  # 上一页
page1.locator('BUTTON#play-cine').click()    # 播放/暂停
```

**通用探查方法**：先在 DOM 中搜可能的翻页控件

```python
# 在 page1/iframe 中探查翻页控件
controls = scope.evaluate('''() => {
    const candidates = [];
    // 找 role=slider
    document.querySelectorAll('[role="slider"]').forEach(el => candidates.push({
        tag: el.tagName, role: 'slider', id: el.id, class: el.className
    }));
    // 找 input[type=range]
    document.querySelectorAll('input[type="range"]').forEach(el => candidates.push({
        tag: el.tagName, role: 'range', id: el.id, class: el.className
    }));
    // 找含翻页关键词的按钮
    document.querySelectorAll('button, [role="button"]').forEach(el => {
        const t = (el.textContent || '').toLowerCase();
        if (/next|prev|up|down|scroll|frame|page/.test(t) || /next|prev|up|down|scroll|frame|page/i.test(el.id || ''))
            candidates.push({tag: el.tagName, role: 'button', id: el.id, text: t.slice(0, 30)});
    });
    return candidates;
}''')
print(f"[探查] 翻页控件候选: {controls}")
```

**适用**：有显式翻页按钮的 viewer，点击后 viewer 自动翻一帧。

## 已知 viewer 控件列表

> 以下选择器均为 viewer 专有，使用时先通过 viewer 识别确定类型。

| viewer | iframe 选择器 | canvas 识别方式 | 翻页控件 |
|---|---|---|---|
| uicloud Dapeng | `[id="2d-iframe"]` | 面积排序（ID: `#overlaycanvas-0_0`） | `#thumnailCol` / `#thumnailRow` |
| cornerstone3D | 因实现而异 | 面积排序（常见 class: `.cornerstone-canvas`） | 滚轮 |
| cornerstone | 因实现而异 | 面积排序（常见 class: `.cornerstone-canvas`） | 滚轮 |

> **注意**：`canvas 识别方式` 列统一用 `querySelectorAll('canvas')` + 面积排序即可，括号中的 ID/class 仅为参考。

## 调试 Checklist

当翻页不生效时，按以下顺序排查：

```
[ ] 检测 viewer 类型（cornerstone3D / cornerstone / Dapeng）
[ ] 确认 canvas 存在且可见（bounding_box() 不为 None）
[ ] 确认 canvas 是 WebGL 还是 2D
    → WebGL: 用键盘 / 滚轮（toDataURL 截图正常工作）
    → 2D: 用键盘 / 滚轮 / JS API
[ ] 确认 click() canvas 后再翻页
[ ] 确认 mouse.wheel 前 mouse.move 到了 canvas 上
[ ] 确认 keyboard.press 时 canvas 有焦点
[ ] 查找页面上其他翻页控件（input[type=range] / button / [role=slider]）
```
