# 【执行计划】修复中山 zscloud 复刻「布局切换 ⇄ 序列选择」耦合

> **问题文档**：`docs/ZSCLOUD_LAYOUT_SERIES_REPLICA_ISSUE_2026-08-17.md`（根因 R1–R4 与证据链，先读它）
> **格式参考**：`docs/PLAN_FIX_REPLICA_META_SCROLLABLE_PANEL.md`（本项目执行计划惯例如下）
> **用途**：可直接执行的分步计划，交接给下一位模型或按序人工执行。
> **项目根**：`D:/00-Project/04-codegencopy` · 解释器一律 `D:/Anaconda/envs/codegen-marker/python.exe`
> **测试前缀**：`$env:QT_QPA_PLATFORM='offscreen'`；`PYTHONIOENCODING=utf-8`（中文输出）
> **当前 git**：`main`，HEAD=`f61194f`；工作区已有未提交的 Z3/Z4 build 修复（`_promote_series_regions_to_earliest_documents` / `_reroute_branch_series_regions_to_viewer_documents`）+ 中山记忆文档，本计划在其上叠加，**先提交现有成果再动 R1–R4**。

---

## 0. 目标（最终验收标准）

中山 zscloud（Dapeng viewer）离线复刻，做到「**布局调整**」与「**序列选择**」是**两个独立可用**的步骤：

- ① 入口 `s_001` 能**先单独调布局**（点「序列布局」→ 弹层**至少 1×1/2×2/3×3 等 ≥2 个布局可点**，各自真实生效）——解决 R3。
- ② 调完布局后**仍能选序列**（不会卡死、可回退、序列热区不消失）——解决 R2/R1 的「s_003 死胡同」。
- ③ **点序列项时布局不变、仅换序列**（背景帧不变、仅选中态变化，不再 2×2→1×1 一起跳）——解决 R1。
- ④ 入口点「布局」附近**不误触序列**（重叠热区已剔除）——解决 R4。
- ⑤ 重新录制一次后，**一键产出满足 ①–④ 的复刻**（不用手工改产物）。

> 验收环境：headless 全链路（`test_replica_runtime` 浏览器套件）跑 `out/zscloud/runs/<新run>/replica/index.html`。
> 关键：**数量不变性** —— series 热区数量、分支数量在 ①②③④ 前后必须守恒（不新增假分支、不吞掉真实分支）。

---

## 1. 背景事实（已确认，勿重查；证据见问题文档 §2/§3/附录A）

1. **复刻 = 静态截图帧 + 热区 overlay + 整页跳转**，原站 JS 禁用，无 dblclick，无状态内动态变化（`build_replica.py` 顶部 `RUNTIME`）。
2. **中山录制顺序**：`序列布局切换`（点「序列布局」→ 点「*1 Shift+1」）→ `序列选择`（dblclick `#HLeftThumnail li.ui-draggable`）→ Meta → 画布（`out/zscloud/processed_script_zscloud.py:16-29`）。
3. **布局与序列耦合的机制**：布局被烘焙进「选序列后的每一帧」——`s_004` 与全部 `bviewer_b00X` 都是 1×1；入口 `s_001` 是分享页默认 2×2。点序列 = 整页跳 1×1 分支帧。
4. **主路径「序列选择」转场断链**：`a_002_001` 无 `target.json`（`capture_locator_snapshot` 多匹配 strict-mode 被 `except: pass` 吞，`batch_capture_replicate.py:791-798`）→ `t_a_002_001` 无载体 → `s_003` 死胡同、`s_004` 不可达。
5. **布局浮层**：region 归 `layout` 类型（`capture_snapshot.py:391-403`），root=`#cellStyle`，16 个成员；**只有被录制成动作的 `*1 Shift+1`（a_001_002，id=`layout_1_1`）可点**；其余（`layout_1_2` 等）走无 action 的 `_positioned_html` → `data-replica-overlay` 纯装饰（`build_replica.py:667` 分支）。
6. **入口热区重叠**：布局按钮 (25,458,40×40) 与第 4 序列项 (19,389,314×83) 在 y∈[458,472] 相交；CSS `series-key z-index:2` > `action z-index:1`（`build_replica.py:461`），runtime 先匹配 series-key 再匹配 action（`build_replica.py:155-181`）。
7. **RUNTIME 已支持 `data-replica-back`、`data-replica-action`、`data-replica-series-key` 三类跳转**，无布局专用逻辑（`build_replica.py:108-182`）。
8. **历史同族问题**：strict-mode 吞异常已两度修复（`_capture()` 帧 owner 探测 `.first`，`batch_capture_replicate.py:722`；`_locate_series_row` 透传 `item_selector`），但 `capture_locator_snapshot` 调用点（`:793`）**仍未加保护** —— 本计划的 R2 是第三现场。

> **非多模态约束**：本机代理模型非多模态，截图只 `.jpeg` 落盘不回读。验收图形类一律用 DOM 断言（元素存在性 / 背景 hash 不变 / 路由正确），不用 `Read` 读图。
> **HTTPS/localhost**：replica 用 `file://` 或本地 http（`_tmp_serve_zs.py` 模式）跑，勿在 HTTPS 页面 fetch。

---

## 2. 前置：先提交工作区现有成果（与 R1–R4 无关，但避免混淆）

**理由**：Z3/Z4 的 build 修复（`_promote...`/`_reroute...` + 测试）是**已跑通但未 commit** 的成果；`memory/zscloud-dapeng-replica-adaptation.md`、`docs/ZSCLOUD_LAYOUT_SERIES_REPLICA_ISSUE_2026-08-17.md` 也应入库，让 R1–R4 的 commit 干净可读。

```powershell
cd D:/00-Project/04-codegencopy
git add -- build_replica.py test/test_build_replica.py memory/README.md memory/zscloud-dapeng-replica-adaptation.md docs/ZSCLOUD_LAYOUT_SERIES_REPLICA_ISSUE_2026-08-17.md
git commit -m "fix: promote entry & rehome branch series regions for zscloud popup viewer
- _promote_series_regions_to_earliest_documents: entry viewer shows clickable
  series list from the start (zscloud records series click late)
- _reroute_branch_series_regions_to_viewer_documents: branch series regions
  land on the viewer iframe document, not the share-page shell
- docs: zscloud replica adaptation memory + layout/series coupling issue doc"
```

**注意**：`CLAUDE.md`、`memory/README.md` 若含本会话外的手工改动，先 `git diff` 核对再一起提交；不 `git add .`。

**自检**：`git status --short` 只剩 R1–R4 相关改动（即下文步骤产生的）。

---

# —— 执行步骤（按依赖顺序，每步可独立验收）——

## 步骤 1（P0 · 管线的根）：修 `capture_locator_snapshot` 多匹配保护 —— 让「序列选择」target 落盘（R2 主因）

**文件**：`batch_capture_replicate.py`（`::791-798`）、`capture_snapshot.py`（`::134-161`）

**现状**：
```python
# batch_capture_replicate.py:791-798
if target_locator is not None:
    try:
        target = capture_locator_snapshot(target_locator)      # ← 多匹配→strict-mode 抛错
        (capture_root / "target.json").write_text(...)
        closure = capture_selector_closure(target_locator, action_id)
        (capture_root / "selector_closure.json").write_text(...)
    except Exception:
        pass                                                  # ← 静默吞掉
```
`capture_locator_snapshot`（`capture_snapshot.py:134`）直接 `locator.evaluate(...)`，多元素 locator 触发 Playwright strict mode violation。

**做法（4 处）**：
1. `capture_snapshot.py`：给 `capture_locator_snapshot` 内部加多匹配保护。**归一化语义收紧**——只在 `count>1` 时取 `.first`（对多匹配零行为变化、返回仍非空），仅 `count==0`（真·无匹配）才返回 `None`：
   ```python
   def capture_locator_snapshot(locator: Any, coordinate_space: str = "page_viewport_css") -> DomNodeSnapshot | None:
       """多元素 locator 归一 `.first`，避免 strict-mode 抛错；仅无匹配时返回 None。"""
       count = locator.count()
       if count == 0:
           return None                 # ← 真·无匹配，调用方需处理
       if count > 1:
           locator = locator.first     # ← 多匹配归一，返回仍非空
       payload = locator.evaluate(...)
       ...
       return dom_snapshot_from_payload(payload, coordinate_space)
   ```
   > ⚠ **决定点：不要整体 `except→None`**。项目 `test_locator_factory_raising_is_swallowed` 的「吞异常」风格属于**调用点**（下方第 3 点），不是这个函数本身。此函数保持「有匹配就返回真实快照、无匹配才 None」的清晰契约，别把真实 evaluate 错误也吞成 None（会掩盖选择器 bug）。
2. **〔阻断级·必做〕审计全部 10 个调用方，补 `None` 分支**：返回类型从 `DomNodeSnapshot` 放宽到 `| None` 是**契约变更**，以下调用方目前都假设非空，必须逐一加 None 保护，否则步骤 1 一提交即 `AttributeError`：
   - **直接取 `.rect/.attributes` 的 root 快照（None 即崩，最危险）**：`capture_snapshot.py:180`、`:387`、`:528`（`root = capture_locator_snapshot(root_locator)` 后紧接属性访问）——None 时应 early-return 跳过该 region。
   - **成员/子项快照**：`capture_snapshot.py:187`、`:302`、`:547`（None 时 `continue` 跳过该成员）。
   - **batch 侧**：`batch_capture_replicate.py:649`（series item）、`:793`（target，见第 3 点）、`:872`（series root）、`:1355`（`meta_open_dom`）——各自按现有语义补 None 分支（跳过 / 不写文件 / 字段留空）。
   - **回归**：现有 `test/test_capture_snapshot.py:47` `test_capture_locator_snapshot_reads_a_real_playwright_element` 必须仍绿（单匹配路径不变）。
3. `batch_capture_replicate.py::793`：**不要**把 `capture_locator_snapshot` 返回 `None` 当成功；且**去掉 `except Exception: pass` 的静默**，改为：
   - 记录 `warning`（`print`/`pipeline_events`）「snapshot missing: {action_id} → 该 action 的离线转场无载体」；
   - `target.json` 缺失的 action，在 `pipeline_report.json` 标 `missing_target_evidence=true`（让 preflight/验收能显式看见，而不只是离线时才暴露）。
4. `capture_selector_closure`（`capture_snapshot.py:164`）同样加 `.first` 保护（它也是 `evaluate`，同源风险）；同步审计其调用方对 `None`/降级的容忍度。

**为什么这样修（关键）**：`target` 只用于**记录该 action 的 DOM 证据**与参与 transitions 过滤；对多匹配目标（序列列表）取 `.first` 的 DOM 是**合理降级**。修复后 `a_002_001` 的 `target.json` 落盘 → `document.targets` 含它 → `t_a_002_001` 进 `__REPLICA_TRANSITIONS__` → `s_003 → s_004` 可点 → **主路径「先布局后选序列」恢复**（配合步骤 3 的序列热区补齐）。

> **价值定位（即使方案 A 落地，本步骤仍必要）**：步骤 2 方案 A 落地后，序列切换走 branch route、不再必经 `s_003→s_004`，`t_a_002_001` 载体的**路由必要性下降**。但本步骤真正消除的是「**静默吞异常 → 快照缺失 → 报告无痕**」这个管线可见性缺陷本身（`missing_target_evidence` 显式化），并为**老 run / 方案 A 未落地的兼容路径**兜底。故与步骤 3 不矛盾：步骤 1 是「管线可见性 + 兼容兜底」，步骤 3 是「方案 A 未落地时的主路径 fallback」。

> ✅ **已确认（非假设）**：录制目标就是 `.first`——`out/zscloud/processed_script_zscloud.py:22`：`page1.locator("iframe").content_frame.locator("#HLeftThumnail li.ui-draggable").first.dblclick()`。故落盘的 `target.json` 对应 first 行是**正确**的，不存在「对应错误序列行」的风险；步骤 1 取 `.first` 与录制语义一致。

**测试**（`test/test_batch_capture_replicate.py`）：
- 新增 `test_capture_locator_snapshot_multimatch_returns_first`：构造 `count()>1` 的 fake locator，断言**返回非空快照且等于 `.first` 的快照**（不抛）。
- 新增 `test_capture_locator_snapshot_no_match_returns_none`：构造 `count()==0` 的 fake locator，断言返回 `None`（不抛）。
- 新增 `test_multimatch_action_target_json_is_written_not_silently_dropped`：走 `_capture`，多匹配下断言 `target.json` 存在 + `pipeline_events` 有 warning。

**验收**：
- `D:/Anaconda/envs/codegen-marker/python.exe -m unittest test.test_batch_capture_replicate -v` 全绿；
- `D:/Anaconda/envs/codegen-marker/python.exe -m unittest test.test_capture_snapshot -v` 全绿（`capture_locator_snapshot` 返回类型放宽为 `| None` 后，**capture_snapshot 侧也要跑**，不只 batch 侧）；
- 不破坏 `test_locator_factory_raising_is_swallowed`。

> **本步骤无需重录即可验证**（若老 run 无 target.json 则是历史事实，重录才有）。**修复后需重录一次**才有新 `target.json`——见步骤 6。

---

## 步骤 2（P1 · 布局独立性 · R1 治本）：布局作为「背景层切换」而非「状态跳转」

**文件**：`build_replica.py`（`_render_document` + `RUNTIME` + 新增辅助函数）、`replica_models.py`（可选字段）

**现状**：布局变化只能靠「跳到一个新状态帧」（s_001→s_002→s_003），而 s_003 是死胡同。背景是 `<img class="replica-bg" src="{asset}">`（因状态切换而变化）。

**核心设计（方案 A「背景层替换」，比「状态×布局正交快照」成本低、优先做）**：
把「布局」实现成**同状态内的背景 `<img>` 替换**，布局不产生新状态、不改变路由：

1. **模型扩展**（`replica_models.py`）：`ReplicaDocument` 增加 `layout_variants: dict[str, str]`（`{布局名: 背景asset相对路径}`）+ `default_layout: str`；**带默认值（空 dict / 空串）**，旧 manifest 解码兼容（参照已有 `series_list_full_asset_relpath` 的做法）。
2. **捕获扩展**（`batch_capture_replicate.py`）：对「序列布局切换」marker 组，捕获**多种布局的背景帧**存入 `layout_variants`：
   - 复用现有 topology 捕获：在布局 marker 的 after 快照里，对每个**可见布局选项**（`#cellStyle` 成员），顺序执行「点击该选项 → 等画布稳定（复用 `skills/zscloud-film-capture/references/known-issues.md` 的 1.5s DOM 重建经验）→ 截 viewer 背景图」；
   - 落盘为 by-hash 资产，`layout_variants["1*1"]=…/"2*2"/…`；
   - 保存 `default_layout`（入口状态实际布局 = 分享页默认，通常 2×2）。
   > **标记点**：此捕获是**对布局维度显式采集**，把「布局在选序列前被切到 1×1 并烘焙」从根上变成「布局是一组可选背景」。
   > ⚠ **这是 capture-time 新增交互，不是纯 build 改动**：「逐个点 `#cellStyle` 成员 → 截背景」是回放期新增的连点操作，可能重蹈 Z1/Z2 的回放竞态。必须自带自适应等待：①**布局浮层可见**（`#cellStyle` 成员可见性轮询）；②**每次切换后画布稳定**（`canvas.width>0` 轮询 + 1.5s 上限，**勿用纯固定 sleep 当唯一条件**）。并纳入步骤 6 的重录 preflight 一起验证。
   > ⚠ **捕获降级策略（防 Z1 复发）**：某个布局选项点击后画布无变化（如该布局在当前序列/当前层级下无内容、或切换失败）时，**该变体直接不入 `layout_variants`**（该选项在 UI 标记 `aria-disabled`），**绝不阻断整个布局 marker/整组捕获**——否则「某个操作失败 → 整组超时」的历史模式（Z1）会复发。仅当**全部**变体都失败时才把「序列布局切换」整个 marker 组降级为 partial（并在 `pipeline_report` 记录 `layout_capture_partial`）。
3. **构建扩展**（`build_replica.py::_render_document`）：
   - 布局按钮/浮层选项（`a_001_002`、及 `layout` region 其余成员）渲染为 `data-replica-layout="{variant_id}"` 的可点元素（复用 `_positioned_html` + `data-replica-overlay`，但加 `data-replica-layout` 而非 `data-replica-action`）——**不再跳转**；
   - 页面注入 `window.__REPLICA_LAYOUTS__ = {layout_id: url}`（类似现有 `__REPLICA_SERIES_ROUTE__`）；
   - **series 热区在布局切换时保持常驻**（不依赖当前布局变体）。
4. **RUNTIME**（`build_replica.py` 顶部）：
   - 在 click 监听里加 `data-replica-layout` 分支：`const url = window.__REPLICA_LAYOUTS__[id]; if (url) document.querySelector('img.replica-bg').src = url;` —— **只换背景，不导航**；
   - 其余热区（series / action / back）命中逻辑**完全不变**。

> **为什么方案 A 正确解决 R1**：「点序列=跳 1×1 帧」的机制是背景随状态变。方案 A 后**背景只在用户主动点布局时才变**，与序列切换彻底解耦——点序列只走 series route 换分支，背景保持当前布局。

**测试**（`test/test_build_replica.py` + `test/test_replica_runtime.py`）：
- build 单测：构造含 `layout_variants` 的 document，断言 `__REPLICA_LAYOUTS__` 注入 + 布局选项带 `data-replica-layout` + **series 热区与布局选项共存**。
- runtime 浏览器测试：入口页 → 点「*2x2」→ 断言 `img.replica-bg.src` 变为对应 hash（**背景变了**）→ 点序列项 → 断言跳转的是分支且**新背景仍为 2×2**（不是 1×1）——直接锁住「布局与序列解耦」。
- 兼容：无 `layout_variants` 的老 run（现有 s_001..s_007）**行为不变**（退化为现状，不回归）。

**验收（headless）**：入口先切 2×2 → 点序列 → 分支背景是 2×2；再切 1×1 → 点另一序列 → 背景 1×1。序列热区数量恒等。

> **替代方案 B（状态×布局正交快照）**：成本高（快照乘法级），仅当需要「布局成为全链路可回溯状态」（每布局都有一整条 viewer 流）时才用。本文默认 A；B 留作后续可选项，见 §5。
> **止血（可选，5 分钟级）**：若想先看效果，把入口基线布局直接设成 1×1（复用捕获的 1×1 背景），点序列无 2×2→1×1 跳变感。不解决 2×2 等需求，仅临时缓解。

---

## 步骤 3（P1 · 主路径恢复 · R2 组成 · **条件性兜底**）：`s_003`/`s_002` 补序列热区 + `s_003` 返回入口，消除死胡同

> ⚠ **与步骤 2 的主/备关系（先读）**：步骤 2 方案 A（布局=同状态背景替换、**不导航**）一旦落地，`s_002/s_003` 根本不会再被跳转到，「s_003 死胡同」随之消失——**此时本步骤大部分工作不必做**。因此本步骤定位为**方案 A 未落地 / 老 run（无 `layout_variants`）兼容**的 fallback，避免两套模型并存、重复投入。执行顺序：**先做步骤 2；仅当方案 A 暂缓或需兼容旧产物时，才执行本步骤**。

**文件**：`build_replica.py`（`_promote_series_regions_to_earliest_documents` 附近 + `_render_document`）

**现状**：`_promote_series_regions_to_earliest_documents`（`build_replica.py:1060`）**只**把 series region 提升到「同一 document_id 最早状态」（s_001）；`s_002`/`s_003` 没有序列热区，`s_003` 无任何出口。

**做法（3 处）**：
1. **扩展提升函数**：把「提升到最早状态」扩展为「提升到**该 document 的每个不含 series region 的 viewer 状态**」（s_001 已有时跳过、s_002/s_003 补上）。理由：1×1 就绪态（s_003）正是「布局已调、待选序列」——必须让序列热区在那里也在。
   - 注意：对 `layout_variants` 生效后（步骤 2），s_002/s_003 若成为「同一背景下的不同布局视图」，序列热区常驻即可，无需额外状态；仍建议保留 s_003 → s_001 返回入口作为兜底。
2. **给 s_003 加返回**：对「非入口、无后续 transitions」的状态，注入 `data-replica-back` 回「前一可交互状态」（s_001 或 s_002），复用现有 `_render_document` 的 back 逻辑（`build_replica.py:714`，只在 `back_target is not None` 时渲染）。判定「可交互」= 该状态有可点元素或本身是入口。
3. （配合步骤 1）确认 `t_a_002_001`（序列选择转场 → s_004）有载体：目标 target 落盘后可点；若仍缺，用 series region 生成 `data-replica-action` 兜底（见步骤 5 的取舍）。

**测试**：
- 扩展现有 `SeriesPromotionTests`（`test_build_replica.py:918-944`）：断言提升覆盖到 s_002/s_003（多状态时都带 series region）。
- 新增「s_003 有返回入口且返回正确、s_004 可达」的 build 断言 + runtime 浏览器测试（s_003 → 点返回 → 回 s_001）。

**验收**：headless 依次 入口→布局1×1(s_003)→序列热区可点→进分支；s_003 右上角「× 关闭」回 s_001。

---

## 步骤 4（P2 · 布局自由度 · R3）：`layout` region 全部成员可点化

**文件**：`build_replica.py`（`_render_document` 的 region 循环，`:613-679`）

**现状**：`layout` region 的**其余成员**走 `_positioned_html(member.dom)`（`:667`）→ 无 action → 纯装饰（`pointer-events:auto` 但无 handler）。

**做法**：
- 在 region 循环里对 `region.region_type == "layout"` 的成员，**不仅** `a_001_002`（录制动作），**所有可见成员**都渲染为 `data-replica-layout="{variant_id}"` 可点元素（配合步骤 2 的 `data-replica-layout` 处理）。
- `variant_id` 从 member 的文本/结构推断（`layout_1_2` → `1*2`；`*1 Shift+1` → `1*1`），映射到 `__REPLICA_LAYOUTS__`。**成员产物三态（明确全部失败面，避免实现卡在推断分支）**：
  - **可点**：`variant_id` 能推出 且 有对应捕获背景 → `data-replica-layout`（真交互）；
  - **disabled**：`variant_id` 能推出 但**无对应捕获背景**（如捕获降级丢失）→ `aria-disabled` + disabled 样式（**不假装可点**）；
  - **纯装饰**：`variant_id` **连都推不出**（命名不规则 / 结构无文本，如纯图标项）→ 不加 `data-replica-layout`、保持现状 `data-replica-overlay`（视觉在，不参与交互）。
- 布局选项 z-index 提升到 series-key 之上（见步骤 5 统一处理），确保「点布局」不误触「序列」。

**测试**：build 单测——s_002 浮层里 `layout_2_2` 等成员带 `data-replica-layout` 且映射到变体；无法映射的 disabled。

**验收**：headless 入口→点布局→浮层 ≥3 个可点；点 2×2→背景变 2×2。

---

## 步骤 5（P2 · 入口交互防误触 · R4）：布局按钮与序列热区去重叠 + 命中优先级统一

**文件**：`build_replica.py`（`:461` CSS 常量 + `:155-181` RUNTIME 命中顺序）

**现状**：入口布局按钮 (25,458,40×40) 与第 4 序列项 (19,389,314×83) 在 y∈[458,472] 重叠；series-key `z-index:2` > action `z-index:1`，runtime 先 series 后 action。

> ⚠ **先厘清机制（决定哪几条是必做）**：布局按钮与序列项是 `.overlay` 的**兄弟节点**（非嵌套）。兄弟重叠时点击命中哪个，**完全由 CSS z-index + 几何决定**，`event.target` 已经是最上层元素；RUNTIME 里 `.closest()` 的检查顺序只在元素**互相嵌套**时才改变结果。故对本场景：**第 2 点（抬高布局 z-index）才是真正修复**，第 1 点（去重叠）是加固，第 3 点（RUNTIME 重排）对纯兄弟重叠**冗余**——保留仅作防御性一致，勿据此过度改造 `build_replica.py:156-173` 的 handler。

**做法（3 条，第 2 条为核心，1/3 为加固）**：
1. **〔加固〕去重叠**：渲染 series 热区时（`_series_member_html` / `_render_document`），若某序列项 rect 与**可交互布局按钮** rect 相交，裁剪该序列项的命中区（分块：上方非重叠区保留、重叠带剔除），或把重叠带整体判给布局按钮。
2. **〔核心〕z-index 语义化**：布局/动作按钮 `z-index:3`（与 series 分离命名如 `.overlay>[data-replica-layout]{z-index:3}`），**高于** series-key（`:461` 目前 `z-index:2`）。理由：布局是「粘性按钮」（点了还要能再点），序列是「瞬跳项」；同像素上按真实交互意图让布局优先。这是兄弟重叠命中的**决定性因素**。
   > ⚠ **z-index:3 已被占用，需语义隔离**：`build_replica.py:466` 已有 `.overlay>[data-replica-visible]{...z-index:3}`（合成 Tags 入口）。两者**不同元素、不共存于同一像素**，功能上不冲突；但同为 3 会让未来调试混淆。明确约定：`data-replica-layout` → `z-index:3`（布局按钮）、`data-replica-visible` → 保持 3（合成入口），两者在同一 state 不同时出现；若将来需共存，布局按钮改 `z-index:4` 避平级。
3. **〔防御·非必需〕RUNTIME 命中顺序**：先匹配 `[data-replica-layout]`（布局按钮），再 `[data-replica-series-key]`，最后 `[data-replica-action]`（`build_replica.py:155-181`）。仅在未来出现**嵌套** overlay 时才有实际作用；纯兄弟重叠由第 2 点已解决。

**测试**：
- build 单测：构造重叠 rect，断言裁剪后序列项 bbox 不含重叠带；`data-replica-layout` 的 z-index 高于 series-key；runtime 常量里布局命中在前。
- runtime 浏览器测试：在重叠坐标 `(22, 463)` 附近点击 → 命中布局按钮（触发布局），**不**跳分支。

**验收**：headless 在 s_001 点布局按钮附近 → 布局菜单打开（不跳分支）；点序列 → 切分支（布局不变）。

---

## 步骤 6（P2 · 闭环验证 · 需重录）：重录中山 + 一键 rebuild 验收

**文件**：无代码改动（纯操作 + 验收）

**做法**：
1. 用当前工作区（含步骤 1–5 改动）重新录制中山共享链接：
   - 录制顺序**规范化**：布局动作**前置并独立标注**（它成为「起点参数」而非「中间步骤」），序列选择紧随（marker 分组粒度按「布局组 / 序列组」明确分开）。
   - 若布局 marker 有回放等待问题（历史 Z1 同源），在 `skills/zscloud-film-capture/references` 补「布局切完 1.5s 稳定」等待（`batch_capture_replicate.py:37-101,191-231` 目前对布局 marker 无特判——**这是 R 系列之外的历史残留，一并修**）。
2. `build_from_manifest` 一键 rebuild：`out/zscloud/runs/<新run>/replica/`。
3. headless 验收（`test_replica_runtime` 浏览器套件 + 手动坐标点击）：
   - 入口：先布局→选 2×2→仍能选序列（不卡死、可回退）；
   - 任意布局下点序列：**背景 hash 不变、仅换分支**；
   - 入口点「布局」附近不误触序列；
   - 布局浮层 ≥2 选项可点且各自生效；
   - 分支内再切序列、两步 Meta（历史 Z4/F5 已修，回归确认）。
4. `series_capture_manifest` 指标：`discovered_count`/`captured+partial` 守恒（不新增假分支），`overall_ok=true`。

**验收**：①–④ 全达成；历史功能（入口可点、分支可切、两步 Meta）无回归。

---

## 7. 万一出问题的排查点

| 现象 | 排查 |
|---|---|
| 重录后 `a_002_001` 仍无 target.json | 确认步骤 1 的 `.first`/容错生效；`pipeline_report` 无 `missing_target_evidence`；再查 `capture_locator_snapshot` 是否走新代码 |
| 点布局背景不变 / 变错 | `__REPLICA_LAYOUTS__` 注入是否正确；`layout_variants` 的 variant_id ↔ 捕获背景是否对应；`img.replica-bg` 选择器是否命中 |
| 布局切换后序列热区消失 | 方案 A 必须保证 series 热区与布局变体**解耦**（常驻 DOM）；查 `_render_document` series 块是否在布局 switch 后仍渲染 |
| s_003 仍死胡同 | 步骤 3 的返回入口是否注入；`t_a_002_001` 有载体了吗（步骤 1） |
| 入口误触序列 | 步骤 5 裁剪/层级/命中顺序是否生效；headless 在重叠坐标实测 |
| 老 run 行为变差 | 步骤 2/3/4 的兼容分支（无 `layout_variants`/无 series region）是否保留；对比 rebuild 前后交互表（问题文档 §2） |
| 布局捕获画布不稳 | 复用 known-issues 的 1.5s + 自适应等待（`canvas.width>0` 轮询），勿用纯固定 sleep 当唯一条件 |

## 8. 提交建议（按逻辑拆分）

1. `fix: capture_locator_snapshot multimatch first + drop silent pass`（步骤 1 + 测试）
2. `feat: layout as background-layer variants in replica`（步骤 2，若含捕获扩展则另拆 `feat: capture layout variants`）
3. `feat: promote series regions to post-layout states + s_003 back entry`（步骤 3 —— **仅在方案 A（步骤 2）落地后作为 fallback 的 build 部分**；若步骤 2 已让 s_002/s_003 不再被跳转，则本提交只保留「s_003 返回入口」这一兜底，序列热区扩展部分并入步骤 2 或跳过）
4. `feat: layout region members clickable (data-replica-layout)`（步骤 4）
5. `fix: dedupe layout/series hotspot overlap + unified hit priority`（步骤 5）
6. `chore: zscloud layout/series fix acceptance runs`（步骤 6 的运行记录/记忆更新）

每个提交 `git add` 精确文件；不 `git add .`；提交前跑对应测试全绿。

## 9. 概念速览（下家速读）

- `build_replica.py::build_replica` 主入口；`_render_document` 生成单 state 页；`RUNTIME` = `replica_runtime.js` 内容（含 `[data-replica-*]` 三路基线 + 本计划新增 `data-replica-layout`）。
- 背景机制：`<img class="replica-bg" src="assets/by-hash/xxx.jpeg">`，每状态一个 by-hash 资产。
- series 热区：`_series_member_html`（几何注入 + `data-replica-series-key` + `z-index:2`）；布局选项改造后走 `data-replica-layout`（`z-index:3`）。
- capture：`batch_capture_replicate._capture`（`::692`）主快照；`capture_locator_snapshot`（`capture_snapshot.py:134`）**步骤 1 重点**；`_capture_series_list_full`（`::415`）序列长图。
- region 类型：`capture_snapshot.py:_MARKER_REGION_CANDIDATES`（`序列布局切换`→`layout`，`序列选择`→`series`）。
- 本项目约定：**先修 skill / 共享逻辑，再生成代码**；**发现/激活/定位/层级必须同源**；**布局必须作为独立维度显式建模**（本次计划核心）。
- 目标产物：`out/zscloud/runs/<新run>/replica/index.html`；serve 用 `_tmp_serve_zs.py`（或独立后台 http server）。
