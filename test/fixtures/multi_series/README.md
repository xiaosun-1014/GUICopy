# Multi-series anonymous fixtures

本地匿名化 fixture，供「多序列可点击 Replica」实施计划 Phase 5/6/7（多序列探索）测试使用。
所有值均为匿名测试值——无真实患者姓名、检查号或 UID。

## 文件清单

| 文件 | 用途 |
|------|------|
| `series_list.html` | 单文档可滚动序列列表（主要 fixture）。含 5 个序列：4 个初始可见 + 1 个滚动后才进入视口。含两个**同名序列**。 |
| `nested_host.html` | 两层 iframe 变体的顶层宿主文档。 |
| `nested_outer.html` | 两层 iframe 变体的中间 iframe，内嵌最内层序列列表。 |
| `nested_inner.html` | 两层 iframe 变体的最内层 iframe，持有与 `series_list.html` 中相同的 5 序列列表。 |
| `popup.html` | popup 变体：popup 文档直接持有可滚动序列列表 + viewer + metadata（无 iframe）。 |
| `README.md` | 本文件。 |

## 序列清单（三份 HTML 共用同一套序列编排）

| # | 标签（label 文本） | data-series-uid | data-viewer-key | 帧数（data-frame-count / 文本） | 可见性 |
|---|-------------------|-----------------|-----------------|-------------------------------|--------|
| 1 | Coronal MIP | `uid-1992` | `vw-coronal-a` | 362 / `1.0 362幅` | 初始可见 ✅ |
| 2 | **Coronal MIP（同名）** | `uid-2047` | `vw-coronal-b` | 180 / `3.0 180幅` | 初始可见 ✅ |
| 3 | Axial | `uid-3011` | `vw-axial` | 400 / `0.8 400幅` | 初始可见 ✅ |
| 4 | Thin 1.0 | `uid-4105` | `vw-thin` | 500 / `1.0 500幅` | 初始可见 ✅ |
| 5 | Sagittal | `uid-5209` | `vw-sagittal` | 120 / `2.0 120幅` | **滚动后出现**（下方折叠） |

### 关键点

- **同名序列**：#1 与 #2 都显示文本「Coronal MIP」（`data-series` 亦同为 "Coronal MIP"），
  但 `data-series-uid` 不同（`uid-1992` / `uid-2047`）。发现算法按 `_SERIES_IDENTITY_ATTRS`
  最高优先级稳定属性 `data-series-uid` 生成 `series_key`，因此两者应得到不同 key，
  用于验证「同名序列区分」。
- **滚动后出现**：#5（Sagittal）初始位于 `#series-list` 折叠区之外，需滚动后才进入视口。
  静态文件只需呈现「可滚动列表」——虚拟列表节点复用行为由测试脚本在运行时驱动
  （参见 `test/test_replica_regions.py::test_discovery_virtualized_list_deduplicates_reused_nodes`）。
- **独立 Viewer 标记**：每个序列有独有 `data-viewer-key` 与背景色（`data-bg`），
  点击序列后 `#viewer` 更新 `data-viewer-key` + 背景 + 文本——截图差异通过不同背景色/文本体现，
  无需真实图片。
- **Metadata 独有值**：每个序列带独有 SeriesNumber / SeriesDescription / SeriesInstanceUID（见下表）。

## Metadata 独有值表

| data-series-uid | SeriesNumber | SeriesDescription | SeriesInstanceUID |
|-----------------|--------------|-------------------|-------------------|
| `uid-1992` | 101 | Coronal MIP thin | `1.2.826.0.1.3680043.2001.1992` |
| `uid-2047` | 102 | Coronal MIP thick | `1.2.826.0.1.3680043.2001.2047` |
| `uid-3011` | 201 | Axial soft tissue | `1.2.826.0.1.3680043.2001.3011` |
| `uid-4105` | 301 | Thin bone | `1.2.826.0.1.3680043.2001.4105` |
| `uid-5209` | 401 | Sagittal reformat | `1.2.826.0.1.3680043.2001.5209` |

在 `series_list.html` / `popup.html` 中点击某序列会写入 `#meta-number` / `#meta-desc` / `#meta-uid`；
`nested_inner.html` 中 MV 值位于各 item 的 `data-series-number` 等属性上。

## 序列 item 结构约定

每个 item 满足 `capture_snapshot.py` 的发现算法识别要求（
`_SERIES_ITEM_SELECTOR = "option, [data-series], [role='option'], .series-item, li"`、
`_SERIES_IDENTITY_ATTRS`、`_SERIES_FRAME_ATTRS`）：

```html
<div class="item" role="option" data-series="Coronal MIP" data-series-uid="uid-1992"
     data-frame-count="362" data-viewer-key="vw-coronal-a" data-bg="#cfe8cf"
     data-series-number="101" data-series-description="Coronal MIP thin"
     data-series-instance-uid="1.2.826.0.1.3680043.2001.1992"
     aria-selected="true" onclick="selectSeries(this)">
  <span class="series-name">Coronal MIP</span><span class="series-frames">1.0 362幅</span>
</div>
```

- 稳定身份属性：`data-series-uid`（最高优先级 → `series_key`）、`data-series`、`id`。
- 显式帧数属性：`data-frame-count`；文本同时含「幅」以支持 `_series_frame_count_from_text` 推断。
- 发现时要把可滚动根容器传给 `discover_series_candidates`。在 `series_list.html` / `popup.html`
  中为 `#series-list`；在 `nested_inner.html` 中为 `#series`。

## 如何打开

静态文件通过 `page.goto(path.as_uri())` 或 `page.set_content(...)` 打开。两层 iframe 静态文件
（`file://`）在 Playwright 中是跨源、`window.frameElement` 为 null，因此基于真实 frame 路由的测试
应改用 `set_content` 同源方式构建两层 iframe（镜像
`test/test_batch_capture_replicate.py::test_nested_frame_series_uses_scroll_harvest` 的做法），
只用静态文件作为人类可读的结构参考。

## 冒烟验证

`_smoke.py` 用 Playwright chromium 打开 `series_list.html`，调用
`discover_series_candidates(page.locator("#series-list"), "msl")` 验证：
枚举到 ≥3 个 descriptor；两个同名「Coronal MIP」序列得到不同 `series_key`
（`uid-1992` ≠ `uid-2047`）；滚动后出现的 `uid-5209` 亦被枚举。

```bash
D:/Anaconda/envs/codegen-marker/python.exe test/fixtures/multi_series/_smoke.py
```
