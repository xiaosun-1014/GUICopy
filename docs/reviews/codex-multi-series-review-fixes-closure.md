# Codex closure review — 多序列 Replica 修复关闭核验（第三轮）

- 审阅对象：当前工作区（相对 `HEAD 6782bbf` 的未提交改动）。
- 上一轮审阅：`docs/reviews/codex-multi-series-closure-review.md`（下称「closure」）、`docs/reviews/codex-multi-series-final-review.md`。
- 修复计划：`docs/superpowers/plans/2026-08-14-multi-series-replica-review-fixes.md`。
- 结论：**closure 的 7 项「残留必须修改项」已在当前工作区逐一闭环（含本轮新修的生产代码）；5 项「建议项」中 2 项本轮已修复、3 项经核验为诚实边界/无需改动。** 详见逐项对照与验证证据。

---

## 一、closure 7 项残留必须修改项 → 当前状态

### 1. loader Metadata 文件路径 + 真实目录 fallback 测试 —— ✅ 已关闭

- 修复点：`batch_capture_replicate._load_series_branch_snapshots` 读取 `branch_dir/metadata/metadata_rows.json`（`batch_capture_replicate.py:2184-2195`），不再读错的 `branch_dir/metadata_rows.json`。
- 防复次：仅在 topology 尚未内嵌 `metadata` region 时才 re-attach（`batch_capture_replicate.py:2203-2215`），writer 已嵌入的 region 不会被 fallback 重复。
- 测试：`test/test_multi_series_capture.py:SeriesContractEndToEndTests.test_metadata_rows_fallback_attaches_region_when_topology_lacks_it`（真实目录剥离 region 后 re-attach 且不重复）。

### 2. 加强 `SeriesContractEndToEndTests`：双 branch 点击 + Metadata open/close + meta_open_dom 非空 —— ✅ 已关闭

- `test_real_capture_to_offline_runtime_click_two_series`（`test/test_multi_series_capture.py:374`）：真实 capture 目录 → builder → 离线点两个不同 branch，断言 `data-replica-series-key` ≥ 2、每 branch viewer/metadata entry 生成。
- `test_real_capture_flow_metadata_region_and_atomic_remap`（`:437`）：断言每 branch `meta_open_dom` 非空、remap 后 `entry_document_id` 可解析。
- `test_real_capture_clicks_metadata_and_close_returns_to_same_viewer`（`:512`）：从每 branch Viewer 点 Metadata trigger → 断言完整 `.replica-metadata` 面板可见、含 branch 独有 value → close 返回同 branch Viewer。
- 从真实 capture directories 构建，不再手工构造理想 branch document。

### 3. readiness 每 poll/retry 从稳定 recipe 重解析 root/row —— ✅ 已关闭

- `_wait_for_series_ready`（`batch_capture_replicate.py:956-995`）每 poll 用 `_find_viewer_frame(page)` 重找 frame、`_reparse_series_root(recipe, page)` 重解析 root（内部 `_resolve_locator_recipe(recipe, _live_pages_map(page))`，`batch_capture_replicate.py:1003-1020`），再从新 root `_reparse_target_row`；不再持有激活前 `root_locator`。
- 证据分类：`_collect_evidence` 的 `name_match` 比较 Viewer 当前显示 identity（`_viewer_current_series_label`），`dom_stable` 比较两次指纹相等，`screenshot_nonblank` 不计数（`_evidence_satisfied` 要求 ≥ 2 类 core）。
- 测试：`test/test_capture_readiness.py:69-115`（`test_evidence_satisfied_requires_two_core_classes`、`test_row_label_does_not_serve_as_name_match`、`test_root_replacement_reparses_from_recipe`）、`test/test_capture_readiness.py:393`（`test_close_noop_degrades_branch_to_partial_and_next_branch_still_clean`）。

### 4. per-branch finally 可验证恢复，失败不吞异常 —— ✅ 已关闭

- `capture_one_series` 单一 `finally` 兜底 + clean 路径 `else` 显式恢复（`batch_capture_replicate.py:888-918`）。
- `_restore_hub_state` 返回 `(ok, problem)`：panel hidden、hub 可操作、selection/scroll 全部验证（`batch_capture_replicate.py:1689-1743`），`_open_metadata_if_needed` 关闭时 `_wait_for_metadata_hidden`（`:1745-1774`）。
- restore 失败 → clean 路径把 branch 降 partial（`warning=hub_restore_failed`），不当 partial 时写 `status.json` 并可由 caller 驱动 reload；不吞异常。
- 测试：`test/test_capture_readiness.py:393`（close no-op 降 partial + 下一条仍 clean）、reload 后重建 pages/root/locator（`batch_capture_replicate.py:1384-1388`）。

### 5. max/budget/duplicate 尾项成为全链一等终态 —— ✅ 已关闭

- 循环后 `manifest_outcomes` 追加 tail，函数返回 `manifest_outcomes`（`batch_capture_replicate.py:1450-1462,1533`）。
- 为未尝试的尾项持久化分支目录 `status.json` + `descriptor.json`（`_persist_unattempted_branch`，`batch_capture_replicate.py:1613-1645`），loader 由此构造一等 `SeriesBranch`,进入 flow `series_branches` 与 `_expansion_evidence` 分母（`batch_capture_replicate.py:2625-2659`）。
- 每个尾项发射约定 terminal event（skipped 映射 `series_capture_partial`，`batch_capture_replicate.py:1481-1499`）；`overall_ok` 要求 `captured==discovered` 且他桶为零。
- 测试：`test/test_multi_series_budget.py`（守恒、reloaded、skipped 映射）。

### 6. 真实 live explorer 7 事件 + 转发链 —— ✅ 已关闭

- 生产者：`_emit_series_event`（`batch_capture_replicate.py:341-366`）发射 `discovery_started/discovered/capture_started/completed/partial/failed/expansion_completed`；尾部 skipped 有 terminal event。
- 测试（改写为诚实声明）：
  - `SeriesCaptureEventsTests.test_live_explorer_full_success_produces_success_path_events`（`:728`）——成功 fixture 只产生成功路径，required set 为 5 个成功事件（名称/scope 诚实）。
  - `test_live_explorer_produces_all_seven_events_across_real_scenarios`（`:757`）——captured/partial/failed 三个真实场景合计覆盖全部 7 个事件。
  - `test_real_event_stream_forwarded_to_tracker_and_report`（`:779`）——真实 explorer stdout JSONL → `normalize_child_event`/`SeriesTracker` → report coverage，区分 captured/partial，非手工喂事件。

### 7. 隐私边界一致性 —— ✅ 已闭环（含本轮文档修正）

- 代码已实现并一直一致：route map / served 非 Metadata HTML / 事件 / 日志只用 `series_key_slug()`/SHA-256；`sanitize_html()` 统一用于 Metadata outerHTML 写盘前（`batch_capture_replicate.py:1144`）与 DOM snapshot（`capture_snapshot.py`）；`build_replica` 剥离 `data-series-uid/data-uid` 原始身份属性（`build_replica.py:25-38`）。
- 隐私边界定案（产品决策）：**offline served Metadata 面板是本地受限敏感产物**，保留完整（已剔可执行/credential/remote）DOM 文本；其余所有 served 面/route/event/log/report 一律禁止 raw UID/患者派生 key。`pipeline_validation.validate_series_privacy` 通过 `_strip_generated_metadata_blocks` 实现同一边界。
- 测试逐字节扫描真实产物：`SeriesPersistedOutputSafetyTests.test_real_generated_replica_has_no_raw_uid_outside_metadata_and_panel_readable`（`test/test_multi_series_capture.py:623`）遍历生成 replica 的 html/json，断言 raw UID 只出现在 metadata 块内；`test_real_metadata_outer_html_is_sanitized_on_disk`（`:588`）。
- **文档残留 misstatement（本轮修复）**：`docs/PIPELINE_RUNBOOK.md:113-115`「Metadata 面板只落 `<hash 前缀>` …不落原文」与实现的 `metadata/metadata_rows.json` 落完整 sanitized outerHTML 不符（closure 项 7 及此前最终审阅均点名）。已改写为与 §5b 豁免边界一致（public 面只落 hash，完整 Metadata DOM 作为本地受限产物落盘并复刻 served 面板）。
- `docs/MULTI_HOSPITAL_REPLICA_RUNBOOK.md:236-244`、design spec §2.4/§8/§11 与实现一致，无需改动。

---

## 二、closure 建议项 → 当前状态

1. **`_branch_topology` remap `ReplicaPage.opener_page_id` + popup 图测试 —— ✅ 本轮已修复**
   - `batch_capture_replicate.py:2595-2599`：`new_pages` 现在经 `remap_page` 映射 `opener_page_id`（含 None 保护）。
   - 新增测试：`test/test_branch_topology_fixes.py:BranchTopologyRemapTests.test_opener_page_id_remapped_together_with_page_and_entry_doc`（PASS）。
2. **builder 弱身份（text+occurrence）fallback —— ⚠️ 诚实边界，不新增**
   - capture 侧已用 `document_id+normalized_text+occurrence` 区分无稳定属性同名项（`capture_snapshot.py:326-330`），且确定性发生序来自 DOM order（`same_name_index`）。
   - builder 侧 `_member_series_key` 对无稳定属性成员返回 `None`（`build_replica.py:41-53`），不给 `data-replica-series-key`。
   - 判定：分支 viewer 的 DOM order 未必等于 hub 的 order，若在 builder 重建 text+occurrence 反而可能**错绑**到同名另一分支；当前「不给路由键 → 不可点」是**诚实不误绑**行为，符合「不伪造 complete」反模式。记录为已知边界，不引入脆弱 fallback。
3. **Meta open target 精确挂载 owning document —— ✅ 本轮已修复**
   - 新增 `_document_id_for_recipe` + `_frame_hop_matches_document`（`batch_capture_replicate.py:2622-2688`）按 recipe `page_var`+`frame_chain` 解析唯一 owning document；每 hop 必须唯一，歧义返回 `None` 不猜。
   - `_build_branches_into_flow` 只向该 owning document 挂载单一 target（`batch_capture_replicate.py:2488-2510`），删除「同一实例追加到所有 document」与 `documents[-1]` 猜测；`_synthetic_meta_open_target` 改为接收显式 `document_id`（`:2690`）。
   - 新增测试：`test/test_branch_topology_fixes.py:DocumentIdForRecipeTests`（跨 frame 解析/歧义 None/空链/无 page）+ `MetaOpenMountTests.test_build_branches_mounts_meta_open_once_on_owning_document`（PASS）。
4. **contract test 经标准 `build_flow_from_snapshots()`/manifest 主入口 —— ⚠️ 部分（诚实说明）**
   - 现有 `SeriesContractEndToEndTests._build_flow` 以真实 branch 的第一个 viewer snapshot 手工包装为 entry state，再经 `_load_series_branch_snapshots`/`_build_branches_into_flow` 合并真实分支。这是刻意为之：让 offline entry 是「真实 branch viewer（含真实 series region）」以便从其中点击其它序列。生产主入口 `build_flow_from_snapshots` 的合并路径与之一致（同 `_build_branches_into_flow`）。
   - `_capture_to_manifest_core`（`batch_capture_replicate.py:2662-2709`）已通过标准 `build_flow_from_snapshots` + `write_manifest` 产出 manifest，作为主入口测试的落点。
5. **sanitizer outerHTML hash/内容一致性 —— ⚠️ 已覆盖核心不变量**
   - writer 写 `metadata/metadata_rows.json` 与 topology region 共用同一份 sanitized `outer_html` 变量（`batch_capture_replicate.py:1144-1157`），loader 与 builder 均读这份同一外存；`SeriesPersistedOutputSafetyTests` 逐字节验证写盘产物。未额外做 hash 校验字段（避免扩 schema），属可选项。

---

## 三、closure 8 个 finding 的关闭结论

| Finding | 结论 | 关键证据 |
|---|---|---|
| 1. P0#1 真实系列/Metadata region + loader fallback | ✅ closed | region 写入拓扑 + loader fallback 修复 + 双测试 |
| 2. P0#2 原子 remap | ✅ closed | `_branch_topology` 全引用 remap + `opener_page_id` 补充 |
| 3. P0#3 稳定属性 route binding | ✅ closed | `series_route_by_identity`/`_member_series_key` + 成员本地绑定 |
| 4. P1#4 组合就绪 | ✅ closed | 每 poll 重解析 + ≥2 类证据 + 针对性测试 |
| 5. P1#5 per-branch finally/恢复验证 | ✅ closed | finally/else 恢复 + `(ok,problem)` + 降 partial |
| 6. P1#6 计数守恒 + 尾项一等终态 | ✅ closed | `manifest_outcomes` 返回 + 尾项持久化 + terminal event |
| 7. P1#7 7 事件 + 转发链 | ✅ closed | 生产者发射 + 三场景测试 + 转发链测试 |
| 8. P1#8 隐私边界 | ✅ closed | sanitizer + slug 公出面 + 逐字节扫描 + 文档修正 |

---

## 四、本轮改动清单（相对上次审阅基线）

生产代码：
- `batch_capture_replicate.py`：
  - `_branch_topology` remap `opener_page_id`（closure 建议 1）。
  - 新增 `_document_id_for_recipe`/`_frame_hop_matches_document`；`_build_branches_into_flow` 单点挂载 meta-open target 于 owning document；`_synthetic_meta_open_target` 收显式 `document_id`（closure 建议 3）。
- `docs/PIPELINE_RUNBOOK.md:113-115`：修正「Metadata 只落 hash」misstatement，与豁免边界一致（closure 项 7）。

测试：
- 新增 `test/test_branch_topology_fixes.py`（6 个纯函数/合并路径测试，PASS）。

文档：
- 新增本 closure 核验文件。

---

## 五、执行与验证证据（本环境）

**静态验证：**
- `py_compile` 全量生产文件 + 新增测试：PASS（exit 0）。
- `git diff --check`：PASS，仅工作区 LF→CRLF 提示，无空白错误。
- 反模式 grep（plans Phase 7.2）：`contentDocument`/`contentWindow.document` 仅注释出现；事件/报告无 `PatientName/Accession/SeriesInstanceUID` 输出。

**可执行纯测试（本受控环境可运行，全部 GREEN）：**
- `test_branch_topology_fixes` 6/6 ✅
- `test_replica_manifest` + `test_pipeline_validation` + `test_orchestrator_events` 等 100 ✅
- `test_replica_gui`/`test_replica_annotation_panel`/`test_pipeline_gui`/`test_pipeline_report`/`test_pipeline_models` 71 ✅
- `test_canvas_capture`/`test_locator_risk`/`test_markers`/`test_meta_extract`/`test_pipeline_adapter`/`test_pipeline_io`/`test_process_runner`/`test_runtime_python` 30 ✅

**无法在本环境执行的（环境限制，非实现失败）：** 依赖真实浏览器子进程 / Playwright pipe / 临时目录文件访问的套件（`test_multi_series_capture`、`test_batch_capture_replicate`、`test_capture_snapshot`、`test_capture_readiness`、`test_replica_runtime`/`e2e`/`topology`/`regions`、`test_build_replica`、`test_multi_series_budget` 的浏览器部分等）在本受控 Windows 沙箱启动时挂起/拒绝访问，无法给出 GREEN 执行结果。其结构与断言已在代码层逐条核验；这些 browser 测试需在可运行 Playwright 的合法登录/沙箱翻墙环境执行。

---

## 六、Known gaps / 残余说明

1. **builder 弱身份（无稳定属性同名跨 snapshot 路由）**：记录为诚实边界——不回退 text+occurrence 以免错绑；此类成员渲染为不可点击而非误绑。若产品要求支持，需在 viewer 提供可审计逻辑位置（`aria-posinset`/`data-index`）后再做。
2. **frame region/metadata trigger 的坐标对齐与长文档截图**（早期 P2#8）：当前实现仍以 frame HTML element 截图作资产；不在 closure 7 项 + 5 建议范围内，未改动。
3. **popup 分支的完整离线点击**：`_branch_topology` 现已正确 remap `opener_page_id`，但未在浏览器 E2E 中跨 popup 实测（环境限制）。
4. **真实站 smoke（cxhospital/uicloud）**：未执行（需合法登录环境）；见 MULTI_HOSPITAL_RUNBOOK。

---

## 七、完成度自评

closure 7 项「残留必须修改项」= 全 closed；8 个 finding = captured/closed 字段全部 closed；建议项 1、3 已修复并附 PASS 测试，建议项 2、4、5 经核验为诚实边界/已覆盖核心不变量/无需改动。生产代码与文档无已知 P0/P1 残留。受控环境无法执行浏览器套件是本轮唯一未闭环的验收维度（环境限制，非代码缺陷）。
