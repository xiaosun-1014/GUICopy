# 多序列 Replica 审阅问题修复计划

> **供实施与复审 agent 使用：** 本计划修复 `docs/reviews/2026-08-14-multi-series-code-review.md` 中的 1 个 P0、4 个 P1 和 3 个 P2。各阶段必须先写能复现问题的失败测试，再复制本文列出的现有模式做最小实现；不得用 URL 跳转成功代替 Viewer/Metadata 语义正确性。

**目标：** 修复同名序列串支、嵌套 iframe document 归属、Meta trigger 重复、stale root/双恢复、Metadata 身份未校验、弱身份 discovery、schema validation 和 frame screenshot 对齐问题，使真实 capture → schema v2 flow → builder → 离线 runtime 的内容与每个序列一一对应。

**输入审阅：** `docs/reviews/2026-08-14-multi-series-code-review.md`

**实施原则：** 保持 `processed_script` 为 live capture 的唯一示教来源；保留当前 schema v2 的 O(N) branch route；优先复用现有 `LocatorRecipe`、topology、readiness 和 sanitizer，不引入新的 Viewer crawler 或 DICOM renderer。

---

## Phase 0：文档/API 核验与修复边界冻结

**目的：** 在改代码前统一身份、document ownership、Metadata identity 和截图坐标契约，避免修复阶段产生第二套算法。

**必须阅读：**

- `docs/reviews/2026-08-14-multi-series-code-review.md` 全文。
- `docs/superpowers/specs/2026-08-14-multi-series-replica-expansion-design.md` 的 §2.2、§4、§6、§7、§10、§11。
- `docs/REPLICA_DESIGN.md` 的 document topology、iframe screenshot、region coordinate space、HTML sanitizer 章节。
- 原计划 `docs/superpowers/plans/2026-08-14-multi-series-replica-expansion.md` 的 Phase 2、3、5、6、7、10。

### Task 0.1：冻结四项修复契约

- [ ] **主身份契约：** descriptor 有稳定属性时，只比较 `_SERIES_IDENTITY_ATTRS` 中最高优先级的第一项；该项不等即不匹配，禁止回退 label。
- [ ] **弱身份契约：** 仅无稳定属性时使用 `document_id + normalized_text + occurrence`；虚拟列表无法证明 occurrence 时标 partial/ambiguous，不伪造 complete。
- [ ] **owning document 契约：** series region、Metadata region、Meta open target 必须属于 locator recipe 的 `page_var + frame_chain` 指向的 `ReplicaDocument`。
- [ ] **Metadata identity 契约：** UID 优先；无 UID 时使用 SeriesNumber + SeriesDescription；明确不一致为 `metadata_identity_mismatch`，无可比较证据为 `metadata_identity_unverified`，两者都只能 partial。
- [ ] **截图契约：** child-frame PNG 只捕获 frame 可见 content viewport，像素尺寸必须等于 `ReplicaDocument.viewport`；不捕获完整滚动 HTML。

### Task 0.2：建立审阅问题追踪表

在本计划执行记录或后续 closure review 中维护：

| Finding | 修复阶段 | 必须证明的结果 |
|---|---|---|
| P0#1 同名串支 | Phase 1 | 两个同名序列的 Viewer marker、截图 hash、Metadata identity 均不同 |
| P1#2 document 错挂 | Phase 2 | series/Metadata region 只属于 inner document |
| P1#3 Meta trigger 重复 | Phase 2 | 每 branch 全拓扑恰好一个 meta-open target |
| P1#4 stale root/双恢复 | Phase 3 | 激活后使用新 root；每事务恢复恰好一次 |
| P1#5 Metadata 未校验 | Phase 3 | stale/mismatch panel 不得 captured |
| P2#6 weak discovery | Phase 4 | 静态同名 occurrence 可区分；不确定虚拟列表诚实 partial |
| P2#7 schema validation | Phase 5 | 非法 v1/v2 branch graph 在写出/构建前被拒绝 |
| P2#8 frame screenshot | Phase 6 | PNG 尺寸/overlay 与 frame viewport 对齐 |

### 允许复用的现有 API/模式

- `capture_snapshot.capture_locator_snapshot(locator, coordinate_space=...)`
- `capture_snapshot.discover_series_candidates(...)`：保持为唯一 discovery 算法。
- `capture_snapshot.capture_series_interaction_region(...)`
- `capture_snapshot.capture_marker_interaction_region(...)`
- `capture_snapshot.capture_marker_panel_region(...)`
- `capture_snapshot.capture_page_topology(...)`
- `capture_readiness.metadata_panel_signature(...)`
- `capture_readiness.wait_for_metadata_panel_state(...)`
- `capture_readiness.canvas_hash(...)`
- `capture_readiness.viewer_dom_fingerprint(...)`
- `capture_readiness.screenshot_nonblank(...)`
- `capture_readiness.metadata_uid_sha256_prefix(...)`
- `batch_capture_replicate._resolve_locator_recipe(...)`
- `batch_capture_replicate._live_pages_map(...)`
- `LiveCaptureSession._reparse_series_root(...)`
- `LiveCaptureSession._capture()` 中通过 locator/frame 选择 owning document 的已有模式。
- `ReplicaDocument.from_dict()`：继续作为 branch topology 唯一 decoder。
- `_branch_topology(...)`：继续统一 remap page/document/region/target 引用。

**验证清单：** 四项契约写入测试名称、warning 名称和后续实现注释；如需修改冻结字段，必须先修订设计文档。

**反模式守卫：** 不新增 `identity_kind/value` 字段作为默认方案；现有 `stable_attributes + series_key` 足以推导主身份。不得静默改变 schema。

---

## Phase 1：修复稳定主身份与同名序列串支（P0）

**目的：** 保证 discovery、live re-location、branch-local member binding 使用同一主身份规则，任何成功 branch 都对应正确序列内容。

**文件：**

- Modify: `capture_snapshot.py`
- Modify: `batch_capture_replicate.py`
- Modify: `test/test_replica_regions.py`
- Modify: `test/test_multi_series_capture.py`
- Reuse: `test/fixtures/multi_series/series_list.html`

### Task 1.1：先写主身份失败测试

- [ ] 新增纯匹配测试 `test_same_name_descriptor_requires_primary_identity_match`。
- [ ] 使用两个 row：共享 label 和 `data-series="Coronal MIP"`，仅 `data-series-uid` 不同。
- [ ] 断言 descriptor B 不匹配 row A，descriptor A/B 互不相等。
- [ ] 直接覆盖 `_matches_descriptor()` 和 `_series_descriptor_matches()`，先确认 RED。

### Task 1.2：强化真实 capture E2E

- [ ] 在 `SeriesContractEndToEndTests` 新增 `test_same_name_series_capture_distinct_viewer_and_metadata_identity`。
- [ ] 复用 `series_list.html` 已有两个同名 Coronal MIP、`#viewer[data-viewer-key]` 和三字段 Metadata。
- [ ] 对两个同名 branch 分别断言：
  - Viewer `data-viewer-key`/文本不同；
  - viewer screenshot SHA-256 不同；
  - Metadata SeriesNumber/SeriesDescription/UID hash 与目标 descriptor 对应；
  - 离线点击后显示目标 branch 独有内容，而不只断言 URL 变化。
- [ ] 保留 raw UID 不进入 route/event/log 的隐私断言。

### Task 1.3：提取唯一主身份 helper

建议新增内部 helper，名称可按现有风格调整：

```python
def _series_primary_identity(attributes: Mapping[str, str]) -> tuple[str, str] | None:
    for name in _SERIES_IDENTITY_ATTRS:
        value = attributes.get(name)
        if value:
            return name, value
    return None
```

- [ ] `capture_snapshot._series_identity()` 调用该 helper，不再保留第二套优先级判断。
- [ ] `batch_capture_replicate` 复用同一优先级常量/helper；如跨模块使用，提升为不带下划线的共享 API，禁止复制常量后各自漂移。
- [ ] `_matches_descriptor()`：descriptor 有主身份时只比较同一属性，失败立即 false；只有没有稳定身份时才进入弱身份路径。
- [ ] `_series_descriptor_matches()`：比较主身份；不得因次级共享属性或相同 label 命中。
- [ ] `_capture_series_region()` 找不到主身份对应 member 时标 warning/partial，不回退成看似有效但不可路由的 hub member id。

### Task 1.4：验证

```powershell
D:/Anaconda/envs/codegen-marker/python.exe -m unittest `
  test.test_replica_regions `
  test.test_multi_series_capture.MultiSeriesCaptureTests.test_same_name_descriptor_requires_primary_identity_match `
  test.test_multi_series_capture.SeriesContractEndToEndTests.test_same_name_series_capture_distinct_viewer_and_metadata_identity -v
```

**验收：** 同名 descriptor B 的 capture artifact 不能包含 descriptor A 的 Viewer/Metadata identity；两个 branch 的内容证据不同且与各自 key 一致。

**反模式守卫：** 不用“任意 stable attr 相等”；有 stable identity 时不回退文本；不以不同 URL 代替内容正确性。

---

## Phase 2：统一 owning-document 与 Meta target 归属（P1#2/#3）

**目的：** 让 main page、popup、单层/两层 iframe 的 expansion 产物都保留正确 document graph 和局部坐标系。

**文件：**

- Modify: `batch_capture_replicate.py`
- Optional Modify: `capture_snapshot.py`（仅当 resolver 适合成为 topology 公共 helper）
- Modify: `test/test_multi_series_capture.py`
- Modify: `test/test_batch_capture_replicate.py`
- Modify: `test/fixtures/multi_series/nested_inner.html` 或在测试内用同源 `set_content` 构造完整 inner viewer

### Task 2.1：写 nested expansion 失败测试

- [ ] 新增 `test_nested_frame_expansion_attaches_regions_to_owning_document`。
- [ ] 复用 `test_nested_frame_series_uses_scroll_harvest` 的同源两层 iframe 搭建方式；不要直接依赖跨源 `file://` 的 `window.frameElement`。
- [ ] inner frame 包含 series list、Viewer、Metadata open/close，至少两个序列。
- [ ] 运行真实 `finalize_series_branches()`，读取 branch viewer/metadata topology。
- [ ] 断言每类 region 恰好一个，且 `region.document_id == inner_document.document_id`。
- [ ] 断言 frame region/root rect 使用 `frame_viewport_css`，member 继续使用 `region_content_css`。

### Task 2.2：写 Meta target 唯一性失败测试

- [ ] 新增 `test_nested_frame_branch_has_exactly_one_meta_open_target`。
- [ ] 从真实 branch directories 经 loader 和 `_build_branches_into_flow()` 构建 states。
- [ ] 全 Viewer state topology 中 `series:{branch_id}:meta_open` target 恰好一个。
- [ ] target 只位于 inner document，`target.document_id` 与承载它的 document 一致。
- [ ] 离线 browser 不使用 `.first` 掩盖重复；先断言 count==1，再点击。

### Task 2.3：实现纯 topology owning-document resolver

建议签名：

```python
def _document_id_for_recipe(
    recipe: LocatorRecipe,
    pages: list[ReplicaPage],
    documents: list[ReplicaDocument],
) -> str | None:
    ...
```

- [ ] 从 `recipe.page_var` 找对应 `ReplicaPage.entry_document_id`。
- [ ] 按 `recipe.frame_chain` 顺序，在当前 document 的直接 children 中匹配 `frame_selector`；必要时用已保存 `frame_id/frame_name` 作等价匹配。
- [ ] 每一 hop 必须唯一；0 或多于 1 个候选返回 None/明确 warning，不猜 `docs_out[0]` 或 `documents[-1]`。
- [ ] 普通 marker capture 与 expansion 逐步收敛到同一 resolver，避免两个 owning-document 算法继续漂移。

### Task 2.4：持久化 Meta open 所属 document

- [ ] 将 `meta_open_target.json` 从裸 `DomNodeSnapshot` 升级为带版本的内部 payload，例如：

```json
{
  "schema_version": 1,
  "document_id": "d_p_000_f_002",
  "dom": { "...": "DomNodeSnapshot" }
}
```

- [ ] loader 向后兼容旧裸 snapshot；旧数据没有 document id 时只能明确 warning，不得猜最后一个 document。
- [ ] `SeriesBranchCapture` 增加内部 `meta_open_document_id`（不修改公开 `SeriesBranch` schema）。
- [ ] `_branch_topology()` remap 后，根据 source document 映射得到 branch-local document id。
- [ ] `_synthetic_meta_open_target()` 接收明确 `document_id`，删除 `documents[-1]` 猜测。
- [ ] `_build_branches_into_flow()` 只向 owning document 追加一次 target；builder 可额外防御性跳过 `target.document_id != document.document_id`，但不能靠 builder 掩盖错误 graph。

### Task 2.5：完整 nested offline E2E

- [ ] 新增 `test_nested_frame_expansion_builds_clickable_offline_routes`。
- [ ] 点击至少两个 series route，断言 inner-frame Viewer 独有 DOM/asset。
- [ ] 从每个 Viewer 点击唯一 Metadata target，断言 Metadata 独有内容；close 返回同 branch Viewer。
- [ ] 断言 main/outer documents 不含伪造的 meta-open overlay。

### Task 2.6：验证

```powershell
D:/Anaconda/envs/codegen-marker/python.exe -m unittest `
  test.test_batch_capture_replicate `
  test.test_multi_series_capture.SeriesContractEndToEndTests.test_nested_frame_expansion_attaches_regions_to_owning_document `
  test.test_multi_series_capture.SeriesContractEndToEndTests.test_nested_frame_branch_has_exactly_one_meta_open_target `
  test.test_multi_series_capture.SeriesContractEndToEndTests.test_nested_frame_expansion_builds_clickable_offline_routes -v
```

**验收：** 每个 region/target 的 `document_id`、承载 document 和坐标空间一致；任一 Viewer state 全拓扑只有一个 Metadata trigger。

**反模式守卫：** 不使用 `docs_out[0]`、`documents[-1]` 或给所有 documents 追加同一 target；不使用 `contentDocument/contentWindow.document`。

---

## Phase 3：修复事务 root 生命周期与 Metadata 身份校验（P1#4/#5）

**目的：** 激活可能重建 iframe/root 时仍能捕获、验证并恢复正确序列；错误/陈旧 Metadata 不得标 captured。

**文件：**

- Modify: `batch_capture_replicate.py`
- Modify: `capture_snapshot.py`（如需收集 Metadata audit attributes）
- Modify: `test/test_multi_series_capture.py`
- Modify: `test/test_capture_readiness.py`

### Task 3.1：写 root replacement 与单恢复失败测试

- [ ] 新增 `test_capture_reparses_root_after_activation_before_viewer_harvest`。
- [ ] click handler 替换 series root 或重建 inner iframe，并给新 root 设置 generation marker。
- [ ] readiness 后捕获的 series region 必须包含新 generation marker。
- [ ] 新增 `test_restore_reparses_replaced_root`，断言 selection/scroll 在新 root 上恢复。
- [ ] 新增 `test_successful_series_transaction_restores_exactly_once`，用 `patch.object(..., wraps=...)` 断言 `_restore_hub_state.call_count == 1`。

### Task 3.2：收敛 `capture_one_series()` 生命周期

- [ ] `_capture_viewer_topology()` 不再接收激活前 root；改为接收稳定 recipe/page，并在捕获 region 前调用 `_reparse_series_root()`。
- [ ] capture 前用 `_live_pages_map(page)` 刷新 page map。
- [ ] `_restore_hub_state()` 改为接收 recipe 或在调用前强制重新解析最新 root/pages；不得让 stale root 穿过 activation 边界。
- [ ] 重构成功/异常路径为单一 `finally` 恢复：
  - 保存原始异常；
  - 尝试且只尝试一次恢复；
  - 把 restore 失败写入最终 `status.json`；
  - 不用 restore 异常覆盖原始 capture 异常；
  - clean path 在恢复完成后再构造并 return outcome。
- [ ] 删除 `_capture_series_region()` 捕获失败后静默使用 hub member id 的假成功路径；捕获不到 branch-local member 时至少 partial。

### Task 3.3：写 stale Metadata 失败测试

- [ ] 新增 `test_stale_metadata_identity_degrades_branch_to_partial`。
- [ ] fixture 激活 B 后故意保留 A 的 Metadata UID/SeriesNumber/Description。
- [ ] Viewer B 可用，但结果必须：
  - `capture_status == "partial"`；
  - `fail_stage == "identity"`；
  - warning 为 `metadata_identity_mismatch`；
  - warning/status/event 不含原始 UID。
- [ ] 新增 `test_metadata_identity_unverified_degrades_branch_to_partial`，面板只有 Study 级字段时使用 `metadata_identity_unverified`。

### Task 3.4：实现结构化 Metadata identity evidence

- [ ] 扩展本地 Metadata parser，输出规范化字段：`SeriesInstanceUID`、`SeriesNumber`、`SeriesDescription`；保留原始 sanitized rows 供审计。
- [ ] discovery row 若存在 `data-series-instance-uid/data-series-number/data-series-description`，允许写入 restricted `descriptor.json` 的 `stable_attributes` 作为内部 audit evidence；这些字段不得进入 route/event/log/report。
- [ ] 新增内部比较 helper，例如：

```python
def _compare_metadata_identity(
    descriptor: SeriesDescriptor,
    parsed: Mapping[str, str],
) -> tuple[str, str | None]:
    # verified | mismatch | unverified, safe warning
    ...
```

- [ ] 比较顺序：
  1. 双方有 SeriesInstanceUID → 比较 SHA-256，不比较/记录公开原文；
  2. 否则双方有 SeriesNumber + SeriesDescription → 规范化后同时比较；
  3. 有可比较字段且不一致 → mismatch；
  4. 没有足够证据 → unverified。
- [ ] 只有 verified 且 panel close/restore 成功才允许 Metadata 部分贡献 captured；mismatch/unverified 保留 Viewer 并降 partial。

### Task 3.5：验证

```powershell
D:/Anaconda/envs/codegen-marker/python.exe -m unittest `
  test.test_capture_readiness `
  test.test_multi_series_capture.MultiSeriesCaptureTests.test_capture_reparses_root_after_activation_before_viewer_harvest `
  test.test_multi_series_capture.MultiSeriesCaptureTests.test_restore_reparses_replaced_root `
  test.test_multi_series_capture.MultiSeriesCaptureTests.test_successful_series_transaction_restores_exactly_once `
  test.test_multi_series_capture.MultiSeriesCaptureTests.test_stale_metadata_identity_degrades_branch_to_partial `
  test.test_multi_series_capture.MultiSeriesCaptureTests.test_metadata_identity_unverified_degrades_branch_to_partial -v
```

**验收：** iframe/root replacement 后仍捕获新状态；恢复调用严格一次；stale/Study-only Metadata 不得 captured。

**反模式守卫：** 不缓存跨 activation 的 Locator/Frame/root；不只计算 UID hash 而不比较；不因 Metadata 失败删除已成功 Viewer branch。

---

## Phase 4：修复弱身份 discovery 与异步虚拟列表等待（P2#6）

**目的：** 无稳定属性的同名静态项可按 document order 区分；无法证明逻辑位置的虚拟项诚实报告 partial。

**文件：**

- Modify: `capture_snapshot.py`
- Modify: `replica_models.py`（仅 warning/evidence 能用现有字段时不改）
- Modify: `test/test_replica_regions.py`
- Modify: `test/fixtures/multi_series/`（新增异步虚拟列表 fixture 或测试内 markup）

### Task 4.1：写 weak identity 失败测试

- [ ] `test_discovery_same_name_without_stable_attributes_uses_occurrence`：静态列表两个完全同名 `li`，断言两个 descriptor/member，key 以 `x0/x1` 区分且重复 capture 确定性一致。
- [ ] `test_discovery_waits_for_async_virtual_window`：scroll 后 DOM 延迟更新并复用节点，断言不会立刻读取旧窗口。
- [ ] `test_ambiguous_virtual_same_name_is_partial`：虚拟列表同名且没有 `aria-posinset/data-index/绝对位置` 等证据，断言 `reached_end=false`、warning=`series_identity_ambiguous` 或等价冻结名称。
- [ ] 保留 source `scrollTop` 恢复断言。

### Task 4.2：实现窗口签名稳定等待

- [ ] 每次 scroll 后计算可见 item signature（主身份、规范化文本、可用 logical position、数量）。
- [ ] signature 连续相同达到短稳定窗口后再收集；总等待受 `max_duration_s` 约束。
- [ ] 连续两步无新增才允许提前结束；到达真实 bottom、步数、总时限仍是硬边界。
- [ ] 不用固定 sleep 作为唯一 readiness。

### Task 4.3：实现 occurrence 与 ambiguity 规则

- [ ] 非虚拟/全部 DOM 同时存在：在去重前按 DOM order 给相同 normalized text 分配 occurrence。
- [ ] 虚拟列表优先使用可审计逻辑位置：`aria-posinset`、稳定 `data-index` 或 viewer adapter 明确配置。
- [ ] 没有逻辑位置且多个完全同名项跨窗口无法证明是否同一项：不得任意合并或宣称 complete；保留已确认 members，设置 ambiguous partial warning。
- [ ] descriptor/member 仍一一对应；不存 Locator、ElementHandle、绝对点击坐标。

### Task 4.4：验证

```powershell
D:/Anaconda/envs/codegen-marker/python.exe -m unittest test.test_replica_regions test.test_capture_snapshot -v
```

**验收：** 静态同名弱身份得到不同稳定 key；异步虚拟列表不漏读旧窗口；无法证明身份时不报告 complete。

**反模式守卫：** 不先按 text 去重再补 occurrence；不为完全不确定的虚拟节点伪造唯一身份。

---

## Phase 5：收紧 schema v2 与 branch graph validation（P2#7）

**目的：** 在 builder/runtime 之前拒绝会静默丢字段、route 覆盖或缺少可用 Viewer/Metadata state 的 manifest。

**文件：**

- Modify: `replica_models.py`
- Modify: `pipeline_validation.py`
- Modify: `replay_helpers.py`（如 write helper 需要前置 validation）
- Modify: `test/test_replica_manifest.py`
- Modify: `test/test_pipeline_validation.py`

### Task 5.1：写 validation 失败测试

- [ ] `test_v1_flow_with_series_data_is_rejected`。
- [ ] `test_v1_series_round_trip_cannot_silently_drop_branch_fields`。
- [ ] `test_duplicate_branch_id_is_rejected`。
- [ ] `test_source_member_must_resolve_to_series_region_member`。
- [ ] `test_partial_branch_must_have_viewer_state`。
- [ ] `test_captured_branch_requires_metadata_and_return`（本 MVP 的完整模板始终要求 Metadata open/close）。
- [ ] `test_metadata_state_requires_viewer_and_explicit_return`。
- [ ] 保留纯 v1、无 series fields 的读取兼容测试。

### Task 5.2：实现模型/写出守卫

- [ ] `ReplicaFlow.to_dict()` 或 manifest write helper 在 `schema_version == 1` 且 series fields 非空时抛出可读错误；禁止写出后由 `from_dict()` 静默丢字段。
- [ ] schema v2 继续 round-trip 所有 branch/evidence 字段。
- [ ] 未知 schema version 继续拒绝。

### Task 5.3：扩展 `validate_manifest()`

- [ ] 有 series data 时强制 `schema_version == 2`。
- [ ] `branch_id` 与 `series_key` 分别唯一。
- [ ] captured/partial 必须有存在的 Viewer state；failed/skipped 可无 Viewer，但必须有安全 reason/warning。
- [ ] captured 必须有 Metadata state、`return_state_id == viewer_state_id`；partial 的 Metadata 可缺失但 Viewer 不允许缺失。
- [ ] `source_member_id` 必须能在 branch Viewer state 的 series regions 中关联；如果支持 stable-identity route fallback，validation 也必须证明至少一种 route binding 可成立，不能只看非空字符串。
- [ ] 所有 branch state、transition、page entry document 引用继续通过现有 graph validation。
- [ ] aggregate 计数与 branch terminal 数量一致；skipped 映射 partial 时保留原因。

### Task 5.4：验证

```powershell
D:/Anaconda/envs/codegen-marker/python.exe -m unittest `
  test.test_replica_manifest `
  test.test_pipeline_validation.SeriesBranchValidationTests -v
```

**验收：** v1+series、重复 branch id、悬空 source member、partial 无 Viewer、captured 无 Metadata 均在 build 前失败；合法 v1/v2 round-trip 不回归。

**反模式守卫：** 不在 `from_dict()` 静默修正损坏 v2；不让 builder 猜缺失 return/source member。

---

## Phase 6：恢复 iframe viewport screenshot/overlay 对齐契约（P2#8）

**目的：** child-frame 背景资产与 `frame_viewport_css` rect 使用同一 CSS viewport 坐标系。

**文件：**

- Modify: `capture_snapshot.py`
- Modify: `test/test_capture_snapshot.py`
- Modify: `test/test_replica_topology.py`
- Verify: `build_replica.py`

### Task 6.1：写 screenshot 尺寸与对齐失败测试

- [ ] `test_frame_screenshot_dimensions_match_recorded_viewport`：frame inner document 高于 viewport；用 Pillow 断言 PNG size 等于 document viewport。
- [ ] `test_nested_frame_clip_accounts_for_borders_and_ancestor_offsets`：两层 iframe 各有 border/offset，断言捕获的是 inner content viewport，不含外层错误区域。
- [ ] `test_frame_region_overlay_aligns_with_viewport_screenshot`：在 inner frame 放置已知色块/按钮，build 后断言 overlay 与背景同一局部坐标。

建议尺寸断言：

```python
with Image.open(capture_root / document.screenshot_asset_relpath) as image:
    self.assertEqual(image.size, (
        document.viewport["width"],
        document.viewport["height"],
    ))
```

### Task 6.2：实现 frame viewport clip

- [ ] 删除 `frame.locator("html").screenshot()` 的完整 element screenshot 路径。
- [ ] 从 owning Page 截取 iframe content viewport：基于 `frame_element.bounding_box()` 与 `clientLeft/clientTop/clientWidth/clientHeight` 计算 CSS-scale clip；验证 Playwright 返回的 nested frame box 是否已是 main-frame 坐标。
- [ ] 截图固定 `scale="css"`，PNG 像素尺寸与记录 viewport 一致。
- [ ] clip 失败时写明确 warning/partial，不退回全文 HTML 截图后谎称 viewport 对齐。
- [ ] 保留 PNG→JPEG 派生和 SHA-256 asset 去重。

### Task 6.3：验证

```powershell
D:/Anaconda/envs/codegen-marker/python.exe -m unittest `
  test.test_capture_snapshot `
  test.test_replica_topology `
  test.test_build_replica -v
```

**验收：** 长 frame PNG 不被拉伸；两层 iframe 的背景与 series/meta/action overlays 对齐。

**反模式守卫：** 不混用 full-element screenshot 和 viewport dimensions；不忽略 iframe border/content offset；不扩大公开 schema 来规避最小修复。

---

## Phase 7：文档、全链回归与真实站验收

**目的：** 证明全部审阅 finding 已关闭，且 cxhospital/uicloud 真实约束与实现一致。

**文件：**

- Modify: `docs/superpowers/specs/2026-08-14-multi-series-replica-expansion-design.md`
- Modify: `docs/MULTI_HOSPITAL_REPLICA_RUNBOOK.md`
- Modify: `docs/PIPELINE_RUNBOOK.md`
- Modify: `skills/_shared/viewers.yaml`（仅写匿名 selector/行为证据）
- Create: `docs/reviews/2026-08-14-multi-series-review-fixes-closure.md`
- Local-only: `out/{hospital}/multi_series_spike/`

### Task 7.1：更新契约与运行手册

- [ ] 记录主身份只比较最高优先级属性，不允许次级共享属性/text fallback。
- [ ] 记录 Metadata identity 的 verified/mismatch/unverified 与 partial 语义。
- [ ] 记录 owning document payload、单恢复事务和 frame viewport screenshot 契约。
- [ ] 更新 warning/status 字典与 operator 可见含义。
- [ ] 保持隐私边界：restricted Metadata panel 可包含完整 sanitized DOM；route/event/log/report 只用 slug/hash。

### Task 7.2：反模式 grep

```powershell
rg -n "contentDocument|contentWindow.*document" batch_capture_replicate.py capture_snapshot.py
rg -n "docs_out\[0\]|documents\[-1\]" batch_capture_replicate.py
rg -n "frame\.locator\(\"html\"\)\.screenshot" capture_snapshot.py
rg -n "for name, value in descriptor\.stable_attributes" batch_capture_replicate.py
rg -n "raw UID|PatientName|Accession|SeriesInstanceUID" orchestrator_events.py pipeline_report.py docs/PIPELINE_RUNBOOK.md
```

预期：前三类生产反模式无命中；最后一项仅允许文档/validator 的安全说明，不允许事件/报告输出原值。

### Task 7.3：Focused tests

```powershell
D:/Anaconda/envs/codegen-marker/python.exe -m unittest `
  test.test_replica_regions `
  test.test_capture_snapshot `
  test.test_capture_readiness `
  test.test_batch_capture_replicate `
  test.test_multi_series_capture `
  test.test_multi_series_budget `
  test.test_replica_manifest `
  test.test_build_replica `
  test.test_replica_runtime `
  test.test_replica_e2e `
  test.test_pipeline_validation `
  test.test_pipeline_report `
  test.test_orchestrator_events `
  test.test_pipeline_orchestrator -v
```

Playwright 组应拆分 job 或给予足够超时；不得把“输出 OK 但外层进程超时”记录成完整 PASS。

### Task 7.4：完整回归与静态检查

```powershell
D:/Anaconda/envs/codegen-marker/python.exe -m py_compile `
  capture_snapshot.py capture_readiness.py replica_models.py `
  batch_capture_replicate.py build_replica.py pipeline_validation.py

D:/Anaconda/envs/codegen-marker/python.exe -m unittest discover -s test -v
D:/Anaconda/envs/codegen-marker/python.exe test/fixtures/multi_series/_smoke.py
git diff --check
```

### Task 7.5：离线 E2E 验收

- [ ] 从真实 branch capture directories 经标准 `build_flow_from_snapshots()`/manifest 主入口构建，不手工拼理想 states。
- [ ] 点击两个同名但不同 UID 的序列，逐一验证独有 Viewer DOM、asset hash、Metadata identity。
- [ ] 在两层 iframe replica 中重复相同验证。
- [ ] 每个 Metadata trigger count==1；close 明确返回同 branch Viewer。
- [ ] failed/skipped branch 可见、disabled、不假跳转。
- [ ] 浏览器 network log 中外部 HTTP(S) 请求数为 0。
- [ ] 扫描 served route/event/log/report，raw series identity 不泄漏；Metadata sensitive panel 边界按设计豁免并保持可读。

### Task 7.6：cxhospital/uicloud 真实站只读 smoke

- [ ] 每站选择至少两个序列，其中至少一站覆盖同名或相似名条目。
- [ ] 验证录制 activation 是 click/dblclick，并确认实现只继承不猜测。
- [ ] 记录切换是否重建 iframe/root；确认 capture/restore 使用新 root。
- [ ] 记录 series/Metadata locator 的实际 owning frame chain。
- [ ] 校验每 branch Viewer 视觉/当前序列证据与 Metadata identity 一致。
- [ ] 验证虚拟列表 reached_end/partial 语义和 panel close。
- [ ] selector/行为只以匿名配置写入 `viewers.yaml`；患者 DOM、截图、UID 仅保存在 gitignored `out/`。

### Task 7.7：closure review

- [ ] 按本计划 Task 0.2 的 8 项逐条给出 `closed / partial / open`。
- [ ] 每个 closed finding 引用生产函数、失败测试和 GREEN 结果。
- [ ] 记录 focused/full suite 的测试数、耗时和退出码。
- [ ] 记录真实站 smoke 已验证项与仍未知项。

**最终验收：** closure review 中没有 P0/P1 open；8 个 finding 全部有自动化证据；同名序列、两层 iframe、Metadata identity 和 screenshot 对齐均进入真实 capture → offline runtime 整链。

---

## 完成定义

只有同时满足以下条件，才可将本修复计划标记完成：

- [ ] 同名序列按最高优先级主身份重新定位，两个 branch 的 Viewer/Metadata 内容不串支。
- [ ] main/popup/两层 iframe 的 series region、Metadata region、Meta target 均属于正确 document。
- [ ] 每 branch 只有一个 Metadata trigger，且 document/rect 坐标一致。
- [ ] activation 后所有 capture/restore 使用最新 root/pages；每事务恢复恰好一次。
- [ ] Metadata mismatch/unverified 诚实降 partial，不得 captured。
- [ ] 无稳定属性同名静态项可区分；不确定虚拟列表不谎报 complete。
- [ ] v1+series 和损坏 v2 graph 在 build 前被拒绝；合法 v1/v2 round-trip 保持兼容。
- [ ] frame screenshot 像素尺寸等于 viewport，嵌套 overlay 对齐。
- [ ] focused suite、完整 unittest、静态检查、离线 E2E 全部取得明确退出码 0。
- [ ] cxhospital/uicloud smoke 完成，证据匿名且不进入 Git。

## 全局反模式

- 不把 `max()`/第一个文本命中改成循环就视为身份修复完成。
- 不用任意次级属性或 label 覆盖主 UID 不一致。
- 不用 `docs_out[0]`、`documents[-1]`、所有 document 批量追加 target。
- 不缓存跨 activation/reload 的 Locator、Frame、root 或 pages map。
- 不在 success path 与 finally 各恢复一次。
- 不把 Metadata hash“已计算”当成“已校验”。
- 不在虚拟列表身份不明时伪造 complete。
- 不让 v1 写出 series fields 后由 reader 静默丢弃。
- 不用完整 HTML element screenshot 充当 frame viewport screenshot。
- 不用 URL/aria-selected 变化代替 Viewer DOM、asset 与 Metadata identity 的内容断言。
- 不把真实患者 DOM、截图、UID 或 token 写入提交文件、公开日志、事件或报告。
