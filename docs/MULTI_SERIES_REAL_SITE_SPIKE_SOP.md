# 真实站 Multi-Series Spike 操作清单（Phase 0.2 SOP）

> **配合文档**：计划 `docs/superpowers/plans/2026-08-14-multi-series-replica-expansion.md` Task 0.2；
> 设计契约 `docs/superpowers/specs/2026-08-14-multi-series-replica-expansion-design.md` §2 术语 / §3 录制模板 / §6 就绪条件 / §8 真实站未知项。
>
> **目的**：在写任何新代码前，用**有合法登录权限的真实 viewer**（uicloud / cxhospital / 其他）把设计 §8 的未知项实测校准，把结论写回 `skills/_shared/viewers.yaml`，并以 `out/{hospital}/multi_series_spike/` 留存匿名化证据。spike 不完成，多序列扩展在真实站的假设就只是「保守假设 + fallback」。

---

## 1. 本次 Spike 要回答的核心问题（设计 §8 未知项）

| # | 未知项 | 为什么重要（落在哪） |
|---|--------|----------------------|
| U1 | 序列激活是 **click 还是 dblclick** | `RecordingTemplate.series_action` 原样继承人工录制的动作；录制错了 kind，整条扩展链都错 |
| U2 | 切换序列是否**销毁/重建 iframe**（或虚拟列表节点复用） | 每轮探索必须重新发现 Page/Frame/locator，不能缓存；决定 `discover_series_candidates` 每步是否要重新解析 |
| U3 | Viewer 稳定证据可观察性 | 组合就绪要求「两类独立证据」；至少要确认 2 种证据真实可见（选中态 / 当前序列文本·帧数 / canvas hash / DOM 指纹 / 截图非黑） |
| U4 | **Metadata 面板级别**：Study / Series / image | 决定 identity 校验与 partial 语义：切换序列后 Metadata 面板内容**是否变化**、面板里有没有 SeriesInstanceUID |
| U5 | Metadata **open / close locator** 及 close 后面板是否真的消失、**序列列表是否恢复可操作** | expansion 触发点是 close 成功 else 分支；close 后面板残留 = 下一个 branch 在错误 UI 状态操作 |
| U6 | 序列列表的**滚动容器 / item selector / 稳定属性 / 节点复用**行为 | `discover_series_candidates` 的 scroll-harvest 与去重依赖这些；虚拟列表节点复用是同时给「去重」和「定位」上强度的场景 |

---

## 2. 前置准备

- [ ] 确认有真实站**合法登录账号**，且是**只读检查**（不触发任何开单 / 写操作 / 跨患者操作）。
- [ ] 打开目标 `out/{hospital}/`，本次 spike 证据统一放 `out/{hospital}/multi_series_spike/`（**不提交到 git**）。
- [ ] 选一个 **3–5 个序列**的检查；序列越典型越好：包含至少一个**同名序列**（若门诊有）+ 一个**较长、需滚动才见**的列表。
- [ ] 启动 GUI：`D:/Anaconda/envs/codegen-marker/python.exe main_gui.py`
- [ ] 打开 `skills/_shared/viewers.yaml`，把本次要测的 viewer 段落准备好待补。
- [ ] 环境自检：先跑一遍 `test/fixtures/multi_series/_smoke.py` 确认本机 Playwright 可用。

---

## 3. 录制「完整模板」的三条铁律（设计 §3，失败不做多序列）

> 多序列扩展是 opt-in（GUI「自动探索全部序列」默认关）。**只有打开它时**，模板完整性才是硬门。但 spike 必须按完整模板录，因为 U1/U3/U5 都要靠这份模板驱动。

- [ ] **R1 序列选择**：插入 **1 个「序列选择」marker**，位置在**序列条目点击行上方**。实际激活用 click 还是 dblclick——**只录真实站需要的动作**（这本身就是 U1 的答案）。
- [ ] **R2 Meta open + close 是两个不同动作**：插入「Meta 信息工具」marker，让 Meta 面板**打开点击**和**关闭点击**都各自落进 Meta 分组（一个 marker 包两行，或两个 marker 各包一行）。**只录一个 Meta 点击 = 模板不完整**（`metadata_close=None`）。
- [ ] **R3 录制顺序**：选序列 → 等 Viewer 出现 → 打开 Meta → 等内容稳定 → 关闭 Meta。（「等 Viewer / 等内容」**不要**插 marker，由就绪证据承担。）
- [ ] 停止录制 → 保存（同一按钮落盘 `processed_script_{医院}.py` + `replica_annotations.json`）。
- [ ] 勾选 GUI Phase 8「自动探索全部序列」（并留预算默认值），点导出跑 `capture-build`。
- [ ] **确认 preflight 通过**：不通过时，读 orchestrator 输出里的 `expansion_missing_series_select / expansion_missing_metadata_open / expansion_missing_metadata_close` 排查（注意 GUI 导出前只查语法，错误要到 preflight 阶段才暴露）。

---

## 4. 观察与采集清单（每项都要「看到」才算数）

> 每一行：**看什么 → 怎么看 → 判定 → fallback（无法确认用它）→ 写到哪里**。
> 只把**结论**回填表格，不要复制患者 DOM 大段进文档/配置。

### 4.1 序列激活方式（U1）

| 检查 | 做法 |
|---|---|
| 打开序列面板，观察是否单击即切换 | 若双击才切换 Viewer，则录制时保持双 |
| 确认后 | 结论写回 `viewers.yaml` 该 viewer 的注释 + 录制 SOP 提示；spike 结论文件第 1 行 |

**fallback**：无法从录制确定 → 按「点击原样执行」（设计 §8 既定决策，`RecordingTemplate.series_action` 继承）。

### 4.2 iframe / 容器结构（U2）

| 检查 | 做法 |
|---|---|
| 序列列表在**哪个 frame**（top / iframe / 嵌套 iframe） | DevTools → 检查序列 item 的 `window.frameElement` 深度；对照 `viewers.yaml.iframe_selectors` 当前值 |
| 切换序列后该 iframe **是否被销毁重建** | 切前后各记一次 frame 元素引用 / DOM 树是否整体替换 |
| 若重建 | 结论：每轮探索必须重新发现 Page/Frame/locator（当前实现已按此假设，需验证假设为真） |

**fallback**：无法判断 → 保守假设会重建（每轮重新发现，设计 §8）。

### 4.3 Viewer 稳定证据可观察性（U3，至少确认 2 类）

| 证据类别 | 怎么在真实站看到 |
|---|---|
| 选中态 | 目标 item `aria-selected` / active / current class 是否变化 |
| 当前序列文本 / 帧数 | Viewer 里是否有当前序列名 / 「xx 幅」指示文本变化 |
| canvas hash | 画布内容是否随序列变化（截图前后对比） |
| DOM 指纹 | Viewer 容器 DOM 在两次采样间稳定、且跨序列有差异 |
| 截图非黑 | 截图非空 / 非全黑 |

> 判定：**≥2 类可稳定观察到** → 组合就绪条件在真实站成立；只观察到 1 类 → 标记风险，需在 `viewers.yaml` 备注并考虑降级。

### 4.4 Metadata 面板级别（U4）

| 检查 | 做法 |
|---|---|
| 切一个**不同序列**后，Meta 面板里 SeriesNumber / SeriesDescription / SeriesInstanceUID **是否变化** | 打开 Meta → 记录 → 关 → 切下一个序列 → 再开，比对 3 个字段 |
| 面板是**本次切换的那个序列**（Series 级）还是**整个检查共用**（Study 级） | 变化了 = Series/image 级；没变 = Study 级 |
| 面板有 SeriesInstanceUID 吗 | 有 → identity 校验走 UID-hash；没有 / image-level → 降级为 SeriesNumber + SeriesDescription 一致即接受并记 warning（设计 §8） |
| 两个同名序列的 Meta **身份是否可区分** | 同名 ≠ 同身份；spike 用一张同名序列截图存证据 |

### 4.5 Metadata open / close 与 hub 恢复（U5）

| 检查 | 做法 |
|---|---|
| open locator 稳定吗 | 记下可复用的按钮文本 / 结构选择器，写 `viewers.yaml.meta_panel.open_button_names` |
| close 之后**面板确实不可见**吗 | 关后截图；面板残留 → 记下正确 close locator（`close_button_selectors`） |
| close 后**序列列表还能点吗** | 立即点另一个序列验证 hub 可操作（per-branch finally 恢复的前提） |

### 4.6 序列列表滚动 / 去重（U6）

| 检查 | 做法 |
|---|---|
| 滚动容器是哪个 | 可滚动元素 = 容器（`#series-list` 之类） |
| item selector / 稳定属性 | 每个 item 有哪些稳定属性（`data-series-uid` / `data-series` / `id` / role / class） |
| 同名序列 | 两个同名 item 的**稳定属性是否不同**（这是「同名可区分」的唯一可靠依据） |
| 虚拟列表节点复用 | 滚动到底再滚回，同一位置 DOM 节点是否被复用 → 决定去重靠什么 |

**fallback**：`data-series-uid` / `data-series` / `id` 都不存在、且出现同名 → 这是当前已知弱身份软肋（`_matches_descriptor` 先合并同 label），spike 必须显式记录「此 viewer 存在同名无稳定属性的序列」，扩展采集前需人工处理。

---

## 5. viewers.yaml 回写指引

> 规则：**只写选择器与行为结论，绝不写患者姓名、检查号、UID、token**。改动 `skills/_shared/viewers.yaml`（`_shared` 是共享的，别只改 `.reasonix` 旧副本）。

| 字段（uicloud 示例值） | spike 后确认/修正 |
|---|---|
| `iframe_selectors`（uicloud: `["[id=\"2d-iframe\"]"]`） | 是否仍是实际 iframe 容器（U2） |
| `meta_panel.open_button_names`（uicloud: `DICOM信息 F2` / `DICOM信息`） | 真实站能点开的按钮名（U5） |
| `meta_panel.close_button_selectors`（当前 `[]`） | **spike 重点**：真实 close 选择器（U5） |
| `meta_panel.tag_row_format` / `tag_pattern` | 面板 DOM 结构是否匹配（U4） |
| `sequence_select.item_container_selectors`（uicloud: `.series-item` 等） | 真实 item 容器（U6） |
| `sequence_select.text_pattern` | 帧数文本匹配正则是否命中（U3/U6） |
| `sequence_select.canvas_selectors` | 画布选择器（U3 的 canvas hash 证据用） |
| （新增字段，若整体结构不匹配） | 按「新增 viewer 模板」流程补一份，先本地 fixture 验证再回填 |

**改法**：spike 当天结论 → **先在 `test/fixtures/multi_series/` 造一个真实结构的高保真匿名 fixture**，产品代码经 fixture 全绿后再把选择器固化进 `viewers.yaml`。不要把真实 DOM 原文贴进 yaml。

---

## 6. 证据保存规范（`out/{hospital}/multi_series_spike/`）

```
out/{hospital}/multi_series_spike/
  findings.md            ← 第 4 节每行的结论汇总（匿名）
  structure/
    series_list_topology.png     ← 序列列表 DOM/选中态截图（去敏感后）
    metadata_panel_{series}.png  ← 每个采集序列的 Meta 面板截图（去敏感后）
    after_close.png              ← close 后面板状态截图
  multi_series_contract.json     ← 匿名化的 U1–U6 结论（不含患者字段）
```

- [ ] 截图一律 `.jpeg`（本机 `.png` 会被加密改写，Read 报错）。
- [ ] 图片/JSON 在保存前手动清除患者姓名、检查号、UID、token 痕迹。
- [ ] 该目录已有 `.gitignore` 覆盖（`out/` 整体 + `/out/**/series_branches/`，见根 `.gitignore` 35–40 行），**绝不提交**。

---

## 7. Spike 完成定义（全绿才算完成）

- [ ] 设计 §8 的 U1–U6 每项都有**实测结论**（不是「没看到」）。
- [ ] 至少录制并通过一个**完整模板**（R1+R2+R3），preflight 无 `expansion_missing_*`。
- [ ] 认证 ≥2 类 Viewer 稳定证据在真实站可观察。
- [ ] `viewers.yaml` 已按 Spike 结论更新（含 `close_button_selectors`），且**不含任何患者文本**。
- [ ] 同名序列的 Identity 可区分性有明确结论（可区分 / 需人工 / 需改弱身份算法）。
- [ ] `out/{hospital}/multi_series_spike/` 证据齐全且匿名。

**未达成的项**：不要假装完成——每一项保持「未确认 + fallback」，并在 `findings.md` 里写明 fallback 是什么。

---

## 8. 安全 / 反模式红线（违反即重做）

- 不在真实站上做**跨患者 / 跨检查 / 批量写**操作；只读导航。
- 不在日志、配置、yaml、提交里出现患者姓名、检查号、原始 SeriesInstanceUID、token。
- 不把真实站 DOM 大段复制进配置文件；只写「选择器 + 行为结论」。
- 不用固定 sleep 当唯一就绪条件（spike 记录证据类型是为了让组合就绪有据可依）。
- 不改产品代码完成本次 spike；spike 结论先经匿名 fixture 验证再下沉。
- 验证 `git status --short` 不出现 `out/` 下任何产物。
