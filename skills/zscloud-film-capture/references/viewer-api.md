# zscloud Viewer JS API

`zscloud.zs-hospital.sh.cn/film` 内部嵌入的是**联影 UIH web2d viewer**(同 uicloud Dapeng 平台)。
viewer 通过 `window.mainview` 暴露,所有 DICOM 操作走 mainview → viewport → imageManager。

> **重要前提**:viewer 在 iframe 内,所有 JS 调用必须从 `iframe.content_frame` 入口。
> 在顶层 page 上访问 `window.mainview` 会得到 undefined。

## 入口层级

```
window
└── mainview                  ← GetMainView()
    ├── getViewports()        ← 返回 [Viewport]
    ├── getActiveViewport()
    └── ... (其它方法)

Viewport[0]                   ← 主视口(1×1 布局下唯一)
├── imageManager              ← 管理图像加载/缓存
│   ├── availableImagesIndex  ← 所有可用图像的索引数组(总帧数)
│   ├── loadedImages          ← 已加载图像缓存
│   └── ...
├── currFileIndex             ← 当前帧索引(0-based,只读)
├── setCurrFileIndex(i)       ← ⚠ 只更新状态,不渲染
├── pageTurnToCurrFileIndex('manual')  ← ✅ 触发真正的翻页 + 渲染
├── getImageInRealTime(i)     ← 异步取图
└── displayImage(...)         ← 内部绘制

canvas#0_0                    ← 1×1 布局下主画布
canvas#0_0 .. canvas#4_4      ← 5×5 布局下 25 个画布
```

## 关键 API

### `window.mainview.getViewports()`

返回所有 viewport 对象的数组。

```javascript
const v = window.mainview.getViewports()[0];
// 或
const active = window.mainview.getActiveViewport();
```

### `viewport.imageManager.availableImagesIndex`

**总帧数**的可靠来源(比解析 DOM 文本更准)。

```javascript
const total = viewport.imageManager.availableImagesIndex.length;
```

返回的索引数组不一定连续(可能有缺帧),但 `.length` 就是总帧数。

### `viewport.setCurrFileIndex(idx)`

**⚠ 只更新状态,canvas 不重绘。**

```javascript
// ❌ 错误:只调这一个,canvas 还是上一帧
viewport.setCurrFileIndex(5);
```

这个坑很容易踩:表面上 "帧号变了",但 canvas 还是上一帧的内容。

### `viewport.pageTurnToCurrFileIndex(mode)`

**✅ 真正触发翻页 + 渲染。** 内部走 `getImageInRealTime` → `displayImage` 管线。

```javascript
viewport.setCurrFileIndex(5);
viewport.pageTurnToCurrFileIndex('manual');  // ← 必须显式调用
```

`mode` 常见值:
- `'manual'` — 手动翻页,不走 HangingProtocol
- `'auto'` — 自动(协议触发)
- `'scroll'` — 滚轮触发

> **关键洞察**:HangingProtocol 来源会自动调 `pageTurnToCurrFileIndex`。
> 但我们手动翻页必须显式调用,否则翻页 "静默" 失败,canvas 内容不变。

### `viewport.currFileIndex`

当前帧号(0-based),**只读**。

```javascript
const cur = viewport.currFileIndex;  // 例: 5
```

### `viewport.getImageInRealTime(idx)`

异步取图(不渲染,只预取)。通常不需要手动调,`pageTurnToCurrFileIndex` 内部会调。

## Canvas 操作

### canvas id 命名

`{row}_{col}` 格式,5×5 布局下有 `0_0` ~ `4_4` 共 25 个 canvas:

| 布局 | canvas id 范围 |
|---|---|
| 1×1 | `0_0` |
| 2×2 | `0_0` ~ `1_1` |
| 3×3 | `0_0` ~ `2_2` |
| 4×4 | `0_0` ~ `3_3` |
| 5×5 | `0_0` ~ `4_4` |

**1×1 布局下主画布 id 固定是 `0_0`**。

### toDataURL 抓图

```javascript
const canvas = document.getElementById('0_0');
const b64 = canvas.toDataURL('image/jpeg', 0.92);
// 返回 "data:image/jpeg;base64,......"
```

**懒加载检测**:canvas 在未渲染时 `width === 0 && height === 0`,要先确认非零再截。

```javascript
if (canvas.width === 0 || canvas.height === 0) {
    // 还没渲染完,等待
}
```

### 兜底:querySelectorAll + 面积排序

不同 viewer / 不同布局下 id 不一定稳定。用面积排序选主画布:

```javascript
const cs = document.querySelectorAll('canvas');
let best = cs[0], bestArea = 0;
for (const c of cs) {
    const a = c.width * c.height;
    if (a > bestArea) { bestArea = a; best = c; }
}
// best 就是主画布
```

## 调试技巧

### 在 Playwright 里直接 inspect

```python
frame = page.locator("iframe").content_frame
info = frame.evaluate("""() => {
    const v = window.mainview.getViewports()[0];
    return {
        total: v.imageManager.availableImagesIndex.length,
        current: v.currFileIndex,
        type: typeof v.pageTurnToCurrFileIndex,
    };
}""")
```

### 监听 canvas 变化

在 DevTools Console 里看 canvas 的 width/height:

```javascript
// 5×5 切换前 25 个 canvas 都是 0×0
// 切到 1×1 后,canvas#0_0 才被渲染
setInterval(() => {
    const c = document.getElementById('0_0');
    console.log(c ? `${c.width}x${c.height}` : 'no canvas');
}, 1000);
```

## 与 uicloud Dapeng 的关系

zscloud 用的 viewer 和 uicloud 主站的 Dapeng viewer **API 几乎一致**(同一家厂商)。
但有以下差异:

| 项 | zscloud | uicloud Dapeng |
|---|---|---|
| `mainview` 全局 | ✅ | ✅ |
| `imageManager.availableImagesIndex` | ✅ | ✅ |
| 协议双击触发 | ✅ | ✅ |
| 布局切换 | 点按钮 | 点按钮 |
| WW/WL 设置 | 右下角输入 | 右下角输入 |
| 共享链接打开方式 | `?code=xxx` hash 路由 | URL path 直接进 viewer |

如果是从 uicloud 脚本改 zscloud,基本只需要改 URL 入口和点击坐标。
