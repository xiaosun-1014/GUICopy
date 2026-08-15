# Codex final review — 多序列可点击 Replica（Phase 0–10）

结论：**Request changes / 当前不能按 Phase 10 完成定义验收。** schema v2 数据类、显式 `return_state_id`、served route slug、GUI opt-in 和事件聚合器各自已有实现，但真实的 `capture_one_series()` 产物没有被构造成可点击的 series/Metadata DOM，且 branch 拓扑引用在合并时被破坏。与此同时，探索事务的就绪判定、失败恢复、计数守恒和事件发射存在阻断性缺口。现有测试大量使用手工构造的“理想 branch document”，没有覆盖真实 capture artifact → flow → builder → offline runtime 的整链路，因此无法证明 MVP 成立。

## Sources consulted

- 基线与完整变更面：`HEAD 6782bbf`；`git status --short`、`git diff --stat 6782bbf`、`git diff --name-status 6782bbf`、`git ls-files --others --exclude-standard`。审阅了 30 个已跟踪改动文件以及全部未跟踪文件（含 `PRODUCT.md`、`capture_readiness.py`、计划/规格、`test/fixtures/multi_series/` 和 3 个新测试文件）。
- 规范：
  - `docs/superpowers/plans/2026-08-14-multi-series-replica-expansion.md`，特别是全局反模式、Phase 2/3/5/6/7/8/9/10、完成定义和审阅输出要求。
  - `docs/superpowers/specs/2026-08-14-multi-series-replica-expansion-design.md`，特别是数据契约、单序列事务、组合就绪、Metadata 事务、覆盖率裁决和隐私边界。
  - `docs/REPLICA_DESIGN.md`，特别是模型、series/meta region、状态证据、截图对齐、HTML sanitizer、builder/runtime 和隐私要求。
  - `docs/MULTI_HOSPITAL_REPLICA_RUNBOOK.md`、`docs/PIPELINE_RUNBOOK.md`、`PRODUCT.md`、`.gitignore`。
- 生产实现：`batch_capture_replicate.py`、`capture_snapshot.py`、`capture_readiness.py`、`replica_models.py`、`replay_helpers.py`、`build_replica.py`、`orchestrator_events.py`、`pipeline_models.py`、`pipeline_orchestrator.py`、`pipeline_preflight.py`、`pipeline_validation.py`、`pipeline_report.py`、`main_gui.py`。
- 测试与 fixture：本次所有修改/新增的 `test/test_*.py` 和 `test/fixtures/multi_series/*`，重点精读 multi-series capture、budget、manifest、builder、runtime、E2E、pipeline validation/report/orchestrator/GUI 测试。
- 静态/可执行验证：
  - `py_compile capture_snapshot.py replica_models.py batch_capture_replicate.py build_replica.py pipeline_validation.py`：PASS。
  - `git diff --check 6782bbf`：PASS（仅出现 Git 的 LF→CRLF 工作区提示）。
  - Phase 10 anti-pattern grep：生产 capture 代码没有新增 `contentDocument`/`contentWindow.document`；固定等待均处于轮询或已有 hook 兜底中，但下述就绪实现仍有逻辑缺陷；report/events 文件没有直接输出 `PatientName`/`Accession`/`SeriesInstanceUID`。
  - `test.test_orchestrator_events`：45 tests PASS；两个不写磁盘的 v2 schema 测试 PASS。
  - Phase 10 focused/full suite **未能得到有效完整结果**：当前受控 Windows 环境拒绝 `tempfile.TemporaryDirectory()` 下的文件访问和 Playwright driver pipe 创建（统一为 `PermissionError/WinError 5`，发生在测试 setup 而非断言）。尝试把 `%TEMP%` 改到允许根仍被相同 ACL 策略阻止。因而不能把这些环境错误算作实现失败，也不能声称 focused/full regression PASS。

## Concrete findings

### [P0] 1. 真实 branch capture 没有保存可路由的 series region 或可读取的 Metadata DOM，Phase 6/7 的离线分支实际上不可用

- `batch_capture_replicate.py:651-659` 的 `capture_one_series()` 对 Viewer 只调用 `capture_page_topology()`；`capture_snapshot.py:522-574` 的该函数只创建截图、`ReplicaPage` 和空的 `ReplicaDocument`，不会捕获 series `InteractionRegion`、series members 或 Metadata trigger target。
- Metadata 同样只在 `batch_capture_replicate.py:844-855` 把 `metadata_rows.json` 与空 topology 分开落盘。`_load_series_branch_snapshots()` 在 `batch_capture_replicate.py:1592-1617` 虽读取 rows，却没有把 rows/outerHTML 组装成 Metadata `InteractionRegion`。
- `_build_branches_into_flow()` 在 `batch_capture_replicate.py:1858-1866` 创建的 synthetic meta-open `ActionTarget` 来自 `_synthetic_meta_open_target()`；后者在 `batch_capture_replicate.py:1940-1961` 明确 `dom=None`。`build_replica.py:380-408` 只渲染 `target.dom` 非空的 target，因此真实 branch Viewer 页面没有可点击 Metadata 按钮。
- 真实 metadata documents 没有 Metadata region，`build_replica.py:613-620,731-734` 的 `active_page_has_metadata` 为 false；因此 fallback close button 也不会出现，`metadata_rows.json` 的内容更不会进入 served HTML。
- 测试为何没抓到：`test/test_replica_runtime.py:100-140` 手工构造了带 Metadata region、带 series members 且 member id 完美匹配的 documents；`test/test_batch_capture_replicate.py:984-1025` 的 branch fixture 则使用空 documents，只断言 state/transition 对象存在，没有把真实 `capture_one_series()` 产物交给 builder/runtime。
- 影响：不满足“每个成功序列展示自己的语义 DOM 和完整 Metadata DOM”“Metadata 可打开/关闭”“任意成功序列可点击”的核心完成定义。这是合并阻断项。

### [P0] 2. branch document ID 被重写，但 `ReplicaPage.entry_document_id` 没有同步，生成 URL 时会出现悬空引用

- `_branch_documents()` 在 `batch_capture_replicate.py:1921-1936` 给每个 document id/page id 加 branch 前缀，却只返回 documents。
- `_build_branches_into_flow()` 在 `batch_capture_replicate.py:1867-1869,1882-1885` 仍把原始 `snapshot.viewer_pages` / `snapshot.metadata_pages` 原样放入 state。真实 `capture_page_topology()` 的 page 仍引用 `d_p_000_root`，而合并后的 document 已变成 `{branch_id}__d_p_000_root`。
- builder 在 `build_replica.py:630-637` 用 `target_page.entry_document_id` 查 target document，并以 `next(...)` 强制取得；series route 的 `_state_entry_path()` 也依赖同一拓扑关系。对真实 branch state，这会导致 `StopIteration`/无法计算 route URL，而不是可导航页面。
- 测试为何没抓到：Phase 6 loader fixture 在 `test/test_batch_capture_replicate.py:1005-1007` 写 `pages=[]`，绕过了真实 page→entry-document 引用；runtime 测试则直接构造一致的 page/doc id。
- 影响：即使补上 region，真实 flow 的 builder 仍可能在生成 branch route 时失败。需要将 page/document remap 作为一个原子拓扑变换，并增加真实 artifact round-trip 测试。

### [P0] 3. route 绑定依赖一个不会跨 snapshot 复现的 `source_member_id`，真实 series 节点不会获得 `data-replica-series-key`

- discovery 在 `finalize_series_branches()` 中硬编码 `doc_id = "d_series_hub"`（`batch_capture_replicate.py:983-993`），因此 descriptor member id 是 `d_series_hub_series_NNN`。
- 普通录制 snapshot/未来 branch region 的 member id 由实际 `ReplicaDocument.document_id` 生成（`capture_snapshot.py:321-345,350-360`），不会自然等于上述 synthetic hub id；branch snapshot 当前甚至没有 series region（finding 1）。
- builder 只按字符串相等绑定：`build_replica.py:740-743` 建 `source_member_id -> slug`，`build_replica.py:423-435` 仅在 member id 命中时输出 `data-replica-series-key`。注释中“member id recurs across every document”没有实现依据。
- 测试人为让两者相等：`test/test_replica_runtime.py:137-140` 的 `source_member_id` 直接使用 `_series_members()` 产生的 `ma/mb/mc`。
- 影响：真实 hub/Viewer 页面上的 series option 没有 route attribute，新增 O(N) route map 即使正确注入也不会被 runtime 使用。应以可验证的稳定属性/descriptor identity 做每个 state 内的重新关联，不能假设 capture-local member id 跨 snapshot 稳定。

### [P1] 4. 组合就绪既没有要求“两类证据”，也缓存了激活前 Locator/Frame；未切换序列也会被判 ready

- `_wait_for_series_ready()` 的契约说“至少两类”，但 `batch_capture_replicate.py:745-751` 对任意非空 `evidence` 稳定 0.8 秒即成功，没有 `len(evidence) >= 2`，也没有区分变化证据和静态证据。
- `_collect_evidence()` 在 `batch_capture_replicate.py:788-790` 用当前 target 自身文本等于 descriptor label 作为 `name_match`；这是定位成功后的恒真条件，不证明 Viewer 已切换。
- DOM 稳定证据在 `batch_capture_replicate.py:796-801` 取两次 fingerprint，却不比较两次值；只要第二次非空就添加 `dom_stable`。非空截图（803-805）同样可能是上一个序列。
- `capture_one_series()` 在 `batch_capture_replicate.py:617-648` 激活前缓存 `viewer_frame` 和 `row_locator`，随后 readiness 轮询和一次重试继续使用它们。如果真实 Viewer 切换会重建 iframe或虚拟列表节点复用，这正好违反“每次点击前重新解析 Locator/Frame”的核心反模式。
- 影响：点击失败、点到复用节点或 iframe 重建时，旧 Viewer 很容易以 `name_match + dom_stable/screenshot_nonblank` 被误判成功并落成错误 branch。应在每个 poll/retry 重新发现 Page/Frame/row，并实现证据类别计数；静态稳定只能与 selected/current identity 或 Viewer 变化共同组成成功。

### [P1] 5. 单序列 Metadata/Hub 事务没有 per-branch finally，失败会污染后续序列；声明的 reload 恢复路径实际上不可达

- `capture_one_series()` 的主体在 `batch_capture_replicate.py:615-693` 没有 `finally` 恢复 scrollTop、panel 状态或 hub 可操作性；只有整个探索结束时 `finalize_series_branches()` 在 1094-1095 做一次总恢复。这不满足每个 descriptor 独立事务。
- `_capture_metadata_transaction()` 在 Metadata readiness 超时处直接 return（`batch_capture_replicate.py:840-842`）；close 只在成功捕获后的 858-865 执行，而且 close click 异常被吞、`_wait_for_metadata_hidden()` 的 false 返回值也被忽略。于是一个 partial branch 可把面板保持打开，下一 branch 继续在错误 UI 状态操作。
- `finalize_series_branches()` 只在捕获到 `HubUnrecoverableError` 时增加连续失败并 reload（`batch_capture_replicate.py:1028-1060`），但仓库内该异常只有类定义和 catch，没有 raise。`capture_one_series()` 对 locator/hub 失败通常返回普通 failed outcome（621-631），调用方随后在 1027 把 `consecutive_hub_failures` 重置为 0。
- 影响：计划要求的“单条失败隔离、连续三条 hub 失败一次受控恢复、最终恢复原始状态”没有实现。必须用 per-branch `try/finally` 强制关闭/恢复，并让 hub-unrecoverable 分类真正驱动恢复状态机；reload 后还要重新建立 pages/root/template locator，不能继续使用 reload 前的 `pages`/`root`。

### [P1] 6. `max_series`/budget/skipped 分支被从 discovered 口径删除，可能把不完整探索报告为 complete

- 循环只遍历 `descriptors[:max_series]`（`batch_capture_replicate.py:1001`）。如果实际发现数大于 `max_series` 且总时间未耗尽，尾部 descriptors 没有任何终态。
- 结束时 `discovered = len(outcomes)`（1081），没有使用 `len(descriptors)` 或 discovery evidence；因此被 max 截断的条目被静默从 discovered 删除，违反“每个 descriptor 都有终态”和“不把失败项从 discovered 列表删除”。
- budget 分支会生成 `skipped_budget`（1004-1010），但 `_expansion_evidence()` 在 `batch_capture_replicate.py:1964-1979` 只统计 captured/partial/failed，并把 discovered 重新定义成这三类之和；skipped 再次被删除。模型 `SeriesExpansionEvidence` 也没有 skipped_count，`pipeline_validation.py:180-184` 因此无法审计真实 discovery 守恒。
- `overall_ok` 对普通 partial/failed outcome 也没有统一置 false，`reached_end` 仍可能为 true。
- 影响：公共报告/validation 可显示“完整”，实际却有被上限或预算跳过的序列。需要冻结单一口径：discovered 来自 discovery，所有 descriptor（含 skipped_budget/duplicate）必须有终态；若 schema 不接受 skipped，则明确映射为 partial/failed 并保留原因，而不是从分母删除。

### [P1] 7. 七个 `series_*` 事件只有定义和消费者，没有生产者；GUI/报告在真实运行中看不到覆盖率

- `orchestrator_events.py:47-84,218-332` 定义了 7 个事件和 `SeriesTracker`；`pipeline_orchestrator.py:303-355,642-708`、`main_gui.py:1132-1148` 和 `pipeline_report.py:54-104` 都依赖这些事件。
- 但在生产代码中检索 JSON event literal，`batch_capture_replicate.py` 唯一的 series emit 是 `series_expansion_failed`（1354-1363）；这个名字反而不在 `SERIES_EVENT_NAMES`。`finalize_series_branches()`、`capture_one_series()` 和 manifest writer 均未 emit discovery/capture/completed 事件。
- 因而真实 capture 即使执行 expansion，controller 的 tracker 仍是 inactive，`run_capture()` 会返回 SUCCESS 且 report 默认 `not_requested`。异常路径的 `series_expansion_failed` 也不会被 tracker 计入 failed。
- `test/test_orchestrator_events.py` 的 45 个测试验证的是手工喂入事件后的消费者；`test/test_pipeline_orchestrator.py` 同样 mock 了 child event 流，没有验证 live capture 真的产生 7 个事件。
- 影响：Phase 8 的 GUI 进度、stage 降级和公共覆盖率报告没有端到端实现。应在 explorer 的确定时点发射且只发安全字段，并用真实 `run_live_capture` stdout→ManagedProcess→controller→report 测试验证覆盖率。

### [P1] 8. 原始 `series_key`/Metadata outerHTML 仍写入磁盘，且 outerHTML 未走统一 sanitizer；当前“所有输出面脱敏”的说法不成立

- served HTML 与 route map 的正向结论：`build_replica.py:646-681,740-743` 确实统一使用 `series_key_slug()`，未发现 raw `series_key` 直接写入 served route/attribute；事件 tracker 也只保留 branch id/ordinal/status/stage。
- 但 capture 输出仍写原值：`batch_capture_replicate.py:599-602` 将 `asdict(descriptor)` 写入 `descriptor.json`，其中包含 raw `series_key`、label 和全部 stable attributes；`_build_branches_into_flow()` 又在 1892-1905 把 raw key/label 放进 `ReplicaFlow`，`replay_helpers.py:94-95` 将其原样写入 `capture/manifest.json`。
- 更严重的是 `batch_capture_replicate.py:844-847` 把完整 `outer_html` 和解析 rows 写入 JSON；`_capture_metadata_panel()` 在 893-919 直接取 `el.outerHTML`，没有调用 `sanitize_html()`。这违反 `docs/REPLICA_DESIGN.md` “outerHTML 写入内存/磁盘前必须统一 sanitizer、不得另存 raw 副本”的明确安全契约，并可把 UID、患者字段、token-like attribute 或原事件 handler 带入产物。
- `docs/PIPELINE_RUNBOOK.md:108-114,136-139` 对 operator 的表述也比实现更强，尤其“Metadata 面板只落 hash 前缀”的说法与 `metadata_rows.json` 的 raw outerHTML/row 不符。现有 privacy validation（`pipeline_validation.py:456-497`）只扫描 credential pattern，不验证患者名、检查号、UID 或 raw series key。
- 影响：这些文件虽然位于本地敏感 capture 树、当前不直接由 replica server 提供，但仍是持久输出/config-like manifest，不能满足用户要求的“所有输出面均脱敏”或统一 sanitizer。需要明确并实现边界：若 raw identity 仅允许在受限证据库中存在，应隔离、标记敏感且不进入通用 manifest；否则 descriptor/manifest 只保留 slug/hash。无论采用何种边界，raw outerHTML 必须先 sanitizer。

### [P2] 9. discovery 对“无稳定属性的同名序列”先合并后编号，fallback occurrence index 实际无效；滚动后也没有 DOM 稳定等待

- `_series_identity()` 对无稳定属性项返回 `("text", normalized_text)`；`discover_series_candidates()` 在 `capture_snapshot.py:273-301` 以此作为 dict key，所以两个同名、无属性的逻辑序列在生成 descriptor 前已经合并。
- 后续 `same_name_index`（318-330）无法恢复第二项，尽管设计要求 fallback 为 normalized text + document/frame + occurrence index。
- 滚动在 304-309 后立即进行下一轮 `items.count()/nth()`，没有 Phase 2 要求的有界 DOM 稳定等待，也没有“连续两步无新增”终止条件。异步虚拟列表可能采到旧窗口、跳过新窗口，或者错误声明 reached_end。
- 建议增加无稳定属性同名 fixture（当前测试只覆盖有不同 stable attrs 的同名项），并按每个可见窗口 occurrence/position 构造临时 identity，再结合滚动窗口去重；滚动后等待 item signature 稳定而非固定 sleep。

### [P2] 10. schema version 与 series 字段的关系未被强制，v1 flow 可序列化 branches、再读取时静默丢失

- `ReplicaFlow.from_dict()` 在 `replica_models.py:359-370` 对 v1 主动丢弃 `series_branches/series_expansion`，这为真正旧 manifest 提供了兼容路径；未知版本也会拒绝，这是正向实现。
- 但 `ReplicaFlow.to_dict()` 无条件 `asdict()`（355-356），`validate_manifest()` 也没有要求“有 series 数据必须 schema_version==2”。因此内存中 schema v1 + branches 会被写出，随后 read 时 branches 被静默清空，round-trip 不一致。
- 新 runtime fixture 本身就在 `test/test_replica_runtime.py:143-149` 构造 schema_version=1 且含 branches，进一步掩盖了契约错误。
- 建议让模型/validation 明确拒绝 v1+series fields，并把所有 multi-series tests 改为 v2；保留纯 v1 fixture 验证空 branches/None。

### [P2] 11. iframe screenshot 改成 `frame.locator("html").screenshot()`，偏离已冻结的 viewport clip/坐标契约

- `capture_snapshot.py:552-572` 对子 frame 使用 HTML element screenshot，而设计契约要求 Frame 没有 screenshot API 时用 owning Page 的 CSS-scale clip + 累积 content offset，保证截图与 `frame_viewport_css` rect/viewport 对齐。
- element screenshot 可能捕获完整可滚动 HTML、触发滚动，且其像素尺寸不保证等于记录的 `innerWidth/innerHeight`；builder 却仍把它按 document viewport 拉伸，存在 overlay/背景错位回归风险。
- 建议恢复/复用既有 Page clip + frame offset 算法，并增加“frame document 高于 viewport、带 iframe border/scroll”的像素尺寸与 overlay 对齐断言。

## 必须修改项

1. 打通真实产物链：`capture_one_series()` 必须捕获 Viewer 的 series region/语义 DOM和 Metadata 完整 sanitized region；loader 必须把 `metadata_rows/outerHTML` 合成普通 `ReplicaDocument` region/targets；用真实 capture artifact 构建并跑 offline runtime，而不是手工理想模型。
2. 将 branch topology remap 做成原子操作，同时更新 documents、parent ids、page ids、`ReplicaPage.entry_document_id` 和相关 action/region document ids；增加 builder 可成功解析每个 branch entry 的验证。
3. 移除跨 snapshot `source_member_id` 相等假设。每个 state 渲染前按稳定 descriptor 属性重新关联 member；同名项必须由稳定属性/occurrence 明确区分。
4. 重写组合就绪：每次 poll/retry 重新解析 Page/Frame/row；至少两类独立证据；比较 DOM fingerprint 的实际相等/变化；旧序列的非空截图和 target 自身 label 不得单独计成功。
5. 为每个 branch 建立强制 `try/finally`：关闭/恢复 Metadata、恢复 hub/scroll，验证 close hidden；让 hub-unrecoverable 分类和一次 reload 路径可达，reload 后重建全部 locator/page/frame state。
6. 修正 discovery/终态计数：以 discovery count 为唯一分母，为 max/budget/duplicate 尾项写终态，schema/validation/report 全链保留 skipped 或等价 partial reason；禁止通过缩小 discovered 分母报告 complete。
7. 在 live explorer 发射计划中的 7 个安全 `series_*` 事件，并统一处理 expansion infrastructure failure；增加真实 stdout→orchestrator→GUI/report 测试，不能只测 tracker 消费手工事件。
8. 明确 series identity 的持久化安全边界；所有公开/通用输出使用 `series_key_slug`，不允许 raw UID/患者派生 key 进入通用 manifest/config/log/report。Metadata outerHTML 写盘前必须走统一 sanitizer，并使 runbook 与真实保留内容一致。

## 建议修改项

1. 修复无稳定属性同名项的 fallback identity，并为滚动窗口增加 signature-based 稳定等待和连续无新增终止条件。
2. 强制 schema v2 才能携带 series fields；新增 v1+branches 拒绝测试，避免 write/read 静默丢数据。
3. 恢复 iframe Page-clip 截图契约并补齐长文档/边框/嵌套 iframe 对齐测试。
4. 收紧 `_capture_metadata_panel()` 与 shared readiness 的职责：复用同一 panel-root 解析器，避免 readiness、region capture、transaction capture 三处用不同候选过滤规则；当前 `capture_readiness.py` 还直接导入 `capture_snapshot` 的私有 `_MARKER_REGION_CANDIDATES`，与“dependency-light public helper”目标不完全一致。
5. 将 Phase 6/7 测试改成“Phase 5 真实目录输出作为输入”的 contract test；现有手工 JSON fixture可以保留作单元测试，但不能替代整链验收。
6. `PRODUCT.md` 是未跟踪且不在 Phase 0–10 文件清单中的新增文件；内容本身无明显风险，但合并前应确认它是否属于本次提交，避免夹带无关产物。

## Confidence + known gaps

**Confidence：高（0.93）**。P0/P1 结论来自直接控制流和数据结构引用：真实 capture documents 的 region/target 为空、page/document remap 不一致、route key 只按不稳定 member id 相等绑定、readiness 条件只检查 evidence 非空、series 事件没有生产者。这些不依赖真实医院页面即可成立，且测试 fixture 的绕过方式可定位。

**Known gaps：**

- 当前执行环境不允许 Playwright driver 创建子进程管道，也不允许 unittest 在临时目录内写文件；因此 Phase 10 focused suite、完整回归和离线浏览器 E2E未能有效执行。已通过的 45 个 event tests 和 2 个纯内存 schema tests只能证明局部消费者/decoder 行为。
- 未进行 cxhospital/uicloud 真实站 smoke test；工作区也没有可审阅的本地 `out/{hospital}/multi_series_spike/` 证据。计划中 click/dblclick、iframe rebuild、真实 Metadata identity/close 和虚拟列表行为仍需在合法登录环境校准。
- 无法在本轮验证截图视觉像素对齐或外部 HTTP(S) 请求数为 0；这两项需要可运行浏览器的 E2E 环境。
- 因沙箱权限，测试框架短暂创建的空 `tmp*` 目录无法由当前进程访问/删除；它们不含文件且不出现在 Git tracked/untracked 清单中，但宿主环境应在会话结束后清理。除指定 review 文件外，本审阅未编辑任何生产或测试文件。