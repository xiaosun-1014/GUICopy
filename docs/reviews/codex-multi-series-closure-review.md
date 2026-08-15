# Codex closure review — 多序列 Replica P0/P1 整链复核

结论：**Request changes / 上一轮问题已有实质修复，但尚不能认定 P0/P1 全部关闭。** P0#2 的原子 document/page remap，以及 P0#3 的稳定属性 route binding 核心路径已经落地；P0#1 的真实 capture 主路径也已开始保存 series/Metadata region 和非空 Metadata trigger DOM。不过，Metadata loader 的 fallback 路径写错、所谓端到端 contract test 只点击一个 series 且不点击 Metadata、readiness 仍复用 root Locator、per-branch restore 不验证恢复结果、max-series 尾项没有进入返回 outcomes/branch flow、live event 测试没有真实覆盖 partial/failed，隐私测试也没有证明 raw UID 不进入最终 served Metadata HTML。因此本轮建议继续阻断合并，完成下列残留项后再验收。

## Sources consulted

- 基线与变更面：`HEAD 6782bbf`；`git status --short`、`git diff --stat 6782bbf`、`git diff --check 6782bbf`。
- 上一轮审阅：`docs/reviews/codex-multi-series-final-review.md`。
- 规范与契约：`docs/superpowers/plans/2026-08-14-multi-series-replica-expansion.md`、`docs/superpowers/specs/2026-08-14-multi-series-replica-expansion-design.md`、`docs/REPLICA_DESIGN.md`、`docs/MULTI_HOSPITAL_REPLICA_RUNBOOK.md`、`docs/PIPELINE_RUNBOOK.md`。
- 重点生产实现：`batch_capture_replicate.py`、`capture_snapshot.py`、`capture_readiness.py`、`build_replica.py`、`replica_models.py`、`replay_helpers.py`、`orchestrator_events.py`、`pipeline_orchestrator.py`、`pipeline_validation.py`、`pipeline_report.py`、`main_gui.py`。
- 重点测试：`test/test_multi_series_capture.py`、`test/test_multi_series_budget.py`、`test/test_build_replica.py`、`test/test_replica_runtime.py`、`test/test_capture_readiness.py`、`test/test_orchestrator_events.py`、`test/test_pipeline_orchestrator.py`。
- 静态验证：
  - `D:/Anaconda/envs/codegen-marker/python.exe -m py_compile batch_capture_replicate.py build_replica.py capture_snapshot.py capture_readiness.py replica_models.py orchestrator_events.py`：PASS。
  - `git diff --check 6782bbf`：PASS；只有工作区 LF→CRLF 提示。
- 动态验证尝试：`D:/Anaconda/envs/codegen-marker/python.exe -m unittest test.test_multi_series_capture test.test_multi_series_budget test.test_build_replica test.test_replica_runtime -v`。测试发现并启动了 33 项，但 31 项在 setup/cleanup 阶段因受控 Windows 环境拒绝 Playwright subprocess pipe 或 `TemporaryDirectory` 访问而报 `PermissionError/WinError 5`；两个无需这些能力的测试通过。此结果不能归因于实现，也不能作为 E2E PASS。

## Concrete findings

### 1. P0#1 — ⚠️ 部分关闭：真实 capture 主路径已保存 region，但 loader fallback 和真实点击契约仍未闭环

- 已修复的主路径是真实存在的：`capture_one_series()` 通过 `_capture_viewer_topology()` 调用 `capture_page_topology()`，随后调用 `_capture_series_region()`；后者使用共享 `discover_series_candidates()` 生成 members/evidence，并把 `region_type="series"` 的 `InteractionRegion` 追加到 Viewer document（`batch_capture_replicate.py:665-729,827-830`）。这已经关闭了上一轮“真实 Viewer document 永远空 regions”的主缺陷。
- Metadata capture 也不再只落孤立 JSON：`_capture_metadata_transaction()` 在写 topology 前把 sanitized full outerHTML 合成为 `region_type="metadata"` region（`batch_capture_replicate.py:1036-1101,1133-1146`）；`_metadata_panel_snapshot()` 提供非空 text、`role="dialog"` 和完整 outerHTML，使 builder 的 Metadata panel 识别条件可满足（`batch_capture_replicate.py:1892-1921`）。
- Metadata open target 不再固定 `dom=None`：capture 在点击前调用 `capture_locator_snapshot(opened)` 并写 `meta_open_target.json`（`batch_capture_replicate.py:1073-1080,832-838`）；loader 反序列化该 DOM（`batch_capture_replicate.py:1988-1998`），flow builder 再把它传给 `_synthetic_meta_open_target(..., dom=snapshot.meta_open_dom)`（`batch_capture_replicate.py:2262-2270,2372-2394`）。`capture_locator_snapshot()` 本身会经统一 sanitizer 生成 DOM（`capture_snapshot.py:121-161`），所以成功捕获时 target DOM 非空且可进入通用 target render 路径。
- 但 loader 的独立合成 fallback 实际读错目录。writer 写的是 `branch_dir/metadata/metadata_rows.json`（`batch_capture_replicate.py:1058-1093`），loader 却检查 `branch_dir/metadata_rows.json`（`batch_capture_replicate.py:1970-1977`）。因此如果旧/部分真实产物的 topology 没内嵌 region，loader 声称的 rows/outerHTML→region 修复不会运行；当前真实主路径之所以能工作，是因为 region 已提前写进 `metadata/topology.json`，不是 loader fallback 生效。
- `SeriesContractEndToEndTests` 确实存在，并用真实 `finalize_series_branches()` 目录产物进入 `_load_series_branch_snapshots()`、`_build_branches_into_flow()` 和 `build_replica()`，不是手工构造理想 branch documents（`test/test_multi_series_capture.py:259-330`）。这是重要进展。
- 但该 contract 的覆盖声明强于实际断言：它虽然要求至少两个 captured branch，却只对 `captured[0]` 点击一次（`test/test_multi_series_capture.py:332-341`）；entry 又正是从第一个 snapshot 手工包装出来（`test/test_multi_series_capture.py:307-325`）。它没有点击第二个不同 branch，没有点击 Metadata open target，没有断言 Metadata state 可见完整 DOM/可关闭返回，也没有断言 `meta_open_dom` 非空。另一个测试只检查 metadata region 存在和 entry id 可解析（`test/test_multi_series_capture.py:344-362`），并未执行 builder→浏览器 Metadata 点击链。
- 结论：真实 series capture 与 Metadata region 的生产主路径已修，但 loader fallback 和“真实产物→多 branch 点击→Metadata 点击/返回”的验收链仍不完整，故只能判部分关闭。

### 2. P0#2 — ✅ 已关闭：要求列出的 branch topology 引用已原子 remap

- `_branch_topology()` 先建立 document/page 映射，再一次性构造新 pages/documents；它更新 `ReplicaDocument.document_id`、`parent_document_id`、`page_id`，更新 `ReplicaPage.page_id`、`entry_document_id`，并更新每个 region/target 的 `document_id`（`batch_capture_replicate.py:2321-2369`）。这覆盖了本项要求列出的所有引用。
- `_build_branches_into_flow()` 对 Viewer 和 Metadata 两类 state 都统一调用该 transform（`batch_capture_replicate.py:2251-2259,2281-2295`）。builder 在构造 transition URL 时用 `target_page.entry_document_id` 对目标 state documents 做严格 `next(...)` 解析（`build_replica.py:653-668`）；contract test 的 `build_replica()` 能到达这一严格路径，且另有直接断言 remapped entry id 指向真实 document（`test/test_multi_series_capture.py:329-330,358-362`）。
- 小缺口不影响本项关闭结论，但应列入建议：`ReplicaPage.opener_page_id` 没有随 `page_id` 一起 remap（`batch_capture_replicate.py:2343-2352`）。多 popup branch 可能保留旧 opener page id；当前本项明确要求的 parent 是 document parent，已正确更新。

### 3. P0#3 — ✅ 已关闭（稳定属性主路径）：route 不再依赖跨 snapshot member id 相等

- capture 阶段重新在每个 branch 的 Viewer series region 内用稳定属性/series key/normalized label 匹配 descriptor，并返回该 document 自己的 member id（`batch_capture_replicate.py:303-317,665-701`）。这消除了“直接沿用 hub member id”的唯一绑定路径。
- builder 保留 local `source_member_id` 快路径，但新增了 `series_route_by_identity`：route map 以 branch `series_key` 为 identity，render 时由 member 自己的稳定属性按同一优先级提取 identity，再绑定 slug（`build_replica.py:20-35,442-467,765-803`）。因此 data-series-uid/data-series/data-uid/value/id 存在时，不需要任何跨 snapshot member id 相等假设。
- served HTML 与 route map 仍只写 `series_key_slug()`，不写 raw identity（`build_replica.py:676-711,780-803`）。
- 已知边界：完全没有稳定属性的 text-only series member 会让 `_member_series_key()` 返回 None；其 fallback `series_key` 又包含 capture-local document id（`capture_snapshot.py:218-231,324-340`），builder 没有 text+occurrence identity fallback。这个边界属于尚未覆盖的弱身份场景，但不推翻本项“稳定属性 route binding 已落地”的关闭结论。

### 4. P1#4 — ⚠️ 部分关闭：证据规则已修，但“每 poll 重解析 locator”仍不完全成立且无针对性回归

- `_evidence_satisfied()` 现在排除 `screenshot_nonblank` 并要求至少两类 core evidence（`batch_capture_replicate.py:320-327`）。
- `_collect_evidence()` 的 `name_match` 改为读取 Viewer 当前显示 identity，而不是 target row 自身 label（`batch_capture_replicate.py:448-480,1011-1015`）；DOM fingerprint 两次采样也明确比较 `second == first`（`batch_capture_replicate.py:1021-1028`）。这些直接关闭了上一轮的恒真 name_match 和“只检查非空 fingerprint”问题。
- `_wait_for_series_ready()` 每个 poll 都重新找 Viewer frame，并重新生成 target row Locator（`batch_capture_replicate.py:926-964`）；retry 也重新定位 row（`batch_capture_replicate.py:814-825`）。
- 但它仍把 activation 前解析出的 `root_locator` 传入整个 wait loop，并在每次 poll 上从该缓存 root 派生 row（`batch_capture_replicate.py:791-813,926-950`）。如果 iframe/series root 自身被替换，而不仅是 row 虚拟化，新的 row 仍无法从旧 root Locator recipe/context 重新解析；这没有完全满足“每 poll/retry 重建 page/frame/root locator”的强契约。
- 测试中没有直接覆盖 `_evidence_satisfied()` 的一类/两类边界、旧 Viewer `selected+dom_stable` 假阳性、iframe/root replacement 后重新解析，或证明 row label 不再充当 name_match；`test/test_capture_readiness.py` 测的是底层 hash/signature helper，不是组合选择判定。
- 结论：核心逻辑修复有效，但 locator 生命周期和针对性测试仍不足，判部分关闭。

### 5. P1#5 — ⚠️ 部分关闭：finally/reload 已可达，但恢复成功没有被强制验证

- `capture_one_series()` 现有 per-branch `try/finally`，在所有成功/partial/failed 路径调用 `_restore_hub_state()`（`batch_capture_replicate.py:740-889`）。Metadata 成功路径的 close click 后会调用 `_wait_for_metadata_hidden()`，hidden=false 会把 branch 降为 partial（`batch_capture_replicate.py:1103-1120,1148-1154`）。
- `HubUnrecoverableError` 已有真实 raise 点：series root 无法解析、target row 丢失等会 raise（`batch_capture_replicate.py:785-803,814-820`）；finalizer 在连续 hub failures 后执行一次 reload，并重建 pages、series locator/root，再重试 capture（`batch_capture_replicate.py:1313-1359`）。这关闭了上一轮“异常从不 raise、reload 永不可达”的问题。
- 但 per-branch finally 吞掉 `_restore_hub_state()` 的所有异常且不检查结果（`batch_capture_replicate.py:881-888`）。`_restore_hub_state()`/`_open_metadata_if_needed()` 关闭初始为 hidden 的 panel 时只 click，不调用 hidden wait，也不报告失败（`batch_capture_replicate.py:1518-1562`）。因此 Metadata stabilize timeout/异常提前 return 后，finally 虽“尝试恢复”，仍可能把未隐藏 panel 留给下一 branch，且该 branch 不一定被标记 restore failure。
- reload 测试只 mock `capture_one_series` 连续 raise，再断言 manifest 的 `reloaded` 和 warning（`test/test_multi_series_budget.py:170-195`）；它没有让真实 page/frame/root 在 reload 后替换并证明旧 Locator 未复用。也没有测试 close 不生效时下一 branch 仍从 clean hub 开始。
- 结论：事务结构与 reload 可达性已修，强制恢复/验证仍未闭环，判部分关闭。

### 6. P1#6 — ⚠️ 部分关闭：aggregate 计数守恒已修，但 max 尾项没有进入返回 branch 集合

- `discovered` 现在唯一取 `len(descriptors)`（`batch_capture_replicate.py:1268-1277`）；manifest 用 captured/partial/failed/skipped 四桶做 conservation，`overall_ok` 要求 reached_end、captured==discovered 且其他桶为零（`batch_capture_replicate.py:1383-1424,1466-1492`）。`_expansion_evidence()` 也优先使用 manifest 的 absolute discovered，并把 skipped 保守映射为 partial（`batch_capture_replicate.py:2397-2431`）。这关闭了 aggregate/report 缩分母问题。
- budget 在循环中耗尽时，剩余 descriptors 会进入 `outcomes` 的 `skipped_budget`（`batch_capture_replicate.py:1288-1298`）。
- 但 `max_series` 尾项是在循环结束后仅追加到局部 `manifest_outcomes`（`batch_capture_replicate.py:1387-1398`），函数最终仍返回原 `outcomes`（`batch_capture_replicate.py:1420-1425`）。这些尾项没有 descriptor/status branch directory；loader 又只从 branch directories 构造 `SeriesBranchCapture`（`batch_capture_replicate.py:1944-2023`）。因此 flow 的 `series_branches` 不含 max-limit skipped terminals，只有 aggregate `SeriesExpansionEvidence` 保住分母。
- 被跳过的 branch 也没有对应 `series_capture_partial/failed` terminal event，因为 terminal event 只在实际 loop outcome 后发射，且映射不含 skipped（`batch_capture_replicate.py:1368-1381`）。
- 结论：计数与 `overall_ok` 已诚实，但“每个 discovered descriptor 都保留一等终态/branch”尚未全链完成，判部分关闭。

### 7. P1#7 — ⚠️ 部分关闭：生产者已接入，但“7 种真实事件”测试名不副实

- live explorer 现在真实发射 discovery started/discovered、per-branch started、captured/partial/failed terminal，以及 expansion completed；所有调用点只传 branch slug、ordinal、count、status/error type 等安全字段（`batch_capture_replicate.py:341-366,1250-1282,1310-1381,1424-1430`）。旧的 orphan `series_expansion_failed` 已改为 `series_expansion_completed(overall_ok=false)`，hook 外层异常也用同一合法事件名（`batch_capture_replicate.py:1697-1709`）。生产者缺失这一核心问题已经修复。
- 但 `SeriesCaptureEventsTests.test_live_explorer_emits_all_seven_series_events_with_safe_fields` 的 required set 实际只有 5 种；`series_capture_partial` 和 `series_capture_failed` 只在 allowed set，成功 fixture 根本不触发它们（`test/test_multi_series_capture.py:457-479`）。消费者测试中的 partial/failed 仍是手工喂事件（`test/test_orchestrator_events.py:313-315,369-370`）。
- 该 live test 是 in-process `finalize_series_branches()` stdout capture，不是完整 `run_live_capture` child stdout→ManagedProcess→orchestrator→GUI/report 链（`test/test_multi_series_capture.py:420-455`）。此外 skipped descriptors 没有 terminal series event，见 finding 6。
- 结论：生产者主体已落地、非法 phase event 已修，但 7 种事件及真实转发链的验证没有完成，判部分关闭。

### 8. P1#8 — ⚠️ 部分关闭：sanitizer/route slug 已修，但“raw UID 不进 served HTML/所有产物”的字面保证未成立

- Metadata `raw_outer_html` 在写 `metadata_rows.json` 和 topology 前调用统一 `sanitize_html()`（`batch_capture_replicate.py:1086-1101`）；DOM snapshot 同样统一 sanitize（`capture_snapshot.py:101-130`）。新增真实 capture 测试检查 script、event handler 和 token-like attribute 被移除（`test/test_multi_series_capture.py:381-417`）。
- 事件字段与 route/runtime HTML 使用 safe branch id / `series_key_slug()`，没有把 raw series key 当作 `data-replica-series-key` 或 route map key（`batch_capture_replicate.py:352-366,1250-1282,1310-1381`; `build_replica.py:676-711,780-803`）。这一部分已关闭。
- 但 `capture_one_series()` 仍把完整 `asdict(descriptor)` 写入 `descriptor.json`，其中包含 raw `series_key`、label 和 stable attributes（`batch_capture_replicate.py:758-768`）；当前所谓“generated artifacts contain no raw UID”测试只检查 `_safe_series_key()` 返回值和一条手写 clean log，完全没有扫描真实生成目录（`test/test_multi_series_budget.py:156-168`）。
- 更关键的是 `sanitize_html()` 只移除可执行/credential/remote attributes，不脱敏文本节点（`capture_snapshot.py:101-118`）。完整 Metadata outerHTML 会被 builder 原样渲染为离线 served Metadata panel；若真实面板包含 SeriesInstanceUID/患者字段，raw UID 仍会出现在 served Metadata HTML。新增 sanitizer fixture 没有 UID/患者文本，因此没有验证用户要求的“raw UID 不进 served HTML”（`test/test_multi_series_capture.py:81-109,384-417`）。
- 这与“离线 Replica 展示完整 Metadata DOM”的产品目标存在边界冲突，必须明确：若 served offline replica 被定义为受限敏感医疗产物，应在文档/validation 中明确豁免 Metadata 可见文本，只禁止 raw identity 进入 route/event/log；若要求 served HTML 本身脱敏，则必须在 sanitizer 后另做字段级 redaction。当前实现和测试无法同时证明两种说法，故判部分关闭。

## 残留必须修改项

1. 修正 `_load_series_branch_snapshots()` 的 Metadata 文件路径为实际的 `branch_dir/metadata/metadata_rows.json`，并避免 topology 已有 metadata region 时重复附加；增加“topology 无 region、仅 metadata_rows/outerHTML”的真实目录 fallback 测试。
2. 加强 `SeriesContractEndToEndTests`：从真实 capture directories 构建，至少点击两个不同 series branch；从每个 Viewer 点击非空 Metadata trigger，断言完整 Metadata panel 可见，再 close 返回该 branch Viewer。直接断言 `meta_open_dom` 非空、每个 branch entry document 可解析且 builder 全部生成。
3. readiness 每个 poll/retry 必须从稳定 LocatorRecipe 和最新 pages/frame 重新解析 series root 与 row，而不是长期持有 activation 前的 `root_locator`；补齐一类证据拒绝、两类证据通过、row label 不计 name_match、iframe/root replacement 后仍能成功的测试。
4. per-branch finally 必须把 panel hidden、hub 可操作、selection/scroll 恢复变成可验证结果；restore 失败应使 branch partial/failed 或 raise `HubUnrecoverableError`，不能吞异常继续。补真实 close-no-op 与 reload 后重建 page/frame/root 的测试。
5. 让 max/budget/duplicate 尾项成为全链一等终态：返回 outcomes、持久 branch status/descriptor 的安全形式、进入 flow branch/audit，并产生约定的 terminal event；不能只存在 aggregate manifest count。
6. 用真实 live explorer 场景分别触发 captured、partial、failed，验证 7 种事件；再增加至少一条真实 child stdout→orchestrator tracker→report/GUI 的转发契约测试。修正当前“all seven”测试名或 required set，避免虚假覆盖声明。
7. 明确隐私边界并使实现、runbook、validation、测试一致：route/event/log 必须只用 slug/hash；若所有 served HTML 也禁止 raw UID，则对 Metadata 文本做字段级 redaction；若 offline Metadata 明确是受限敏感产物，则删掉“served HTML 无 raw UID”的绝对承诺并用访问/落盘边界约束。无论选择哪条，测试必须扫描真实 capture/replica/log 输出，而不是只检查 helper 返回值。

## 建议项

1. `_branch_topology()` 同步 remap `ReplicaPage.opener_page_id`，并增加 popup branch 图引用完整性测试。
2. 为无稳定属性的同名 series 增加 normalized text + occurrence identity 的 builder route fallback；当前 capture 的 text fallback 含 document id，不能跨 snapshot 直接相等。
3. `_synthetic_meta_open_target()` 不要把同一个、指向 `documents[-1]` 的 ActionTarget 实例追加到所有 Viewer documents；应根据 capture DOM 所属 document 精确挂载，避免嵌套 iframe 中重复/错坐标 trigger。
4. contract test 不应只把第一个 branch snapshot 手工包装成 entry state；增加从标准 `build_flow_from_snapshots()`/manifest 主入口进入的变体，减少测试与生产装配路径差异。
5. 为 loader/capture region 增加 sanitizer 后 outerHTML hash/内容一致性断言，证明写盘 topology、metadata_rows 和 served panel 使用同一份 sanitized DOM。

## Confidence + known gaps

**Confidence：高（0.91）。** P0#2、P0#3 的关闭结论以及 P0#1/P1#4/#5/#6/#7/#8 的残留均来自直接的数据流和控制流：writer/loader 路径可逐字符比对；contract test 的点击次数和 required event set 可直接读取；max tail 只进入 `manifest_outcomes` 而返回 `outcomes`；restore 异常被吞且无 hidden verification；descriptor/raw Metadata 的持久化与 builder render 路径均明确可见。

**Known gaps：**

- 当前受控 Windows 环境仍拒绝 Playwright driver pipe 和 Python temporary-directory 文件访问，因而无法实际执行新增的 `SeriesContractEndToEndTests`、offline browser clicks 或完整 focused suite。动态测试失败发生在 setup/cleanup，不作为实现失败；同时也不能声称这些 E2E tests PASS。
- 未连接 cxhospital/uicloud 等真实登录 Viewer；无法验证真实 iframe rebuild、虚拟列表 DOM replacement、Metadata trigger 所属 document/坐标和 close 动画。
- 未对真实医疗产物做字节级隐私扫描；关于 Metadata 中 raw UID/患者文本是否允许进入受限 offline replica，需要产品安全边界明确后才能给出最终合规结论。
- 本轮仅新增本 review 文件，未修改任何生产或测试代码。
