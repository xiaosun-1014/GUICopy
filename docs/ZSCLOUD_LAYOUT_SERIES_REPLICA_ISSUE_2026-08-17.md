# 中山 zscloud 复刻：「布局切换」与「序列选择」耦合问题——根因分析与解法

> 日期：2026-08-17 · 范围：中山 zscloud（Dapeng viewer）离线复刻 · 产物：`out/zscloud/runs/20260817T053551Z-c3a374/replica`
> 本文基于**产物层**（replica HTML / runtime / manifest）与**代码层**（capture / build）双份证据交叉验证，所有引用均可定位。

---

## 0. 现象与诉求

**用户诉求**：在中山影像界面，操作顺序是 **先调整界面布局 → 再选择序列**。期望离线复刻能还原这两个独立步骤。

**现状问题**：

1. 在复刻入口点击序列项，**界面布局和序列被一并改变**（布局从入口的 2×2 跳成 1×1，序列也切换了），无法「只选序列不动布局」。
2. 「先单独调布局、再单独选序列」的路径走不通：把布局切到 1×1 后会卡死在 `s_003`，点什么都无反应。
3. 因此**一次录制无法直接生成正确的复刻界面**——中间隔着多个管线缺口，不是单点 bug。

---

## 1. 背景：复刻管线如何工作（读本文前必须理解）

```
录制 processed_script_zscloud.py
  → 插桩回放（instrumented_replay.py：每个 marked 动作前后各一次全量快照）
  → capture：manifest.json + snapshots/（拓扑、截图、target DOM、series 分支）
  → build：build_replica.py 依据快照生成 复刻 HTML（截图帧 + 热区 overlay + 跳转表）
```

**复刻的本质**（`docs/REPLICA_DESIGN.md`）：**静态截图帧 + 热区 overlay**，原站 JS 完全禁用。每一个「状态」（`s_0XX` / `bviewer_b00X`）是一张捕获时刻的截图，可交互的是叠加在截图上的透明热区。**没有任何状态内的动态变化**：没有 dblclick、没有布局重排、没有渲染管线。一次「点击」的结果只能是 **整页跳转到另一个状态帧**（`window.top.location.assign`，`replica/replica_runtime.js:27-28`）。

**含义**：复刻里「布局切换」和「序列选择」都不是动作，而是**跳转到不同布局的截图帧**。布局的变化完全由「帧里截图长什么样」决定——这就是下面一切问题的根源。

---

## 2. 现状：逐状态交互表（已逐一核对 c3a374 产物）

| 状态 | 画面布局 | series 热区 | 布局按钮 | 可点击跳转 | 说明 |
|---|---|---|---|---|---|
| `s_001`（入口 viewer） | **2×2**（分享页默认基线） | ✅ 4 个 | `a_001_001` 可点 | →s_002；序列→bviewer_b* | 布局+序列共存，但点序列=整页跳 1×1 分支 |
| `s_002`（布局下拉展开） | 2×2 | ❌ 0 | `a_001_002`（`*1 Shift+1`）可点 | →s_003 | 其余 9 个布局选项为 0×0 纯装饰 |
| `s_003`（1×1 已应用） | **1×1** | ❌ 0 | 按钮存在但无转场 | **`{}` 空** | **死胡同**：布局调完即卡死，无出口 |
| `s_004`（选序列后） | 1×1 | ✅ 4 个 | 按钮为装饰 | →s_005 | **不可达**（见 §3.3） |
| `s_005`（Meta 打开） | 1×1 | ✅ 4 个 | 装饰 | →s_006 | |
| `s_006`（Meta 关闭） | 1×1 | ✅ 4 个 | 装饰 | →s_007 | 含 canvas 热区 |
| `s_007`（终态） | 1×1 | ✅ 4 个 | 装饰 | `{}` | 终态 |
| `bviewer_b000~b003`（分支） | **1×1** | ✅ 4 个 | **无布局控件**；**无返回主链入口** | 分支间互跳；`series:...:meta_open`→Meta | 点序列后只能在这些 1×1 帧间游走 |

---

## 3. 根因分析

### 3.1 帧级时序耦合（主因 / `R1`）——「点序列 = 布局+序列一起变」的直接机制

录制脚本的时序是 **先切 1×1，再 dblclick 选序列**，之后布局从未切回：

```
序列布局切换: 点「序列布局」→ 点「*1 Shift+1」   (a_001_001 / a_001_002)
序列选择:     #HLeftThumnail li.ui-draggable dblclick（多匹配）(a_002_001)
```

于是捕获侧所有「选序列之后」的帧，**都已经是 1×1 布局**：

- 主路径 `s_004`（a_002_001 的 after 快照）：1×1。
- 每条 series 分支 `bviewer_b00X`：由 Meta 关闭动作成功分支触发 `capture_hook_expand_series` → `capture_one_series`（`batch_capture_replicate.py:2269-2271`、`923-1109`），**逐序列截图时布局早已是 1×1**，bviewer 背景帧全部是 1×1（像素网格验证：s_001=2×2 暗十字，s_003/s_004/bviewer_b* = 1×1）。
- 而入口 `s_001` 是分享页打开时的 **2×2 基线**（用户录制时才把它切到 1×1），序列热区经 `_promote_series_regions_to_earliest_documents` 提升到 `s_001`，`viewerUrl` 指向 1×1 的 bviewer。

**效果**：用户从 2×2 的入口直接点序列 → 整页跳到 1×1 的 bviewer 帧 → **同时看到「布局 2×2→1×1」和「序列切换」**。这不是事件绑定，而是「布局被烘焙进了选序列后的每一帧」，复刻只能照搬快照。**不存在任何「已切 1×1、但还没选序列」的中间帧可供用户先完成第一步。**

### 3.2 序列 dblclick 主路径无可点击载体（`R2`）——「先布局后选序列」走不通的直接原因

`a_002_001`（序列 dblclick）在捕获时，目标 DOM 快照失败：

- `capture_locator_snapshot`（`capture_snapshot.py:134-161`）对多匹配 locator 直接 `.evaluate()` → **strict-mode 多匹配异常** → 被 `except Exception: pass` 静默吞掉（`batch_capture_replicate.py:791-798`）。
- 后果：`snapshots/a_002_001/` 下**没有 `target.json`**（只有 topology 截图；实测缺 `target.json` / `selector_closure.json`）。插桩里 `locator_factory()` 也不带 `.first`（instrumented 第 39/45 行）。
- build 侧 `document.targets` 按 `target.action_id in transitions` 过滤（`build_replica.py:1360-1369`），缺 target 的 `a_002_001` 及其过渡 `t_a_002_001 → s_004` **永远不会被渲染成可点击元素**。

**后果叠加**：

- `s_003`（1×1 已应用、恰是「布局就绪、待选序列」状态）的 `__REPLICA_TRANSITIONS__` 实测为 `{}`，且没有序列热区——series region 只被提升到**最早**状态 `s_001`（`build_replica.py:1060-1116`），`s_002`/`s_003` 都没有。
- `s_004`（真实「选序列后」状态）因转场无载体而**不可达**。
- 用户在 `s_003` 卡死：无转场、无序列热区、无返回按钮。**「先布局 → 再选序列」在结构上不存在。**

### 3.3 布局自由度缺失（`R3`）——只有 1×1 被复刻出来

布局浮层（点「序列布局」弹出的菜单）里：

- 只有被录制成动作的 `*1 Shift+1`（`a_001_002`）变成了 `data-replica-action` 可点元素。
- 其余选项（1×2、2×1、2×2、3×3、品字等 9 个）只是 layout region 的**成员**（region root `#cellStyle`，`capture_snapshot.py:391-403` 把「序列布局切换」归为 `layout` 类型），生成时走无 action_id 的 `_positioned_html` → 仅 `data-replica-overlay` 装饰，`pointer-events:auto` 但**无任何 JS handler**（实测 `layout_1_2` 无 `data-replica-action`）。

即：复刻里**不存在**「把布局先单独调成 2×2/3×3 等再选序列」的自由度，布局切换被固化成录制时那一次点击（1×1）。这与设计文档 §5.1 的要求（`序列布局切换 | layout | 所有可见布局项及文本/ARIA`）相悖——**layout region 捕获了全部选项，但 build 只让被录制的那一个可交互**。

### 3.4 入口热区重叠（次要 / `R4`）——在入口点“布局”附近极易误触序列项

`s_001` 上布局按钮 rect=(25,458,40×40)，第 4 个序列项 rect=(19,389,314×83)，两者在 **y∈[458,472] 有约 14px 重叠带**；CSS 里序列项 `z-index:2` > 动作 `z-index:1`（`build_replica.py:461`），runtime 也**先匹配 series-key 再匹配 action**（`build_replica.py:155-181`）。点重叠带会命中序列项并跳到 1×1 分支帧，增强「我一碰就布局+序列一起变」的观感。这不是主机制，但加剧入口的误触。

### 3.5 系统层面：为什么「一次录制直接生成正确复刻」现在还做不到

| 用户期望的闭环 | 现状断点 | 断裂所在层 |
|---|---|---|
| 「先调布局」这一步，且布局可选 1×1/2×2/… | 只有 1×1 一条链路可点；调完卡死 s_003 | R2（无载体）+ R3（选项未建 checkpoint） |
| 「再选序列」这一步，在已调好的布局上 | s_003 无序列热区；s_004 不可达；入口点序列=跳 1×1 分支 | R1（布局烘焙进帧）+ R2 |
| 「布局」与「序列」是两个独立状态 | 复刻模型里布局只能靠整页跳转表达，被冻结在帧里 | 架构（截图帧 + 整页跳） |
| 录制一次即得正确复刻 | 录制时「布局在选序列之前」这一时序，被捕获/build 原样复制成「布局与序列强耦合」 | R1 |

**一句话**：这**不是**单个 bug，而是四个缺口叠加 —— 布局被烘焙进「序列选择后的帧」（R1）、序列 dblclick 捕获丢 target 导致主路径转场无载体（R2）、布局选项只有被录制的一个可点（R3）、入口热区重叠（R4）。其中 R1 是架构级（决定「布局必须作为独立维度而非烘焙进帧」），R2/R3/R4 是管线缺陷（可在 capture/build 层修复）。

---

## 4. 解决方案

### 4.1 主路径：把「布局」从「序列选择后的帧」里解耦出来（治本，对应 R1）

**核心主张：布局状态必须作为独立维度建模，而不是烘焙进选序列之后的每一帧。**

两种落地方式，按侵入度排序：

**方案 A（背景层替换，改动最小、推荐先做）**
- 把「布局切换」实现为**同状态内的背景图替换**，而不是跳到一个新死胡同状态：
  - 为一个关键状态（入口 `s_001`）捕获**多种布局**的背景帧（1×1/2×2/3×3…各一张）；
  - 布局按钮/浮层选项点击后**只替换该状态的背景 `<img>` 源**（`build_replica.py` 现有 `replica-bg` 机制），**序列热区保持常驻**；
  - 布局选择不产生新状态、不改变路由，序列列表永远可点。
- 代价：需要捕获入口状态的多布局背景（每布局一次全量快照，复用现有拓扑），并给浮层所有选项建 checkpoint（见 4.3）。
- 收益：彻底消除 s_003 死胡同与「布局+序列绑定」，布局从「过程步骤」变成「视图参数」，用户可以在任意时刻、任意状态下切布局再选序列。

**方案 B（状态×布局正交快照，完整但成本高）**
- 为「状态 × 布局」组合分别捕获快照（s_X × {1×1,2×2,…}），布局切换=在正交维度上跳转，序列选择=沿状态链跳转。
- 成本：快照数量乘法级增长，录制工作量显著上升，仅在需要「布局成为完整可回溯流程」时选用。

**最省力的三角色定位**：若业务上最终总是停在 1×1（放大看序列），可以先把**入口基线改设为 1×1**（把 1×1 帧提升为入口，序列热区常驻其上）——此时点序列无 2×2→1×1 的跳变感，「布局与序列同步变化」的观感直接消失，是 5 分钟级的临时止血；但**不解决用户要 2×2 等布局时的问题**，必须与方案 A/B 配合才是完整解法。

### 4.2 恢复「先布局后选序列」主路径（对应 R2）

- **修 a_002_001 的 target 捕获**：`capture_locator_snapshot` / 插桩的 `locator_factory()` 对多匹配 locator 使用 `.first`（或改为按 series region 解析），杜绝 strict-mode 静默丢 target（`capture_snapshot.py:134-161`、`batch_capture_replicate.py:791-798`）。修复后需要**重新录制或重跑一次 capture**，让 `target.json` 落盘。
- **给 s_003 出口**：把 series region 的提升从「只提升到最早状态」扩展为「提升到所有**未含** series region 的 viewer 状态」（`s_002`/`s_003` 也带上序列热区），或给 `s_003` 加返回 `s_001` 的 `data-replica-back` 入口；这样「布局调完」可继续选序列或退回重调。
- 让 `t_a_002_001`（序列选择转场 → `s_004`）拥有可点击载体，使「已布局 → 再选序列」的主路径真正可达。

### 4.3 布局选项全部可点化（对应 R3）

- build 侧对 `region_type=="layout"` 的**所有可见成员**生成 checkpoint（`data-replica-action` + 转场），而不仅是录制的那个动作（`build_replica.py:577-608`）。
- 每个布局选项的转场目标 = 该布局在对应状态的背景帧（配合 4.1 方案 A）。
- 无法立即支持的布局选项至少明确 `disabled` 样式，避免「看似可点、点了没反应」。

### 4.4 入口热区去重叠（对应 R4）

- `s_001` 上布局按钮与序列项 bbox 重叠的 14px 带，构建时剔除序列热区中的重叠区域，或调整命中优先级（布局按钮 `z-index` 提升至序列项之上、runtime 先查 action 再查 series），避免入口误触。

### 4.5 录制/捕获侧配套（保证「一次录制直接生成」）

- **布局 marker 回放加稳定判定**：`wait_for_pre/post_action_state`、`ensure_post_action_state` 目前对「序列布局切换」无任何特判，布局切完不回等画布重构（`batch_capture_replicate.py:37-101,191-231`）。补上「浮层展开可见」与「切完布局画布稳定」的等待（参考 `skills/zscloud-film-capture/references/known-issues.md` 的 1.5s DOM 重建经验），避免回放竞态。
- **录制规格建议**：若「先布局后选序列」是稳定诉求，录制时布局动作**前置并独立标注**（它就是流程前置条件），让捕获把布局状态当作「起点参数」而非「中间步骤」；或直接把入口基线布局录成目标布局（1×1），减少帧间跳变。

### 4.6 建议的落地顺序

| 步骤 | 内容 | 层级 | 是否需要重录 |
|---|---|---|---|
| 1（止血） | 入口基线改 1×1，消除跳变观感 | build | 否（离线 rebuild） |
| 2 | 修 a_002_001 target（.first/容错），恢复主路径转场 | capture+build | 是（重录 1 次） |
| 3 | s_003/s_002 补序列热区 + s_003 返程入口 | build | 否 |
| 4 | layout region 全部选项 checkpoint + 入口去重叠 | build | 否 |
| 5（治本） | 布局维度化（方案 A 背景层替换 / 方案 B 正交快照） | capture+build | 视方案 |

步骤 2 是「先布局后选序列」能走通的**前提**；步骤 5 是「布局成为独立可调维度」的**根治**。

---

## 5. 验证与验收建议

现有 headless 全链路验收（`memory/zscloud-dapeng-replica-adaptation.md` §5）只覆盖「入口点序列→分支→分支内切序列→两步 Meta」。针对本问题补充验收点：

1. 入口：**先**点布局按钮 → 选 1×1 → 该状态仍能从序列列表选序列（不卡死、可回退）。
2. 任意布局下点序列项：**布局不变、仅序列切换**（对比背景帧 hash 相同、仅选中态变化）。
3. 入口点「布局附近」不误触序列（重叠带已剔除）。
4. 布局浮层 ≥2 个选项可点且各自生效（1×1/2×2…背景确实变化）。
5. `s_003` 类状态 `transitions` 非空（无死胡同）。

---

## 附录 A：证据索引

| 发现 | 位置 |
|---|---|
| 复刻只有整页跳转交互、无布局专用逻辑 | `out/zscloud/runs/20260817T053551Z-c3a374/replica/replica_runtime.js:48-75`，series 跳转 `:27-28` |
| 各状态交互表（series_keys / transitions / actions） | 逐一核对 `replica/states/s_001…s_007/documents/d_p_001_f_001/index.html` |
| s_003 `__REPLICA_TRANSITIONS__={}` 且无序列热区 | 同上，`states/s_003/.../index.html`（实测） |
| bviewer 无布局控件、无返回入口 | `replica/states/bviewer_b00X/.../index.html`（`data-replica-back` 全复刻仅 4 处且都在 bmeta） |
| 布局烘焙进选序列后的帧（录制先 1×1 后选序列） | `out/zscloud/processed_script_zscloud.py:16-22`；入口 2×2 vs 分支 1×1 像素网格对照 |
| 分支捕获在 1×1 布局下逐序列截图 | `batch_capture_replicate.py:2269-2271`（expand hook 挂在 Meta 关闭后）、`923-1109`（capture_one_series） |
| 多匹配 locator strict-mode 丢 target | `capture_snapshot.py:134-161`；`batch_capture_replicate.py:791-798`；实测 `snapshots/a_002_001/` 无 target.json |
| build 按 target 过滤 transitions → a_002_001 无载体 | `build_replica.py:1360-1369` |
| series region 只提升到最早状态 | `build_replica.py:1060-1116`（`_promote_series_regions_to_earliest_documents`） |
| 布局浮层其余选项只是 region 成员、无 action | `build_replica.py:577-608`；实测 s_002 `layout_1_2` 无 `data-replica-action` |
| 入口布局按钮与序列热区 bbox 重叠 + z-index | `build_replica.py:461`、runtime 命中顺序 `:155-181` |
| 布局 marker 无回放等待/稳定判定 | `batch_capture_replicate.py:37-101,191-231` |
| 设计文档要求布局独立建模 | `docs/REPLICA_DESIGN.md` §5.1（`layout` region、所有可见布局项）、§6.3（打开布局菜单建 state / 选布局项 always-after） |

## 附录 B：本次调查方法

- 产物层子代理：逐文件核对 c3a374（对照 f160c6/54dbd9/pre-fix）的 manifest、pipeline_report、replica HTML/runtime，以及前后状态背景 hash 差异。
- 代码层子代理：顺着 `batch_capture_replicate.py` → `capture_snapshot.py` → `build_replica.py` 整条链路定位 R1~R4 的代码位置。
- 主会话额外逐状态复核 7 个主链状态的 series_keys / transitions / actions（§2 表格）。

---

## 6. 历史问题复盘：中山（zscloud）与飞图（FTImage）复刻管线问题谱系

> 本文 §3 的 R1–R4 不是孤立缺陷，而是复刻管线同一批结构性问题的最新现场。下面把此前两市已修复的问题完整存档，并提炼跨站点共性规律，方便理解「该类问题还会出现在哪里、怎么防」。

### 6.1 中山（zscloud / Dapeng viewer）已修复问题

来源：`memory/zscloud-dapeng-replica-adaptation.md`（2026-08-17 跑通记录）。

| # | 问题 | 用户可见表现 | 根因 | 修复 |
|---|---|---|---|---|
| Z1 | Dapeng viewer 无联影 RAF 的 `#popTagText_*` | 「窗宽窗位 WL/WW」6 个动作每次空等 30–42s，累计把 instrumented replay 拖满 **900s 超时被杀**，capture fail 且无 manifest | viewer 实为智元数影 **Dapeng**（非联影 RAF）；录自 8/13 的 WL/WW 固定操作 marker 用了 RAF 选择器，`[class*='popTag']` 在 Dapeng 总数 0 | 新站先 headless 探测候选选择器真实存在性；失效的固定操作块（连同 marker 注释）直接删，再重建 annotation |
| Z2 | 分享页 SPA 渲染竞态 | `expect_popup` 偶发 30s 超时、整 run network fail；手工打开却每次能弹 | `domcontentloaded` ≠ JS 事件绑定完成，零等待直接点「查看影像」时 handler 未挂；人工录制有节奏、capture 无脑回放放大竞态 | goto 后加 `wait_for_timeout(2000)` + 注释「等 SPA 渲染完成」 |
| Z3 | series region 只注入「序列选择动作后」的状态 | 复刻入口 viewer **没有可点序列**（用户进入即无法切换）；ft 因序列点击靠前不受影响 | 序列列表在 iframe viewer 内，capture 只在「序列选择」marker 的 after 快照（s_004）注入 series region，s_001~s_003 无 | `_promote_series_regions_to_earliest_documents`：把首个含 series region 的 state deepcopy 到「同一 document_id 最早状态」，入口进入即可点序列 |
| Z4 | 分支 viewer 的 series region 挂错 document | 点序列进分支后**分支内没有任何序列热区**（列表只是截图像素，点不动）| `_capture_viewer_topology` 把分支 series region 一律 append 到 `docs_out[0]`；popup 型 viewer 里它是**外层分享页 document**，热区渲染进没人访问的主 index.html | `_reroute_branch_series_regions_to_viewer_documents`：把分支 region 迁移到 leaf id 与主路径 series document（`d_p_001_f_001`）一致的 document |
| Z5 | 改脚本后 annotation 不重建 | 产物与脚本 sha 失绑，重建副本行为错 | 改 `processed_script_*.py` 后 `replica_annotations.json` 仍是旧 sha 的 marker 行号 | 用 `main_gui.build_annotations_from_source` 从新源码重扫 marker 重建（行号自动更新、fresh UUID）|

### 6.2 飞图（FTImage）已修复问题

来源：`docs/FIX_FT_MULTI_SERIES_CLICK_AND_DISCOVERY_2026-08-16.md`（2026-08-16 关闭，run `20260816T050045Z-f44c89` 8/8 验收）。

| # | 问题 | 用户可见表现 | 根因 | 修复 |
|---|---|---|---|---|
| F1 | 动态下载进度被当成稳定序列身份 | 真实 8 行被识别成 **9 个** descriptor；MPR-Sag 第 7 行在 `locate` 阶段 `hub_unrecoverable`，被拆出的「第 9 个」因预算跳过 | 序列行无稳定 id，`identity_attrs: []` 回退文本身份：`…共 131张 106` → `…共 131张 109` 被当两个身份，全程以 106 的全文重定位 109 的行必失败 | `capture_snapshot.normalize_series_text`：删除「明确总帧数单位之后、文本末尾的独立整数」；**发现去重与激活重定位共用同一标准化函数** |
| F2 | series route 节点遗漏几何定位 | 已捕获序列有路由但透明点击层没盖住截图里的真实行——按画面点击无反应/命中错节点 | `_series_member_html` 只注入 `data-replica-series-key/role/aria-selected`，**不注入 `snapshot.rect`**；离线点击完全依赖 overlay 与截图坐标重合 | 注入 `position:absolute;left/top/width/height` |
| F3 | series route 与旧 action overlay 层级竞争 | 进入后续 Viewer 状态后点击无反应 | 旧 action overlay `z-index:1`，series route 无明确层级被盖住 | `.overlay > [data-replica-series-key] { z-index: 2; }`，序列 route 优先 |
| F4 | 折叠线以下序列不可见、不可点 | 最后一个 MPR-Sag 需滚动才见，离线静态页固定视口下不可达 | 序列行 rect 是**滚动内容坐标**，折叠下 `y >= 视口高` 无对应背景截图 | 两级：①无长图回退——`.overlay` 变独立滚动容器 + 折叠下行 `data-replica-below-fold` 由 route 自身缩略图渲染成可见行；②完整复刻模式——`_capture_series_list_full` scroll-stitch 拼整张长图（含 `series_list_content_height`），整页滚动 + `.series-pane-bg` 面板背景同步移动 |
| F5 | 分支 Meta 两步打开被压成一步 | 分支「点更多→直达 bmeta」与真实站「更多→Tags→面板」不符；主录制路径 Tags 步死路（有 transition 无渲染元素） | build 把分支 `meta_open` 压成合成一步跳 | `_augment_meta_two_step`：主路径给「有 transition 无元素」的 Tags 动作用合成可见行；分支为合成跳跃插中间态 `btags_<branch>`（复用分支 Viewer 文档/截图 + 可见 Tags 行），viewer 的更多改经它路由 |

### 6.3 跨站点共性规律

把这些已关闭问题与本文 R1–R4 放在一起看，能提炼出 **6 条反复犯案的结构性规律**：

1. **「多匹配 → strict-mode → 静默吞异常」是头号复现模式**（本次文档 R2 是第三现场）。
   - 现场一：`_capture()` 帧 owner 探测对多元素 locator `.evaluate()` → 整份 before/after 快照没落盘 → 入口无序列可点（`memory/multi-series-subprocess-mainpath-series-region-bug.md`，`.first` 修复）。
   - 现场二：激活路径 `_locate_series_row` 漏接配置 `item_selector`，默认选择器对 `a` 行一个都不命中 → 「发现成功、激活全失败」空架子（`memory/multi-series-activation-selector-divergence.md`；zs 因 `li.ui-draggable` 恰好被默认 `li` 兜住而掩盖了断链）。
   - 现场三（本次）：`capture_locator_snapshot` 对 `#HLeftThumnail li.ui-draggable` 多匹配直接 `.evaluate()` → `target.json` 缺失 → 主路径转场 `t_a_002_001` 无载体。
   - **防再犯**：凡对序列这类天然多匹配目标的任何单元素操作，一律 `.first` 或显式容错；`except Exception: pass` 会让「快照缺失」静默化，直到离线交互时间用户才发现。

2. **文本身份 / 几何定位 / 点击层级三者任一不一致，离线「截图+热区」就错位**。
   - F1（身份）、Z3/Z4/F2（几何归属）、F3/本次 R4（层级重叠）本质上都是「发现与激活语义不同源」的变体。修复方向是**同一 viewer 的发现、激活、定位、层级必须同源读取、共享同一套标准化/坐标/几何**。

3. **popup/iframe 型 viewer 的「region 挂哪个 document」是一族问题**（Z3/Z4）。
   - 主路径靠「提升到最早状态」、分支靠「以主路径 series document 为基准 reroute」。判别口诀：入口不可点 → 先查 manifest 入口 state 的 document 有无 `region_type=="series"`。

4. **复刻模型冻结一切动态，动态必须显式建模**（贯穿所有案例）。
   - 布局（本次 R1）、滚动（F4）、下载进度（F1）、SPA 时序（Z2）、面板开合（F5）——凡是捕获时在变的，要么被烘焙进帧（等于锁死），要么需要专门的捕获/建模通道。**新录制前先问：这个流程里有几个「维度」在变？**
5. **录制规格直接决定复刻上限**。
   - 布局 marker 无回放等待（本次）、Meta open/close 要成对录制（SOP R2）、序列激活动作 kind 要录真实站需要的（SOP U1）……marker 的**分组粒度与录制顺序**直接决定状态拓扑能不能表达用户要的流程。录制时把「前置条件动作」（如布局）与「主流程动作」(如选序列）分开标注，capture 才能把它们建模成独立状态而非耦合帧。
6. **「离线可修」与「必须重录」有明确边界**。
   - 可离线 rebuild：F1 身份标准化、F2/F3 几何与层级、Z3/Z4 提升/重路由、F5 合成中间态、本次 R3/R4。
   - 必须重录：缺失的快照本身——F4 的长图、本次 R2 的 `target.json`、R1 的「布局×状态」多维帧。§4.6 的落地顺序表正是基于这条边界。

### 6.4 与本次问题（布局 vs 序列耦合）的直接关联

- **R2 是「多匹配→strict-mode→静默吞」的第三现场**：同类已修过两处（`_capture()` 帧 owner 探测、`_locate_series_row` 选择器透传），但 `capture_locator_snapshot` 这个调用点仍在裸奔——本次序列 dblclick 的 target 恰恰是它吞掉的。修 R2 应遵循规律 1：.first/容错 + 不要静默吞。
- **R4 与 F3 是同一个「离线热区叠加」问题的左右手**：FT 那边是 route 被 action 盖住（修法：route z-index:2 优先）；中山这边相反——布局按钮(action)被系列热区(series-key, z-index:2)盖住，点击「布局」附近会误触序列。需要同一套「序列区 route 优先」策略的逆操作：**同像素上 action 与 series 的优先级要按真实交互语义显式决定**。
- **R1 是规律 4 在「布局维度」的体现**：布局与序列是两个正交维度（状态 × 布局），录制时却按线性时序「先布局后序列」录成了单链快照，于是布局被烘焙进选序列后的每一帧。解法（§4.1 方案 A/B）本质上是在给快照模型**补上「维度」这个词**——这正是一系列「冻结动态」问题的根治方向。
