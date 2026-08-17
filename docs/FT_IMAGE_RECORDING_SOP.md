# FTImage 多序列录制与复刻 SOP（2026-08-16）

> 目标：录制一份 FT（yyx.ftimage.cn）检查，产出**离线可点击复刻**——发现全部 8 个序列、
> 逐序列各自 Metadata、分支上「更多→Tags→面板」两步打开、序列列表整块可滚动（含完整面板背景）。
>
> 适用代码：包含 `049fd46`（完整滚动面板长图）及之前 `b5a882d`/`6ce8cf2` 三个提交的工作区。
>
> 关联：`docs/MULTI_SERIES_REAL_SITE_SPIKE_SOP.md`（探测 SOP）、`docs/FIX_FT_MULTI_SERIES_CLICK_AND_DISCOVERY_2026-08-16.md`（缺陷修复与验收）。

---

## 0. 一句话流程

```
录一个序列（双击打开） → 开 Meta → 关 Meta → 插 3 个 marker → 保存 → 勾「自动探索全部序列」→ 导出 capture-build → 看 series_capture_manifest → 开副本验收
```

---

## 1. 前置条件

- [ ] conda 环境就绪：`D:/Anaconda/envs/codegen-marker/python.exe`（Python 3.11 + PyQt6 + playwright 1.60 + chromium）。
- [ ] FT 登录会话可用：FT 用 URL 里的 `stm=...` token 免密，复制完整 URL 即可（`--auth-mode scripted` 直接回放）。
- [ ] 选一个**典型检查**：
  - 序列 ≥ 2（建议含 8 个那种，含 1 个同名/滚动后才可见的长序列如 MPR-Sag）；
  - 只读检查（不触发任何开单/写/跨患者操作）。
- [ ] viewer 配置在位：`skills/_shared/viewers.yaml` 的 `ftimage` 段（**不是** `.reasonix/viewers.yaml`）：
  - `item_container_selector: "div.os-viewport"`（页里有 2 个，取首个可见）
  - `item_selector: "a:has(span.total)"`
  - `identity_attrs: []`（无稳定属性 → 文本 fallback）
  - `meta_panel.open_button_names: ["更多","Tags"]`、`close_button_selectors: ["#tagsBox a.close"]`

---

## 2. 启动与录制（浏览器里逐步）

### 2.1 启动 GUI

```
D:/Anaconda/envs/codegen-marker/python.exe main_gui.py
```
（不要用系统 `python`，是 3.7 缺依赖。）

### 2.2 填 URL 并开始

1. URL 框填入 FT 检查完整 URL（含 `stm=...`）。
2. 点「**▶ 启动录制**」→ 浏览器弹出（codegen 录制态）。
3. 在浏览器里完成进入检查的动作（token 通常直接进列表页；如需点「进入」等，正常录制即可）。

### 2.3 录「完整模板」的三步（R1/R2/R3 铁律）

| 步 | 操作 | 说明 |
|---|---|---|
| **R1 打开一个序列** | 在序列列表页，**双击**其中一个序列行（FT 打开序列实际是双击；以你实测生效为准），等 Viewer 出现 | 这就是被管线自动扩展到其余所有序列的「激活动作」，双击或单击**按真实站动作为准** |
| R2 **开 Meta** | 在 Viewer 里点「**更多**」→ 点「**Tags**」→ 等面板渲染完成 | 两步都要录进去 |
| R2 **关 Meta** | 点 Tags 面板右上角关闭（`#tagsBox a.close`） | 打开和关闭是**两个动作**，都要录 |

**注意（本次修复相关）**：
- 准备双击的那一行，**等它的下载进度数字停稳**再双击（录出干净的点击）。
- 其它行的下载进度（`共 131张 106/109`）**不用等**——`normalize_series_text` 已把动态尾数剔除出身份，发现就是稳定的 8 个。
- 录制或采集期间**不要在进度跳动时手工点击/滚动序列列表**。

### 2.4 停止录制

回到 GUI 面板点「**停止录制**」，面板进入自由编辑态。

---

## 3. 插 marker（GUI 面板右键）

每个 marker 都是注释行。**右键 → 插入标记**，插在对应代码行的**上方**：

| marker | 位置 | 铁律 |
|---|---|---|
| 「🔲 序列选择」 | 在「双击序列行」那行代码上方 | R1：必须有 |
| 「📋 Meta 信息工具」 | 在「点更多/Tags」代码上方 | R2：必须覆盖**打开+关闭**两段——**一个 marker 包两行即可**（打开、关闭都落在它后面、下一个 marker 之前），不必插两个 |
| 「📋 Meta 信息工具」（第二个，可选） | 若想分开，在「关闭 Tags」上方再插一个，把关闭单独包住 | 都是合法；关键是关闭点击要落在某个 Meta marker 区间内 |

目标形态（示例）：

```python
page.goto("https://yyx.ftimage.cn/dimage/index.html?stm=…")
# [MARKER: 序列选择]
# TODO: …
page.get_by_role("link", name="x 10.0_lung 共 41张").dblclick()
# [MARKER: Meta 信息工具 @ 20260816_004942]
page.get_by_title("更多").click()
page.get_by_role("link").filter(has_text="Tags").click()
page.locator("#tagsBox a.close").click()
```

> preflight 缺失时：
> - `expansion_missing_series_select` → 没插「序列选择」；
> - `expansion_missing_metadata_open` / `expansion_missing_metadata_close` → Meta 打开/关闭没都落在 marker 区间（开、关必须在同一或两个 Meta marker 内，且**关闭要是区间里 Meta 动作的最后一个**）。

---

## 4. 保存

点「**💾 保存处理后代码**」→ 落盘 `out/ftimage/processed_script_ftimage.py`。
GUI 同时生成 `replica_annotations.json`（marker→源码行映射）。**不保存无法导出**。

---

## 5. 导出 capture-build（开启自动探索）

### 5.1 GUI 方式

1. 勾选「**自动探索全部序列**」（默认关，必须勾）。
2. 预算用默认即可：最大序列数 40 / 单序列超时 20s / 总超时 900s / capture 模式 `first_stable_frame`。
3. 点「**⚙️ 生成 Adapter + 离线复刻**」：
   - 登录方式：**scripted**（URL 带 stm token）；
   - 运行方式：**只复刻（跳过 Adapter）**（不烧 LLM）。
4. 等面板事件回显到 `completed`。

### 5.2 CLI 方式（等价）

```powershell
PYTHONIOENCODING=utf-8 D:/Anaconda/envs/codegen-marker/python.exe pipeline_orchestrator.py ^
  --script out/ftimage/processed_script_ftimage.py ^
  --annotations out/ftimage/replica_annotations.json ^
  --hospital ftimage --output-root out --operation capture-build ^
  --auth-mode scripted ^
  --expand-all-series --max-series 40 --per-series-timeout 20 --total-series-timeout 900
```

> 新版代码在「序列选择」快照时会对序列容器做 **scroll-stitch 长图**，写入
> `capture/snapshots/.../assets/series_list_full_*.jpeg`。**只有这份新录制的 run 才有**，
> 复刻端才会走「整块滚动完整面板」（§7）。

---

## 6. 验收：采集数字

跑完看 `out/ftimage/runs/{run_id}/capture/series_branches/series_capture_manifest.json`：

| 指标 | 达标值 |
|---|---:|
| `discovered_count` | **8**（真实序列数，不是 9） |
| `captured_count + partial_count` | **8** |
| `failed_count` / `skipped_count` | **0 / 0** |
| `count_conserved` / `reached_end` | **true / true** |
| `overall_ok` | **true** |

另查 `pipeline_report.json`（`status: success`、`series_coverage.status: complete`）与
逐分支 `metadata/metadata_rows.json`（8 个分支 Series Description 互不相同）。

---

## 7. 验收：离线副本交互

```
D:/Anaconda/envs/codegen-marker/python.exe out/ftimage/runs/{run_id}/replica/serve_replica.py
```
浏览器打开它输出的 URL（窗口调到 1696×880 / F11 保持 1:1）。

| 检查点 | 期望 |
|---|---|
| 序列列表 | 8 行；**滚动只发生在序列面板内**（`.series-scroll`，报告固定不动），滚动移动的是**列表行本身**（自渲染 DOM，无移动背景图），**滚动到底**可见 MPR-Cor / **MPR-Sag** 并可点 |
| 点第 1/2/最后一个可见序列 | 各进不同 Viewer（如 MPR-Sag → `bviewer_b007...`） |
| 分支开 Meta | 点「更多」→ 先进 Tags 菜单中间态（**真实一行工具按钮**，Tags 在行尾）→ 点「Tags」→ 才打开该序列 Meta（**两步**，与原站一致） |
| Meta 内容 | 是当前序列自己的（带 SeriesNumber / 唯一值） |
| 关 Meta | 「× 关闭」→ 回到**同一个**分支 Viewer，不是别的分支 |
| 跨分支 | Viewer 里也可直接点其它序列跳转；`aria-selected` 随当前序列变化 |
| 控制台 | F12 无 JS 异常 / route 缺失 |

> 若这次录的 run 是**旧代码之前录的**（无长图字段），列表滚动会是「回退模式」（能点到底下序列、但滚动时面板图不动）——重新录制才触发完整面板。

---

## 8. 常见问题

| 现象 | 排查 |
|---|---|
| `discovered_count=0` | URL 没匹配 `yyx.ftimage.cn` → 配置落空 `{}`；用默认选择器认不出 `a:has(span.total)`。看 `pipeline_events.jsonl` 的 `series_discovered` |
| 发现 9 个（旧 run 症状） | 旧代码没有 `normalize_series_text`；**用新代码重录**，动态进度不再复制身份 |
| Meta「更多」直接出面板 | 旧副本；用 `049fd46` 之后的工作区重建（`--mode offline-build`）或重录 |
| 列表滚动图不动 | 旧 run 无长图 → 回退模式；重录后完整面板 |
| preflight `expansion_missing_*` | 见 §3 铁律 |
| 单跑测试挂起 | 不要 `unittest discover`，用 `-m unittest test.xxx` 逐个 |
| `git status` 出现 `out/` 产物 | 正常（已 gitignore），提交前确认无 `out/` 进库 |

---

## 9. 安全红线（必须遵守）

- 不在仓库文档/git 出现：真实 token（stm）、病人姓名、检查号、原始 SeriesInstanceUID。
- 真实验证产物只留 `out/`（gitignore 已覆盖），绝不提交。
- 图片/JSON 存证前清除患者身份痕迹；截图一律 `.jpeg`（本机 `.png` 会被加密改写）。
- 不在真实站做跨患者/跨检查/批量写操作。

---

## 10. 一键自查清单

```
[ ] 环境/登录/典型检查就绪（§1）
[ ] 录：双击打开一序列 + 开 Meta(更多→Tags) + 关 Meta（§2.3）
[ ] 插 marker：序列选择 + Meta（开+关同区即可）（§3）
[ ] 保存 processed + annotations（§4）
[ ] 勾「自动探索全部序列」→ 导出 capture-build（§5）
[ ] manifest：8 / 8 / 0 / 0 / true / true（§6）
[ ] 副本：完整滚动 + 两步 Meta + 关回同分支 + 无报错（§7）
```
