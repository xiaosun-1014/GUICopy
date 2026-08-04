# 自适应渲染等待：drawImage 缩略图 hash 轮询 vs 替代方案

## 背景

每次翻页后，DICOM viewer 需要时间加载新帧、解码、渲染到 canvas。
如果等待时间太短 → 截到旧的帧；等待时间太长 → 总耗时线性增加。

目标是：**画面一变就截图，不等满固定时间**。

## 方案对比

| 方案 | 每帧耗时 | 依赖 | WebGL 兼容性 | 实现复杂度 |
|---|---|---|---|---|
| ❌ 固定 `wait_for_timeout(1500)` | 1500ms | 无 | ✅ | 1 行 |
| ❌ `drawImage` 8×8 缩略图 + getImageData | ~1ms | 无 | ❌ `getImageData` 返回 0 | 15 行 |
| ❌ **`toDataURL` 直调 WebGL canvas** | ~5ms/poll | 无 | ⚠️ 受 `preserveDrawingBuffer` 影响 | 10 行 |
| ✅ **`drawImage` 到 2D scratch canvas → `toDataURL`** | ~8ms/poll | 无 | ✅ 所有 WebGL 实现兼容 | 18 行 |
| `imagehash.phash` + Image.open | ~50ms/poll | PIL + imagehash | ✅ | 20 行 |
| `canvas.toBlob()` + size | ~10ms/poll | 无 | ✅ | 20 行（异步） |

**推荐方案：drawImage → 2D scratch canvas → `toDataURL`**。
与 `capture_frame_via_js` 的截图路径一致，绕过 preserveDrawingBuffer 限制，
适用于所有 DICOM viewer（uicloud / cornerstone / 自研引擎）。

## 为什么 `toDataURL` 直调不行

`HTMLCanvasElement.toDataURL()` 读取 WebGL canvas 的帧缓冲区时，
行为受 `WebGLContextAttributes.preserveDrawingBuffer` 影响：

- `preserveDrawingBuffer: true`（默认）→ `toDataURL` 正常返回当前帧
- **`preserveDrawingBuffer: false`**（大多数 viewer 为性能这样设）→
  `toDataURL` 返回的内容**可能是空帧或残留帧**，不同帧的 JPEG 长度可能不变

cornerstone 等 WebGL 渲染引擎为了性能，通常设为 `false`。

## drawImage 到 2D scratch canvas 为什么可行

```javascript
const scratch = document.createElement('canvas');
scratch.width = 80;  // 缩略图尺寸
scratch.height = 60;
const ctx = scratch.getContext('2d');  // 2D context，始终可用
ctx.drawImage(sourceCanvas, 0, 0, sourceW, sourceH, 0, 0, 80, 60);
return scratch.toDataURL('image/jpeg', 0.3).length;
```

流程：
1. 新建 2D canvas——浏览器保证任何时候都能拿到 2D context
2. `drawImage` 从 WebGL canvas 读取——浏览器合成后读取，不受 preserveDrawingBuffer 限制
3. `toDataURL` 在 2D canvas 上调用——始终可靠

## 性能

每轮 poll 约 **8ms**（80×60 缩略图 drawImage + JPEG 编码）。
轮询间隔 150ms，1-3 次轮询（150-450ms）即可检测到帧变化。

## timeout 取值

| 场景 | 建议 timeout | 说明 |
|---|---|---|
| uicloud Dapeng（局域网） | 1.5s | 图像加载快，平均 300ms 渲染 |
| uicloud Dapeng（公网） | 1.5s | 保守值 |
| cxhospital cornerstone | 1.5s | drawImage hash 正常工作 |
| **未知 viewer** | **1.5s** | 走 drawImage 路径兼容所有 viewer |

## 降级机制

`capture_all_frames` 在帧 1 截图后调用 `_detect_hash_usable(scope)`：
- 测 3 次 hash 是否 > 0
- hash 可用 → `wait_frame_ready` 用 drawImage 缩略图轮询
- hash 不可用（如跨域 iframe）→ 走 300ms 固定等待

## 替代方案：`canvas.toBlob()`

如果 `toDataURL()` 仍觉得慢，可用异步的 `toBlob()`：

```javascript
() => {
    return new Promise((resolve) => {
        const c = document.querySelector('canvas');
        if (!c) { resolve(0); return; }
        c.toBlob((blob) => resolve(blob ? blob.size : 0), 'image/jpeg', 0.2);
    });
}
```

但 `scope.evaluate()` 在 Playwright 中不支持返回 Promise，所以 `toBlob` 不可用。
只能用同步的 `toDataURL()`。
