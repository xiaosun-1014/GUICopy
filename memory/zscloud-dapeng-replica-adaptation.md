---
name: zscloud-dapeng-replica-adaptation
description: 中山 zscloud 复刻跑通经验——Dapeng viewer 结构、WL/WW 失配、SPA popup 竞态、build 层 series region 提升修复
metadata:
  type: project
---

# 中山 zscloud 复刻适配（2026-08-17 跑通）

中山（`zscloud.zs-hospital.sh.cn/film/#/shared?code=<分享码>`）复刻管线从「4/4 partial 全失败」跑到「4 发现 3 captured、replica 可离线点序列切分支开 Meta」，期间踩并修复了 5 个问题：

## 1. Viewer = Dapeng（不是联影 RAF）——`#popTagText_*` 不存在

中山 viewer URL 是 `/viewer/2d/zh-cn/Dapeng/Viewer/Index?screenid=...`（**Dapeng** UI）。录自 8/13 的「窗宽窗位 WL/WW」marker 块用的是联影 RAF 的选择器 `#overlaycanvas-0_0` / `#popTagText_WL` / `#popTagText_WW`：
- `#overlaycanvas-0_0` 在 Dapeng **存在且可见**（viewer 家族共用的画布 id），「影像画布交互」可用。
- `#popTagText_WL/WW` 在 Dapeng **query 不到**（`[class*='popTag']` 总数 0）→ WL/WW 6 个动作每次空等 30–42s → 累加把 instrumented replay 拖满 **900s 超时被杀**（表现：`subprocess.TimeoutExpired ... timed out after 900 seconds` + 一串 `TimeoutError`），整体 capture fail 且没有 manifest。

**Why**：WL/WW 是固定操作 marker（不进 skill），失效只会在真实跑的时候以「每个动作超时」暴露，非常隐蔽。
**How to apply**：新站 viewer 先 headless 探测 `#popTagText_*` / `#overlaycanvas-0_0` / `#tagsBox` 等各家候选是否真实存在；失效的固定操作块直接删（连同 marker 注释），再重建 annotation。

## 2. 分享页 SPA 渲染竞态 → popup 偶发 30s 超时

`page.goto(share)` 后**零等待**直接 `expect_popup` + 点「查看影像」：分享页是 SPA，`domcontentloaded` ≠ JS 事件绑定完成，偶尔点下去 handler 未挂 → `expect_popup` 30s 超时（`TimeoutError ... while waiting for event "popup"`）→ 整 run network fail。探测时 headless 单独打开却每次都能弹（有人工时序）。

**Why**：人工录制有操作节奏，capture 无脑回放就把竞态放大。
**How to apply**：录制脚本 goto 后加稳定等待（`page.wait_for_timeout(2000)` + 注释「等 SPA 渲染完成，避免回放竞态 popup 失败」）。

## 3. 复刻构建缺口：series region 只注入「序列选择动作后」的状态 → 入口 viewer 无序列可点

zs 的序列列表（`#HLeftThumnail li.ui-draggable`）在 iframe viewer 内，capture 在「序列选择」marker 的 **after 快照（s_004）才注入 series region**，s_001～s_003 的 viewer document 没有 → 复刻入口（popup s_001）里**没有可点序列**，用户「无法点击切换」；4 个分支 `bviewer_b00X` 的 series region 则挂错 document（见 #5，此前 asset 上无法点）。ft 的 s_001 直接带 8 个 key（序列点击靠前）。

**修复（已提交到 build 层）**：`build_replica.py` 新增 `_promote_series_regions_to_earliest_documents(flow)` —— 把主路径里「首个含 series region 的 state」deepcopy 到「同一 document_id 最早出现的主路径 state」，使入口 viewer 从进入即可点序列（路由仍指向各 branch viewer/metadata）。分支状态（bviewer_/bmeta_/btags_）排除在外；已是 ft 场景（入口本就有 region）时不提升、无回归。配套测试在 `test/test_build_replica.py::SeriesPromotionTests`（4 个）。

**How to apply**：popup/iframe 型 viewer 序列点击靠后的站点都要靠这个提升；怀疑入口不可点时先查 manifest 里入口 state 的 document 有没有 `region_type=="series"`。

## 5. 分支 viewer 的 series region 挂错 document → 分支内无法再切序列（本次修复）

点序列进分支（`bviewer_b00X`）后，分支 viewer iframe **没有任何序列热区**——序列列表只是截图像素，点不动、也感知不到选项（曾误判「分支可点」）。根因：`batch_capture_replicate.py::_capture_viewer_topology` 把分支 series region 一律 append 到 `docs_out[0]`；popup 型 viewer（中山 Dapeng：`page`=分享页壳、viewer 在 `page1` 的 iframe）里 `docs_out[0]` 是**外层分享页 document** `d_p_000_root`，热区渲染进没人访问的主 index.html；用户实际到达的 iframe viewer document `{b}__d_p_001_f_001` regions=0。对照：主路径 s_004 的 series region 正确挂 `d_p_001_f_001`；`meta_open`（DICOM信息）target 也正确→所以「进分支→两步 Meta」能通而序列切换不能。FT 主页面即 viewer、`docs_out[0]` 恰好正确，故不炸。

**修复（build 层，未提交）**：`build_replica.py` 新增 `_reroute_branch_series_regions_to_viewer_documents(flow)`——把分支 viewer state 里挂在非 viewer document 上的 series region 迁移到该 state 内 **leaf id 与主路径 series document 一致**的 document（`{b}__d_p_001_f_001`）；源限分支 viewer、主路径与 bmeta/btags 不动、幂等。配套测试 `SeriesRerouteTests`（4 个：迁移/幂等/已挂对跳过/渲染到 viewer html 且外层无热区）。离线 rebuild（`build_from_manifest`，source sha 门禁）已把 c3a374 replica 重建，headless 全链路验收：入口 4 热区→分支 4 热区→分支内再切 b000→b001→两步 Meta(btags→bmeta) 全通。旧产物备份在 `c3a374/replica.pre-fix-20260817`。

**How to apply**：popup/iframe 型 viewer 的分支「进得去但列表不可点」先查分支 state 里 series region 挂在哪个 document（manifest `states[].documents[].regions`），不在 viewer iframe doc（leaf `d_p_001_f_001`）即属此类；build 层 reroute 以主路径 series document 为基准，离线可修、不必重跑 live capture。

## 4. 改脚本后重建 annotation 用 `build_annotations_from_source`

改 `processed_script_{医院}.py`（删 marker 块 / 加 wait 行）后 sha 变化，`replica_annotations.json` 需重建。用 `main_gui.build_annotations_from_source(new_source, [])`（基于 `agent.parse_markers` 重新扫 marker、行号自动更新、fresh UUID 亦可），写回 json 即与脚本 sha 强绑定。**注意 `ReplicaFlow` 构造位置**：字段顺序是 `...states, warnings, series_branches`，把 branches 传错到 warnings 位会让 `SeriesBranch` 混进 report JSON（`is not JSON serializable`）。

## 6. 布局与序列解耦（R1-R4，2026-08-17 本轮）——方案 A「背景层替换」

中山复刻原问题：布局被烘焙进「选序列后的每一帧」（所有 `bviewer_b00X` 都是 1×1）；点序列 = 整页跳 1×1 分支帧；`s_003` 是死胡同；布局浮层除录制的 `*1 Shift+1` 外不可点；入口布局按钮与第 4 序列项重叠误触。本轮修复（提交 `4e4b947`/`7122349`/`b7a03d9`）：

- **R1 治本（方案 A）**：布局实现为**同状态内背景 `<img class="replica-bg">` 替换**，不产生新状态、不改变路由。build 注入 `window.__REPLICA_LAYOUTS__`（布局 id→by-hash 相对 URL）；布局按钮渲染 `data-replica-layout`（RUNTIME 点击只换背景 src，series 热区常驻与其解耦）；series 命中逻辑完全不变。
- **R2（a_002_001 target 缺失→s_003 死胡同）**：`capture_locator_snapshot`/`capture_selector_closure` 多元素 locator 归一 `.first`（不再 strict-mode 抛异常被 `except:pass` 吞），`count()==0` 返回 None；全部调用方补 None 分支；`_capture` 里 target 缺失显式记 warning；`build_flow_from_snapshots` 把 `missing_target_evidence:<action_id>` 进 `flow.warnings`（pipeline_report 可见）。**重录后 a_002_001 的 target.json 正常落盘**。
- **R3 布局成员三态化**：`_render_document` 对 layout region 成员按「variant 可推+有背景 → `data-replica-layout` 可点 / variant 可推无背景 → `aria-disabled` / 推不出 → 纯装饰」处理；legacy（无 `layout_variants`）不注入不回归。`_promote_series_regions_to_earliest_documents` 扩展为提升到「每个无 series region 的主路径状态」。
- **R4 防误触**：布局按钮 `z-index:3` 高于 series-key `z-index:2`（重叠命中优先布局）；series 行与可点布局按钮 rect 相交时裁剪重叠带（`_series_member_html` 的 `clip_rect`）。
- **步骤 3 死胡同兜底**：无 out transition 且有可交互内容的非入口状态注入 `data-replica-back` 回前一可交互状态（仅死胡同时，普通中间态行为不变）。

**重录验收（run `20260817T200555Z-layout-fix`）**：`discovered=4 / 3 captured + 1 partial / 0 failed / reached_end`；**无 `missing_target_evidence`**（a_002_001 target 落盘）；headless 全链路：主 viewer 3 热区 → 点序列跳分支 → 分支内层 viewer iframe 4 热区可再切 → 两步 Meta。序列数量守恒（不新增假分支）。

> ✅ **布局变体采样已闭环（2026-08-17 补充修复 1877f72）**：re-review 发现并修复 3 个缺陷后，真实重录（run `20260817T214500Z-layout-fullfix`）采到 **`1*1` 变体并注入 `__REPLICA_LAYOUTS__`，点 1*1 真实切换背景（不导航）、序列热区随后仍可切**。三缺陷：
> 1. **frame 作用域**（缺陷 A）：`page.locator("#cellStyle")` 跨 frame 查不到——布局浮层在 viewer iframe 内；改用 `_find_viewer_frame(page)`（持有 canvas 的 Frame，有 `.evaluate/.locator`）作为采样上下文。
> 2. **title 推断**（缺陷 F）：Dapeng 布局 button 文本为空、规格在 `title`（`title="2*2 Shift+4"`）；改用 `get_attribute("title")`/`aria-label` 优先、`innerText` 兜底。
> 3. **relpath rebase**（缺陷 G）：`layout_variants` 的 phase 级资产（`snapshots/a_001_001/after/assets/by-hash/...`）需在 `_load_snapshot_state`/`_load_branch_topology` rebase 到 capture 相对，否则 build 复制时找不到资产、`__REPLICA_LAYOUTS__` 不注入。
> **已知剩余**：真机只采到 `1*1`（布局浮层打开时点击 2*2/3*3 等后 canvas 指纹未变化 → 被降级跳过），build 侧渲染 `1*1` 可点 + 其余 `aria-disabled`（不假装可点）。若要做全部布局可点，需在采样时对「点击后 canvas 指纹不变」的选项放宽判定（如确认切换到多视图布局时画布是否真不变），或接受当前 1*1 可点。
> **How to check**：`test_nested_frame_series_uses_scroll_harvest`（scrollTop 恢复 `4` 断言）是既有失败（干净基线也挂 `0 != 4`），与 R 系列无关，未修。

## 已跑通的中山产物

`out/zscloud/runs/20260817T053551Z-c3a374/replica/index.html`（4 分支 3 captured 1 partial(b000 `no_visual_change_evidence` 无害降级)；#5 修复后 headless 端到端全链路验收通过：查看影像 → 点序列 key → 切分支 → **分支内再切序列（b000→b001）** → 两步 meta_open(Tags)）。

> 复刻验证注意：分支内序列热区必须数 `iframe` 里 `[data-replica-series-key]` 元素（≥1）才算可切，光凭 manifest 里 region 存在会误判「分支可点」（#5 教训）。
> 复刻路径注意：viewer 系列热区在内层文档 `<state>/documents/<doc_id>/index.html`（非顶层 state index.html）；验收要先走对嵌套路径。
