# Web 复刻操作手册：如何用「人工标注」搭建复刻界面 + 优化盘点

> 范围：本仓库（Playwright Codegen 智能标记 + 离线复刻工具）。
> 聚焦：**怎样通过人工标注一步步构建一个可断网交互的复刻界面**，以及**当前复刻操作可优化环节的盘点与建议**。
> 读者：一线操作者（SOP，可照做）+ 后续优化复刻操作的开发者。
> 关联：`docs/PIPELINE_RUNBOOK.md`（一键管道操作）、`CLAUDE.md`（项目约定）。

---

## 0. 一句话结论

**「复刻界面」不是凭空生成的，而是由「你在录制脚本里做的标注」层层驱动出来的：**
业务 **Marker 圈定关键动作并触发状态捕获 → 每个关键动作前后的真实 DOM 快照（截图 + 透明可点击 overlay）构成复刻界面 → 自动解析的 locator 供离线回放定位**。
人工标注 = **① 业务 Marker（圈关键节点） + ② 细粒度选择器/拓扑精修（让复刻交互更准、更稳）**，两者结合才是完整方法。

> ⚠️ 重要事实（决定你怎么优化）：当前系统**没有**「专门的人工标注面板」。locator 是**从录制脚本自动解析**的。
> 你人工介入的**唯一入口就是编辑 processed 脚本里的 Playwright 选择器文本**。这既是当前方法的边界，也是后续最值得优化的点（见 §5）。

---

## 1. 两种人工标注，各驱动复刻的哪部分

| 标注类型 | 你做什么 | 它驱动复刻的什么 | 当前支持 |
|---|---|---|---|
| **① 业务 Marker** | 在脚本里插入预设标记（报告截图/序列选择/Meta/影像画布/窗宽窗位/序列布局） | 圈定「需要复刻的关键动作/状态」，触发该动作**前后**的 DOM 快照捕获 → 生成可点击复刻 overlay | ✅ 完善（markers.py 注册表，右键插入） |
| **② 细粒度选择器/拓扑精修** | 重写脚本里的 locator / 补 iframe 链 / 稳定 `data-testid`、`#id`、role | 离线回放时定位可点击元素：**选择器越稳定，复刻交互越准、run 风险越低** | ⚠️ 只能通过改脚本文本间接实现，无专门面板 |

---

## 2. 复刻界面是怎么根据标注渲染出来的（弄清机制才好操作）

```
processed_script（含 Marker + 动作）
        │  插桩重放（capture-only）
        ▼
每个 Marker 关键动作前/后捕获：
  · topology.json    页面/popup/iframe 拓扑
  · target.json      目标元素定位
  · selector_closure.json  外层 DOM 快照
  · 截图             该元素所在区域截图
        ▼  build_replica 渲染
Replica 静态站点：
  · 每张「DOM 快照截图」作背景
  · 每个关键动作目标叠一个【透明可点击 overlay】（data-replica-action）
  · 离线回放用「自动解析的 locator」在 overlay 上定位 → 点击/填值/切状态
```

关键点：
- **复刻界面 = 真实 DOM 快照的截图 + 透明可点击层**（不是重新实现原站 JS）。画布动态像素、原站 viewer JS **不**复刻（能力矩阵标注为 `unsupported`）。
- **每个可点击元素对应一个 `ActionTarget`**，它带一个从脚本自动解析出的 `locator`（`replica_models.LocatorRecipe`）。
- **locator 的稳定性由 7 级风险分档判定**（`_LOCATOR_RISK_ORDER`）：

| 风险档 | 举例 | run 影响 |
|---|---|---|
| 1 `stable_id` | `#series_list`、`[data-testid=x]` | ✅ 最稳 |
| 2 `aria` | `get_by_role("button", name="确定")` | ✅ |
| 3 `stable_attribute` | `[name=report]` | ✅ |
| 4 `text` | `get_by_text("肺窗")` | ✅ 尚可 |
| 5 `ordinal` | `.nth(2)`、`.first` | ⚠️ 顶到 `partial` |
| 6 `structural` | `>td`、`:nth-child`、`[attr]` | ⚠️ 顶到 `partial` |
| 7 `coordinate` | `page.mouse.click(x,y)` | ⚠️ 顶到 `partial` |

> `partial` 不是失败，但意味着「该关键动作只能靠不稳定选择器复现」，不满足最高成功标准。

---

## 3. 操作 SOP：从零搭建一个复刻界面（人工标注视角）

以下每一步都从「我要让复刻界面准确复现这个医院」出发。

### Step 0 — 准备
- 环境就绪 + `.env` 填好 LLM key（`docs/PIPELINE_RUNBOOK.md` §1）。
- 目标医院 URL 确认，账号登录方式选定（scripted / interactive）。

### Step 1 — 录制（尽量一次录到关键状态转换）
- `main_gui.py` → 填 URL →「▶ 启动录制」→ 在真实浏览器依次完成：登录 → 进入序列列表 → **打开一次报告** → **打开一次 Meta 面板** → **切一次窗宽窗位/布局**（这些是你要复刻的关键动作）。
- 录制得越「贴着你想要的复刻流程」，后续标注越少。
- 停录前，把**每个想圈成可点击节点的动作都留下**。

### Step 2 — 插入业务 Marker（圈定要复刻的关键节点）
- 停止录制后右键插入，按需：
  - **报告截图**：放在「打开报告页」之后 → 复刻会留下报告页可点击 + `report.jpeg`。
  - **序列选择 / 影像画布**：放在序列/画布交互处。
  - **Meta 信息工具**：放在打开 Meta 面板之后 → 复刻保留 Meta 面板 DOM。
  - **窗宽窗位 / 序列布局**：手编固定操作，照录即可。
- **原则**：每个你想在复刻里「能点、能切、能读」的节点，都要一个 Marker（或落在关键动作前后）。
- 保存：「💾 保存处理后代码」→ 生成 `processed_{医院}.py` + `replica_annotations.json`。

### Step 3 — 细粒度选择器精修（让人工标注更准、可做到 `success`）
这是「人工标注」的**第二步**：让关键动作的 locator 更稳定。
- **优先让每个关键动作用「稳定 id / role / data-testid」定位**，而非坐标或 ordinal。
- 若原始脚本用的是 `page.mouse.click(x, y)`（坐标）或 `.nth(2)`（ordinal），**改写脚本**为 `page.locator("#stable_id").click()` 或 `get_by_role(...)`。
- iframe 内目标：确认脚本用了 `.locator(X).content_frame` 链（复刻会保留 iframe 子文档拓扑）。
- 你写的选择器越贴近 1–4 挡，`run` 越可能 `success` 而非 `partial`。

> 实操提示：`pipeline_report.json` 里有 `stages[].metrics.risk_counts` 与每动作的 `locator_risk`，跑完后看哪些动作落在 `ordinal/structural/coordinate`，把它们改稳再重跑。

### Step 4 — 一键跑复刻管道
- GUI 点「⚙️ 生成 Adapter + 离线复刻」（或命令行 `--operation full`）。
- 等待 7 阶段；`interactive` 登录时在弹窗登录完点「登录完成，继续」。

### Step 5 — 查看复刻与结果
- 报告：`out/{医院}/runs/{run_id}/pipeline_report.html`。
- 打开复刻界面：`.../replica/serve_replica.py` 起本地服务 → 浏览器访问（可断网打开，纯静态）。
- 验收：
  - `success`：所有 critical 关键动作用稳定 locator 复现。
  - `partial`：有 ordinal/structural/coordinate 或能力降级（如画布动态帧）。**不一定是坏**——canvas 类医院因 `unsupported` 能力天然 `partial`，但复刻界面仍可点击查看。
  - `failed`：有关键失败（定位不到 / offline 有外部请求 / 有隐私泄漏），按 `error_category` 修。

### Step 6 — 迭代
- 看 `locator_risk` / `risk_counts` → 改稳选择器 → 重跑（`--operation offline-validation` 或 `replica-build`，不重新录制）。
- 复刻里点不到的元素 = 没被 Marker 圈到或快照没捕获到 → 回 Step 1/2 补录补标。

---

## 4. 人工标注「做得好」的验收清单（SOP 自检）

- [ ] 每个要在复刻里交互/读取的关键节点，**都有一个 Marker**（或被关键动作覆盖）。
- [ ] 关键动作的 locator **尽量落在 1–4 挡**（无坐标/ordinal 命中的 critical 动作）。
- [ ] iframe 内目标用 `.content_frame` 链，未塌缩成 div。
- [ ] `external_requests.json == []`（两道：浏览器 route + 进程 socket）。
- [ ] `privacy validation ✅`（报告/事件流无凭据、无病人数据）。
- [ ] `pipeline_report.json.status` 与 GUI 显示的 `completed` 一致。
- [ ] 复刻界面可断网打开，关键元素可点击、可切状态。

---

## 5. 优化盘点：当前复刻操作的痛点 → 改进方向

> 本节按「最值得做」排序。都来自对当前实现能力的真实盘点，不是臆想。

### 痛点 1（最核心）：没有「人工标注面板」，只能改脚本文本
- **现状**：locator 由 `parse_action_plan` 自动解析；人工只能通过重写 processed 脚本里的选择器来精修，门槛高、易错、无反馈。
- **建议 A （推荐，入手）**：给 GUI 加一个「复刻标注面板」——在每个关键 `ActionTarget` 旁显示其自动解析出的 locator + 风险档位，允许**直接改写 locator / 补 iframe 链**，改后 `ast.parse` 校验并回写脚本，即时显示风险变化。
- **建议 B**：录制时让用户**点选元素**指定稳定定位方式（如右击元素 → 用 `data-testid`/role），替代手写选择器。
- 受益：把「改文本才生效」变成「可视化高亮 + 风险即时反馈」，显著降低人工标注门槛。

### 痛点 2：风险语义对一线操作者不直观
- **现状**：`partial` 原因分散（ordinal/structural/coordinate + 能力降级），用户难知道「是选择器不稳还是能力边界」。
- **建议**：报告按「可修 vs 能力边界」分类呈现代理 marker：属于可人工修（风险挡）的高亮「可改进」，属于 `unsupported` 能力的标注「复刻边界，不改判为失败」。
- 受益：让「我该去改选择器」与「这不是我改得了的」一眼分清。

### 痛点 3：圈不住「区域/滚动/动态列表」
- **现状**：复刻 overlay 针对单个目标元素；序列长列表/虚拟滚动的局部区域捕获能力有限（`_LOCATOR_RISK_ORDER` 里 `region` 提示），动态较长列表可能只能到 `partial`/`degraded`。
- **建议**：给 Marker 增加「区域标注」类型（框选一个可滚动 region 并记录滚动步进），复用 `capture_snapshot` 的虚拟滚动步进能力，让序列区更完整。

### 痛点 4：画布类只能到 `partial`（能力边界）
- **现状**：`canvas_dynamic_pixels` 默认 `unsupported`，影像画布 marker 天然 `partial`；这不影响复刻界面可查看/可点，但无法证明动态帧。
- **建议**：若后续要支持，可在 Mark 阶段为画布记录「逐帧截图」并实现帧间 diff 判定（非首版承诺，需约定为新增能力）。当前优先确保「定位/聚焦/点击」正确即可。

### 痛点 5：迭代闭环缺「差异对比」
- **现状**：每次重跑是独立 run（`runs/{new_run_id}`），没有「这次标注比上次好在哪」的对比视图。
- **建议**：报告增加「与上次 run 的 risk_counts / 通过集合 diff」，让人工标注的效果可量化(何时改善、何时退化)。

### 优先级小结
| 顺序 | 改进 | 类型 | 收益 |
|---|---|---|---|
| 1 | 复刻标注面板（痛点1） | 新功能 | 人工标注门槛大降 ← **推荐先做** |
| 2 | 风险语义可视化（痛点2） | 报告增强 | 操作者可判断 |
| 3 | 区域/滚动标注（痛点3） | 新能力 | 长列表复刻更完整 |
| 4 | 画布逐帧能力（痛点4） | 能力扩展 | 画布类可望越过 partial（远期） |
| 5 | run 间对比（痛点5） | 报告增强 | 迭代可量化 |

---

## 6. 参考文档
- 一键管道操作：`docs/PIPELINE_RUNBOOK.md`
- 产品化设计（权威规格 / 成功标准 / 能力收缩 §6.6.1）：`docs/superpowers/specs/2026-08-04-one-recording-adapter-replica-pipeline-design.md`
- 事件协议：`docs/superpowers/specs/2026-08-05-gui-orchestrator-event-protocol.md`
- 真实站点验收：`docs/REAL_SITE_SMOKE_TEST.md`
- 项目约定/调试原则：`CLAUDE.md`
