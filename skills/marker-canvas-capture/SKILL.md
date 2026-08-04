---
name: marker-canvas-capture
description: >
  处理 Playwright 录制脚本中的「影像画布交互」marker。
  完整管线：定位 viewer 画布 → 确定请求帧数 → 逐索引翻页并保存 JPEG →
  写入本次运行的 capture_manifest.json。每个请求索引都必须有对应输出。
  无 PIL / numpy / imagehash 依赖。
triggers:
  - 影像画布交互
  - 画布截图
  - 帧翻页
---

> **前置 skill**：必须先执行 `marker-sequence-select` 完成序列选择，画布交互才在正确的序列上操作。

# 影像画布交互 Marker 处理

## 契约

`capture_canvas_interaction` 接收请求帧数 `N`，按 `1..N` 逐一处理并返回 N 个结果。
每个请求索引无条件落盘：即使该索引的导航没有被渲染状态确认，也必须继续截图并保存对应文件；
导航方法、确认状态和警告写入 manifest，不能改变输出数量。

本次运行的输出目录固定为：

```text
SCRIPT_DIR / "canvas_frames" / YYYYMMDD_HHMMSS_ffffff/
```

其中 `YYYYMMDD_HHMMSS_ffffff` 是本次运行新建的独立子目录。目录内必须有：

```text
canvas_frame_0001.jpeg..N
capture_manifest.json
```

帧文件使用四位、从 0001 开始的连续编号；不要使用旧目录中的文件，也不要把多个运行写入同一子目录。
不得按文件大小或渲染 hash 删除任何帧。渲染 hash 只能用于等待或记录导航是否确认，不能决定是否保存请求索引。

## 何时使用

- 录制脚本中存在 `# [MARKER: 影像画布交互]` 或其 TODO 占位行。
- 需要对 DICOM viewer 的 2D 影像序列进行全量帧截图。
- 截图用于后续 VL 模型分析、病灶检测、质量校验或人工复核。
- 适用于 popup viewer、单层 iframe、嵌套 iframe 以及没有 iframe 的 viewer。

## Marker replacement

将 `# [MARKER: 影像画布交互]` 及紧随其后的 TODO 和原始画布截图动作整体替换为下面的调用。
保留录制得到的坐标；viewer 页面变量通常是 popup 的 `page1`，不是报告页的 `page`。

```python
# [MARKER: 影像画布交互]
from pathlib import Path as _CanvasPath
import sys as _canvas_sys

_PROJECT = _CanvasPath(__file__).resolve().parents[2]
if str(_PROJECT) not in _canvas_sys.path:
    _canvas_sys.path.insert(0, str(_PROJECT))
from skills._shared.canvas_capture import capture_canvas_interaction

SCRIPT_DIR = _CanvasPath(__file__).resolve().parent
page1.wait_for_timeout(1500)
frame_paths = capture_canvas_interaction(
    page1,
    click_x=562,
    click_y=526,
    total_frames=locals().get("seq_frames"),
    series_name=locals().get("seq_name"),
    output_root=SCRIPT_DIR / "canvas_frames",
)
print(f"[画布] 已保存 {len(frame_paths)} 帧")
```

生成器应根据录制脚本使用的页面变量和坐标替换示例中的 `page1`、`click_x`、`click_y`；
不得恢复 marker 下原始的单次 `canvas.click()` 或 `viewer_cx.png` 截图动作。

## 架构

```text
capture_canvas_interaction(viewer_page, click_x, click_y, total_frames, output_root)
│
├─ 1. 定位含 canvas 的 viewer frame
├─ 2. 确定请求帧数：显式 total_frames > viewer 文本解析
├─ 3. 创建一次性运行目录 output_root/YYYYMMDD_HHMMSS_ffffff/
├─ 4. 激活录制坐标对应的画布
├─ 5. 对 frame_index = 1..N 逐次执行
│  ├─ 1：保留当前帧；2..N：按降级策略导航
│  ├─ 等待渲染或记录未确认状态
│  ├─ 写入 canvas_frame_{frame_index:04d}.jpeg
│  └─ 追加 manifest.frames，不跳过、不合并索引
├─ 6. 校验恰好 N 个连续编号文件
└─ 7. 写入 capture_manifest.json 并返回 N 个结果
```

导航降级顺序：

1. viewer 原生 JS API（`setImageIndex`、`gotoFrame`、Cornerstone `scrollToIndex` 等）。
2. 先聚焦最大可见 canvas，再发送 `ArrowDown`。
3. 将鼠标移到最大可见 canvas 上后滚轮翻页。
4. 可用时操作可见 slider。

导航失败或渲染状态未确认时，仍然执行当前索引的截图和落盘；manifest 的
`navigation_method`、`change_confirmed`、`warning` 只描述状态。

## 输出与 manifest

`output_root` 必须传 `SCRIPT_DIR / "canvas_frames"`。共享实现会为每次调用创建新的
`YYYYMMDD_HHMMSS_ffffff` 子目录，并写入 JPEG quality 95。单次运行的 manifest 至少包含：

```json
{
  "series_name": "可选序列名",
  "requested_frame_count": 3,
  "saved_frame_count": 3,
  "frames": [
    {
      "frame_index": 1,
      "filename": "canvas_frame_0001.jpeg",
      "capture_method": "canvas_js",
      "navigation_method": "initial",
      "change_confirmed": true,
      "file_size": 12345,
      "warning": ""
    }
  ]
}
```

`requested_frame_count`、`saved_frame_count` 和 `frames` 数量必须都等于 N；文件名必须恰好覆盖
`canvas_frame_0001.jpeg` 到 `canvas_frame_{N:04d}.jpeg`。每个 manifest 行对应一个请求索引，
包括导航未确认的索引。

## Viewer 与 canvas 适配

### 页面对象

- popup viewer：使用 `page1`，所有 frame、canvas、键盘和鼠标操作都作用于它。
- 主页面嵌套 viewer：使用包含 canvas 的 Frame；不要用 `page.evaluate()` 访问 iframe 的
  `contentDocument`。
- 需要主页面坐标时，累加每层 iframe 的 `bounding_box` 偏移；Frame 内坐标不能直接当作页面坐标。

### canvas 选择

不要依赖固定 ID。通过 `querySelectorAll('canvas')` 找可见 canvas，并按显示面积选择主画布。
截图可按以下顺序降级：

1. 在 Frame 上执行 JS，将 canvas 绘制到 2D scratch canvas 后导出 JPEG。
2. 对最大可见 canvas 调用 Playwright `screenshot(type="jpeg", quality=95)`。
3. 使用 viewer page 的全页 JPEG 截图作为最后兜底。

### 帧数

优先使用 `select_series` 返回的 `seq_frames`。没有显式值时，从 viewer 文本解析
`1/N`、`共 N 张`、`N frames` 等格式；解析不到正数时应明确报错，不得静默生成未知数量的输出。

## 常见坑与处理

| 现象 | 原因 | 处理 |
|---|---|---|
| 只生成少于 N 个文件 | 把导航确认当成保存前提 | 导航状态只写 manifest；每个 1..N 索引都必须截图并落盘 |
| 多次运行互相覆盖 | 复用了固定 `canvas_frames` 目录 | 每次调用创建 `YYYYMMDD_HHMMSS_ffffff` 子目录 |
| 文件名不连续或带时间戳 | 用捕获时间命名单帧文件 | 使用 `canvas_frame_{index:04d}.jpeg`，时间戳只用于运行目录 |
| iframe 内找不到 canvas | 在主页面用 `contentDocument` 扫描 | 从 `page.frames` 获取 Frame，再用 `frame.evaluate()` 或 Frame locator |
| 所有帧看起来没有变化 | 键盘事件没有路由到 viewer canvas | 先聚焦最大可见 canvas；再按 JS API、键盘、滚轮、slider 顺序尝试 |
| WebGL 画布导出为空 | 直接从 WebGL buffer 导出 | 先 `drawImage` 到 2D scratch canvas，再导出 JPEG |
| 渲染状态无法确认 | viewer 不暴露稳定的画面指纹 | 记录 `change_confirmed=false` 和 warning，仍保存该索引 |
| 运行结束后无法审计 | 只有散落的 JPEG 文件 | 在运行目录写 `capture_manifest.json`，记录每一帧及其方法和状态 |

## 依赖与辅助函数

推荐直接导入共享实现，不在生成脚本中复制大段 viewer 适配代码：

```python
from skills._shared.canvas_capture import capture_canvas_interaction
```

共享实现的关键职责如下：

| 函数 | 职责 |
|---|---|
| `_find_viewer_frame(page)` | 找到主页面或子 frame 中的 canvas |
| `_parse_total_frames(scope)` | 从 viewer 文本解析 N |
| `_navigate_to_frame(page, target, total_frames)` | 逐级尝试导航并返回状态 |
| `_canvas_hash(scope)` | 可选的渲染变化等待/状态记录 |
| `_capture_frame(frame, path)` | JS、canvas locator、page 截图三级捕获 |
| `capture_canvas_interaction(...)` | 创建运行目录、执行 1..N、写 manifest |

hash 或文件大小只能作为诊断、等待和 manifest 元数据，不能减少请求索引对应的输出。

## 最小接入示例

```python
from pathlib import Path
from skills._shared.canvas_capture import capture_canvas_interaction

SCRIPT_DIR = Path(__file__).resolve().parent
results = capture_canvas_interaction(
    page1,
    click_x=562,
    click_y=526,
    total_frames=seq_frames,
    series_name=seq_name,
    output_root=SCRIPT_DIR / "canvas_frames",
)
assert len(results) == seq_frames
assert results[0].path.parent.name  # YYYYMMDD_HHMMSS_ffffff
```

运行后，`results` 与运行目录中的 `canvas_frame_0001.jpeg..N` 一一对应，
同目录的 `capture_manifest.json` 是本次运行的完整记录。
