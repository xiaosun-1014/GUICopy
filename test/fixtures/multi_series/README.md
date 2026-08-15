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
| `ft_series_list.html` | 单文档 FTImage 风格序列列表镜像（真实站点 Spike 固化）。`div.os-viewport` 滚动容器内含 8 个 `a > div.desc > span.total` 序列行；**无任何 id/data-\*** 身份属性；1 行初始在折叠区外（滚动发现）；页面还有第 2 个不含序列行的干扰 `div.os-viewport`（取 `.first` + 容器限定验证）。 |
| `zs_series_list.html` | 单文档中山 zscloud 风格序列列表镜像（真实站点 Spike 固化）。`div.StudyList#HLeftThumnail` 滚动容器内含 4 个 `li.ui-draggable[id]`（id 为**虚构** UID 形态，`1.2.826.0.1.3680043.201.1001`…`.1004`）；第 2 行 class 含 `select`（选中态）；容器外另放 2 个 `li.ui-draggable`（模拟病人头/检查 LI，验证容器限定后不计入）。 |
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

## 真实站点镜像 fixture（ft / zscloud）

这两个 fixture 是「真实站多序列发现适配」（`docs/TASK_HANDOFF_FT_ZSCLOUD_SERIES_ADAPT.md`，
真实站点只读探测 §2.1）的结构固化，全部数值为**虚构**匿名数据（UID 一律在
`1.2.826.0.1.3680043.201.*` 段），不含任何真实病人文本/token/检查号。

### ft_series_list.html（FTImage 风格）

- 滚动容器 = 文档中 **第一个** `div.os-viewport`（overlay scrollbars 视口，页里共 2 个，
  `.first` 即真实序列容器；第 2 个是干扰容器，用于验证容器限定/取首个可见）。
- 序列行 = `a:has(span.total)`（结构 `a > div.desc > span.total`），**无 id / data-\*** 身份属性
  → `_series_identity` 走文本 fallback（`key` 形如 `ft::scout /共 2张::x0`）。
- 第 8 行 `3.0 x 3.0 MPR-Sag_bone`（共 131张）初始在折叠区外，滚动后枚举到（`reached_end=True`）。
- 发现调用：
  ```python
  discover_series_candidates(root, "ft",
      item_selector="a:has(span.total)", identity_attrs=[])
  # root = page.locator("[class*=os-viewport]").first
  # 预期：8 个 descriptor、key 唯一、reached_end；干扰容器 nth(1) → 0 个
  ```
- 默认（不传参数）选择器 `_SERIES_ITEM_SELECTOR` 不含 `a:has(...)` → 对 ft fixture 匹配 **0 个**
  （证明必须显式传 `item_selector`，改默认行为的回归不会误通过）。

### zs_series_list.html（中山 zscloud 风格）

- 滚动容器 = `div.StudyList#HLeftThumnail`，~3 行可见、第 4 行滚动后进入视口。
- 序列行 = `li.ui-draggable`（结构 `li > div.thumnailClass > a > i.countClass`），
  每行 `id` 为虚构 SeriesInstanceUID 形态 `1.2.826.0.1.3680043.201.1001`…`.1004` → 序列 key 直接取 id。
- 第 2 行 class 额外含 `select`（选中态结构镜像）。注意：当前 `_series_selected` 只按
  **属性**（`aria-selected` / `selected` / `data-selected`）判定选中，**不看 class 令牌**，
  故本 fixture 的 `selected` 字段恒为 `False`——fixture 保留 class 是为了结构忠实，不是断言点。
- 文档里共 6 个 `li.ui-draggable`：容器内 4 个 + 病人头/检查 LI 2 个（`.9001` / `.9002`，
  无「N幅」文本）。根限定为 `#HLeftThumnail` 时外部 LI 不计入。
- 发现调用：
  ```python
  discover_series_candidates(root, "zs",
      item_selector="li.ui-draggable", identity_attrs=["id"])
  # root = page.locator("#HLeftThumnail")
  # 预期：4 个 descriptor、key = 4 个虚构 UID 且唯一、外部 li（.9001/.9002）不计入
  ```
- 身份安全：内部 `series_key` 用虚构 UID 可以；公开面（serve/事件/日志/报告）仍须走
  `series_key_slug()`/hash，`title`（病人姓名）绝不可进入任何 identity/输出。

## 冒烟验证

`_smoke.py` 用 Playwright chromium 打开 `series_list.html`，调用
`discover_series_candidates(page.locator("#series-list"), "msl")` 验证：
枚举到 ≥3 个 descriptor；两个同名「Coronal MIP」序列得到不同 `series_key`
（`uid-1992` ≠ `uid-2047`）；滚动后出现的 `uid-5209` 亦被枚举。

```bash
D:/Anaconda/envs/codegen-marker/python.exe test/fixtures/multi_series/_smoke.py
```
