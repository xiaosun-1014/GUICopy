# 多序列 Replica 扩展代码审阅

日期：2026-08-14  
审阅结论：**Request changes**。当前单页 happy path 已经能够完成真实 capture → flow → builder → 离线导航，但同名序列的稳定身份匹配仍会采错分支，嵌套 iframe 的扩展产物也会挂到错误 document。按计划的完成定义，目前不能判定 Phase 10 验收通过。

## 审阅范围

- 计划与设计：`docs/superpowers/plans/2026-08-14-multi-series-replica-expansion.md`、`docs/superpowers/specs/2026-08-14-multi-series-replica-expansion-design.md`、`docs/REPLICA_DESIGN.md`。
- 生产代码：`batch_capture_replicate.py`、`capture_snapshot.py`、`capture_readiness.py`、`replica_models.py`、`build_replica.py`、`pipeline_validation.py`、事件、GUI、报告和 orchestrator 相关改动。
- 测试与 fixture：本次修改/新增的 multi-series、manifest、builder、runtime、pipeline、事件与报告测试。
- 本审阅仅新增本文件，没有修改生产代码或测试。

## 问题清单

### [P0] 1. 同名序列会按次级共享属性或文本误匹配，多个 branch 实际可能都采到第一个序列

`capture_snapshot._series_identity()` 明确按 `data-series-uid → data-series → data-uid → value → id` 选择第一个非空属性作为主身份（`capture_snapshot.py:218-231`），但重新定位时没有遵守这个主身份：

- `_matches_descriptor()` 对 `stable_attributes` 中**任意一个**属性相等就返回 true；全部不等时还会回退到同名文本（`batch_capture_replicate.py:292-300`）。
- `_series_descriptor_matches()` 同样以任意共享属性或相同 label 判相等（`batch_capture_replicate.py:303-317`）。

fixture 中两个 “Coronal MIP” 分别有 `data-series-uid=uid-1992/uid-2047`，但共享 `data-series="Coronal MIP"`。扫描第二个 descriptor 时，第一个 row 因共享 `data-series` 已经命中；如果次级属性也不共享，相同文本仍会命中。最小复现的当前输出为：

```text
{'row_A_matches_descriptor_B': True, 'descriptor_A_matches_B': True}
```

结果是 branch B 可能保存 branch A 的 Viewer 截图和 Metadata，却仍被标成 `captured`。现有真实链路测试只断言两个 series key 能导航到不同 URL（`test/test_multi_series_capture.py:413-434`），没有断言两个分支的 viewer marker、截图 hash、SeriesNumber/UID hash 不同，所以未发现语义串支。

建议：descriptor 必须记录并使用单一 `identity_kind/identity_value`，或严格按 `series_key` 对应的最高优先级属性比较；只有 descriptor 完全没有稳定属性时才能使用“规范化文本 + occurrence”。增加两个同名、不同 UID、其余属性共享的激活测试，并断言每个 branch 的 Viewer/Metadata 独有值。

### [P1] 2. 自动扩展把嵌套 iframe 的 series/Metadata region 固定挂到根文档，坐标系和 DOM 所属文档错误

普通 marker 捕获已经会根据 locator 所属 frame 选择 `target_document`（`batch_capture_replicate.py:571-607`），但 series expansion 没有复用该逻辑：

- `_capture_viewer_topology()` 固定将 series region 追加到 `docs_out[0]`（`batch_capture_replicate.py:716-727`）。
- `_capture_metadata_transaction()` 固定将 Metadata region 追加到 `docs_out[0]`（`batch_capture_replicate.py:1150-1157`）。
- `capture_locator_snapshot()` 返回的是 locator 自己 frame 内的 viewport 坐标；把它挂到根 document 后，builder 会按根文档坐标渲染。

这对 cxhospital 的 `#iframe → iframe[name="imageFrame"]` 两层结构是直接错误：序列列表和 Metadata 面板的语义 DOM 会覆盖在主页面错误位置，而不是内层 frame。Phase 1 的 nested-frame 测试只覆盖普通 `LiveCaptureSession._capture()`，Phase 5–7 的真实 contract 使用直接位于页面中的 fixture，没有覆盖自动 expansion 的 nested/popup 产物链。

建议：`capture_page_topology()` 返回 frame→document 的可复用映射，或增加统一的 owning-document resolver；series region、Metadata region、Meta open target 都必须挂到 locator 实际所属 document。增加两层 iframe 的真实 expansion → build → offline click 测试，并校验 region.document_id、坐标空间和可点击位置。

### [P1] 3. Metadata open target 被同一个实例追加到所有 Viewer documents，嵌套页面会生成重复且错位的触发器

`_synthetic_meta_open_target()` 把 `document_id` 固定为 `documents[-1]`（`batch_capture_replicate.py:2600-2622`），随后 `_build_branches_into_flow()` 又把同一个 target 追加到每个 viewer document（`batch_capture_replicate.py:2492-2499`）。builder 渲染时按每个 `document.targets` 直接输出 target，并不依据 `target.document_id` 过滤（`build_replica.py:420-449`）。

多 iframe branch 因而会在主文档、外层 frame、内层 frame重复渲染同一个 Metadata 按钮；rect 仍是原 locator 所属 frame 的局部坐标。这可能遮挡真实 UI，也会让用户点击到错误 document 中的伪触发器。现有 Metadata E2E 使用 `.first` 点击（`test/test_multi_series_capture.py:545-547`），且 fixture 没有 iframe，因此无法发现重复。

建议：捕获时持久化 Meta open locator 的 owning document identity；remap 后只把 target 挂到对应 document，并新增“全 state 仅一个 meta-open action target”的嵌套 iframe 断言。

### [P1] 4. 激活后虽在 readiness 中重新解析 root，实际 region capture 与 restore 仍继续使用激活前的旧 root

`_wait_for_series_ready()` 每轮会从 recipe 重建 series root，这是正确的（`batch_capture_replicate.py:956-1019`）。但成功后 `_capture_viewer_topology()` 仍收到激活前保存的 `root`（`batch_capture_replicate.py:781-815,835-838`），per-branch restore 也继续使用同一个旧 root（`batch_capture_replicate.py:894-918`）。

真实 Viewer 如果切换序列时重建 iframe/root，readiness 可以成功，但 series region harvest 会在 stale locator 上失败并被静默降级为旧 `descriptor.member_id`（`batch_capture_replicate.py:721-727`）；restore 随后也无法恢复选中态/scroll。这样 branch 可能有 Viewer 截图，却没有可路由的 series region。

此外，成功路径在 `else` 中恢复一次并 `return`，随后 `finally` 仍会再次调用 `_restore_hub_state()`（`batch_capture_replicate.py:888-918`），造成同一序列被重复激活和额外副作用。

建议：在 Viewer capture 和 restore 前都从 recipe/pages 重新解析最新 root；用一个带状态标志的 `finally` 完成恰好一次恢复。增加 iframe/root replacement 测试，并断言 restore 调用次数为 1。

### [P1] 5. Metadata UID 只计算 hash，没有与目标 descriptor 校验，错误或陈旧面板仍会被标为 captured

`_capture_metadata_transaction()` 接收 `descriptor` 参数（`batch_capture_replicate.py:1092-1100`），但函数内没有使用它。捕获逻辑只从面板提取 UID 并记录 hash（`batch_capture_replicate.py:1142-1183,1212-1246`），没有比较目标序列身份，也没有按计划使用 UID hash 升级/校验 descriptor 关联。

因此以下情况仍会返回 `ok=True`：序列点击命中了同名的错误 row、Metadata 面板停留在上一序列、面板只有 Study 级内容但被当成 Series 级。这个缺口会掩盖 P0#1，而不是把 branch 降为 partial。

建议：明确 identity 校验顺序：SeriesInstanceUID hash 优先，其次 SeriesNumber/SeriesDescription；无法证明属于目标序列时标 partial 并写安全 warning。测试必须故意返回上一序列 Metadata，断言不能 captured。

### [P2] 6. discovery 对无稳定属性的同名序列先去重后编号，occurrence fallback 实际不可达；滚动后也没有稳定等待

`discover_series_candidates()` 先以 `("text", normalized_text)` 作为 dict key 去重（`capture_snapshot.py:271-301`），两个无稳定属性且同名的逻辑序列会在这里合并。后面的 `same_name_index` 只对已经去重后的记录编号（`capture_snapshot.py:316-340`），无法恢复被合并的第二项。

另外，每次改变 `scrollTop` 后立即读取下一窗口（`capture_snapshot.py:302-309`），没有计划要求的 signature-based DOM 稳定等待或“连续两步无新增”终止。异步虚拟列表可能读取旧窗口、漏项，甚至错误报告 `reached_end=true`。

建议：以当前可见窗口 occurrence/位置构造临时 identity，再跨窗口合并；滚动后等待 item signature 稳定。新增“无任何稳定属性的两个同名项”和“异步更新、复用 DOM 节点”的 fixture。

### [P2] 7. schema v2 validation 仍未强制版本/branch 完整性契约

`ReplicaFlow.from_dict()` 会在 schema v1 时丢弃 series 字段（`replica_models.py:359-370`），但 `to_dict()` 无条件序列化所有字段，`validate_manifest()` 也只在 series 数据存在时校验内容，没有要求 `schema_version == 2`（`pipeline_validation.py:151-198`）。当前最小复现中，schema v1 + 非空 series branches/expansion 的 validation 结果仍是 `success`；写出再读回会静默丢失这些字段。

同一区域还没有验证 `branch_id` 唯一、`source_member_id` 可关联、`partial` branch 具有 Viewer state，以及模板要求 Metadata 时 captured branch 具有 metadata/return state。损坏或手工构造的 manifest 可能通过 validation，随后在 builder/runtime 才表现为覆盖、禁用或悬空 route。

建议：validation 明确拒绝 v1+series data，并补齐 branch ID、source member 和 captured/partial state 完整性检查；增加直接 write/read round-trip 失败测试。

### [P2] 8. iframe 截图改为完整 HTML element screenshot，与记录的 frame viewport 尺寸不一致

`capture_page_topology()` 对 child frame 使用 `frame.locator("html").screenshot()`（`capture_snapshot.py:552-572`），但 `ReplicaDocument.viewport` 仍记录 `innerWidth/innerHeight`。当 frame 文档高于 viewport 时，element screenshot 可能是完整滚动高度；builder 再把它拉伸到 viewport，背景与 `frame_viewport_css` overlay 会发生纵向缩放错位。

建议：恢复 owning Page 的 iframe viewport clip（含嵌套 offset 与 CSS scale），或把 screenshot 尺寸/滚动语义一起建模。增加长 frame、边框和嵌套 iframe 的像素尺寸与 overlay 对齐测试。

## 计划验收缺口

- Phase 0.2 真实站 Spike 尚未形成可审阅完成证据。设计文档状态仍写“Phase 0.1 设计冻结”，第 8 节也标明“Phase 0.2 待实测校准”；`viewers.yaml` 没有本次改动。因而 click/dblclick、真实 iframe rebuild、Metadata 语义和虚拟列表节点复用仍未在 cxhospital/uicloud 上闭环。
- 当前 nested fixture 只进入普通 marker 捕获测试，没有进入自动 expansion → branch artifacts → flow → offline runtime 的整链。

## 验证结果

- `py_compile`：`batch_capture_replicate.py`、`build_replica.py`、`capture_snapshot.py`、`capture_readiness.py`、`replica_models.py`、`orchestrator_events.py`、`pipeline_validation.py` 通过。
- `test.test_replica_manifest`：6/6 通过。
- series validation/privacy/report focused tests：20/20 通过。
- `test.test_orchestrator_events`：45/45 通过。
- 两个真实 capture → offline E2E 分别通过：双 series URL 导航、Metadata open/close 返回。
- `test.test_replica_regions + test.test_multi_series_budget` 输出 13/13 `OK`，但组合命令在 90 秒上限后未及时退出，被外层 runner 终止；不能把进程退出状态记为完整 PASS。
- 大 focused suite 在 120 秒上限内没有完成，未取得完整回归结果。

## 建议修复顺序

1. 先修 P0#1，并让同名序列测试断言 branch 独有 Viewer/Metadata 内容，而不只断言 URL。
2. 统一 owning-document 解析，修复 P1#2/#3，再用两层 iframe 跑真实整链 E2E。
3. 在 capture/restore 前重解析 root，消除双恢复，并补 Metadata identity 校验。
4. 修 discovery 弱身份与异步虚拟列表，再收紧 schema validation 和 frame screenshot 契约。
5. 最后执行计划 Phase 10 focused suite、全量 unittest、真实站 smoke 和离线零外网验证。

