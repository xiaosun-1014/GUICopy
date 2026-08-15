# 多序列可点击 Replica 实施计划

> **供实施与复审 agent 使用：** 本计划按独立阶段组织。每个阶段必须先读列出的设计/实现依据，再写失败测试，然后做最小实现。不得跳过真实站 Spike、schema 兼容和最终离线验证。

**目标：** 用户只人工录制一个序列的完整流程，live capture 在同一已登录浏览器会话中自动发现并串行采集其余可发现序列，使离线 Replica 中每个成功采集的序列都能点击，并展示该序列对应的 Viewer 静态视觉状态、序列/Viewer 语义 DOM 和完整 Metadata DOM。

**推荐架构：** 保持 `processed_script` 为 Replica live capture 的唯一示教来源。在录制动作 hook 之外增加一个受控的 `series expansion capture` 阶段：从录制动作学习 Page/Frame、click/dblclick 和 Metadata 开关 locator；有界枚举序列；逐项重新定位、点击、等待、捕获、恢复；把结果编码为 manifest schema v2 的 `SeriesBranch`，由 builder/runtime 使用全局 `series_key -> viewer state` 路由生成可分支的离线状态图。

**技术栈：** Python 3、Playwright sync API、dataclasses、JSON manifest、生成式 HTML/JavaScript、`unittest`。

**MVP 边界：** 每序列保存一个稳定 Viewer 截图、所需语义 DOM、序列列表 DOM 和完整 Metadata 面板 DOM。MVP 不复制原站 JavaScript，不在离线环境运行真实 DICOM/WebGL 渲染器，也不自动采集每个序列的全部帧。

**2026-08-14 复审修订记录：** 已核正 Metadata readiness 与 `_load_snapshot_state()` 的模块归属；区分“已有 transition 的录制序列 target”和“没有 route 的未录制 series member”；记录 `ReplicaFlow.from_dict()` 当前硬拒绝非 v1；补充 public readiness helper、`to_dict()` round-trip、独立 expansion hook 的具体注入机制和测试文件存在性核验；实现引用改用函数/类名为主，行号仅作当前版本的大致定位。

---

## 一、经过确认的架构事实

1. Replica live capture 使用 `processed_script`，不能改为执行 `completed_*.py`。后者包含自动选序列、循环翻帧等额外行为，会污染人工示教状态机。依据：`docs/superpowers/specs/2026-08-04-one-recording-adapter-replica-pipeline-design.md` 中“processed recording / completed adapter / live capture 边界”章节。
2. `capture_snapshot.capture_series_interaction_region()` 已能滚动收集多个序列 DOM，并恢复 `scrollTop`，但这些条目只是 `InteractionRegion.members`。依据：`capture_snapshot.py` 中的同名函数（当前约 191–218 行）。
3. 已录制的序列 target 如果存在 `ReplicaTransition`，runtime 会先更新 `aria-selected`，随后执行 `window.open()` 或 `window.top.location.assign()` 导航；只有未在录制中出现、仅作为普通 series region member 保存的其他序列没有 transition，点击后才会停留在 aria-selected 更新而不切换 Viewer、截图或 Metadata state。依据：`build_replica.py` 的 `RUNTIME` click handler（当前约 41–63 行）。
4. `ReplicaState` 与 `ReplicaTransition` 已能表达分支；缺口主要在 live exploration、manifest 语义和普通 series member 到 route 的绑定。依据：`replica_models.py` 中的 `ReplicaTransition`/`ReplicaState`，以及 `build_replica.build_replica()`。
5. Metadata 完整 DOM 捕获位于 `capture_snapshot.capture_marker_panel_region()`；Metadata signature 和稳定等待目前分别是 `batch_capture_replicate._metadata_panel_signature()` 与 `_wait_for_metadata_panel_state()`。这些 readiness 函数当前是模块内私有实现，跨模块复用前必须提升为共享 public helper。
6. 嵌套 iframe 的序列 marker 当前会走普通 parent-region 捕获，绕过 series scroll harvest。依据：`batch_capture_replicate.LiveCaptureSession._capture()` 的 nested-document marker 路由分支（当前约 319–334 行）。这是后续工作的前置修复。
7. Viewer canvas 不是语义 DOM。只保存 DOM 和 Metadata 不能让医学影像视觉随序列变化；MVP 必须为每个序列保存至少一张稳定 Viewer 截图。
8. `ReplicaFlow.from_dict()` 当前使用 `schema_version != 1` 直接拒绝非 v1 manifest；v2 读取与 v1 向后兼容都是 Phase 3 的待实现能力，不是现有能力。

### 允许复用的现有 API/模式

- `capture_locator_snapshot(locator, coordinate_space=...)`
- `capture_series_interaction_region(root_locator, document_id, max_scroll_steps=40)`
- `capture_marker_interaction_region(scope, marker_label, document_id, target_locator)`
- `capture_marker_panel_region(scope, candidates, document_id)`
- `capture_page_topology(named_pages, output_root)`
- `batch_capture_replicate._metadata_panel_signature(locator_factory)` 的现有签名/算法（当前为 private，只能作为提升 public helper 的复制依据）
- `batch_capture_replicate._wait_for_metadata_panel_state(page, locator_factory, timeout_s, stable_s)` 的现有签名/算法（当前为 private，只能作为提升 public helper 的复制依据）
- `skills._shared.canvas_capture._canvas_hash()` 和 `_wait_for_frame_change()` 的等待模式
- `ReplicaState`、`ReplicaTransition`、`ActionTarget`、`InteractionRegion`
- builder 中 manifest 声明式 transition 和本地相对 URL 路由

### 测试文件存在性核验

- 已存在、后续应 Modify：`test_qt_workflow.py`、`test_replay_script.py`、`test_replica_regions.py`、`test_replica_manifest.py`、`test_replica_runtime.py`、`test_replica_e2e.py`、`test_replica_gui.py`、`test_pipeline_report.py`、`test_pipeline_orchestrator.py`、`test_orchestrator_events.py`、`test_pipeline_preflight.py`。
- 当前不存在、后续应 Create：`test/test_multi_series_capture.py`。
- 实施任一 Phase 前仍需用 `rg --files test` 重新确认文件状态，因为计划与实施可能跨 commit。

### 全局反模式

- 不把 `select_series()` 的 `max()` 简单改成 `for` 循环后就称为完成。
- 不让 Replica capture 直接执行 completed adapter。
- 不使用 `page.evaluate() + iframe.contentDocument`；Frame DOM 只能通过 Playwright `Frame`/`FrameLocator`/Locator 访问。
- 不缓存虚拟列表首次枚举得到的 Locator、ElementHandle 或绝对坐标；每次点击前必须重新解析。
- 不用序列名作为唯一 ID；同名序列必须可区分。
- 不用固定 sleep 作为唯一就绪条件。
- 不在同一个 Viewer 会话中并行点击多个序列。
- 不把 `reached_end=false` 的虚拟列表称为“全部序列”。
- 不把患者姓名、检查号、token 或 SeriesInstanceUID 原文写入公开文件名、日志或报告。

---

## Phase 0：真实站契约 Spike 与设计冻结

**目的：** 在编码前关闭 click/dblclick、iframe 重建、Metadata 刷新/关闭和稳定信号等真实站未知项。

**文件：**

- Create: `docs/superpowers/specs/2026-08-14-multi-series-replica-expansion-design.md`
- Modify: `skills/_shared/viewers.yaml`
- Create: `test/fixtures/multi_series/`（匿名化本地 fixture）
- Local-only evidence: `out/{hospital}/multi_series_spike/`

### Task 0.1：写设计契约

- [ ] 阅读 `docs/REPLICA_DESIGN.md` 中“Interaction Region”“序列区域”“Metadata 区域”“状态产生规则”“坐标与滚动容器”章节和本计划全部内容。
- [ ] 在设计文档固定术语：`series hub`、`series descriptor`、`viewer state`、`metadata state`、`series branch`、`complete/partial/failed`。
- [ ] 固定人工录制模板：选择一个序列 → 等待 Viewer → 打开 Metadata → 等待内容 → **关闭 Metadata**。
- [ ] 固定 MVP 输出：每序列一个 Viewer 静态状态和一个 Metadata 状态；不含全帧。
- [ ] 固定“全部”的定义：只有 discovery `reached_end=true` 且每个 descriptor 都有终态，才可报告 complete。

### Task 0.2：cxhospital/uicloud 真实站只读 Spike

- [ ] 分别选择至少两个序列，记录实际激活动作是 click 还是 dblclick。
- [ ] 验证切换序列是否销毁或重建 iframe；若会重建，明确每轮必须重新发现 Page/Frame。
- [ ] 验证至少两种 Viewer 稳定证据：选中态、当前序列文本/帧数、canvas hash、截图非空。
- [ ] 比较两个序列的 Metadata fingerprint，确认面板是 Study 级、Series 级还是 image 级。
- [ ] 验证 Metadata 打开和关闭 locator，并确认关闭后序列列表仍可操作。
- [ ] 验证虚拟列表的滚动容器、item selector、稳定属性和节点复用行为。
- [ ] 将 selector/行为结论写入 `viewers.yaml`，不把患者文本写入配置。

### Task 0.3：建立匿名化 fixture

- [ ] fixture 至少包含 3 个序列，其中一个仅在滚动后出现。
- [ ] 至少两个序列同名但稳定属性不同。
- [ ] 滚动时复用 DOM 节点，模拟虚拟列表。
- [ ] 每个序列 Viewer 区域具有独有 DOM 标记和截图差异。
- [ ] 每个序列 Metadata 面板具有独有 SeriesNumber/SeriesDescription/SeriesInstanceUID 测试值。
- [ ] 提供 popup + 两层 iframe 变体。

**验证清单：**

- [ ] 设计文档明确回答所有真实站未知项；无法确认的项有明确 fallback 和 partial 语义。
- [ ] fixture 可由本地 HTTP server 打开，不访问真实医院网络。
- [ ] Spike 证据只保存在 `out/`，不提交患者内容。

**反模式守卫：**

- 不基于单一医院写死最终抽象。
- 不把真实患者 DOM、截图或 UID 提交到仓库。
- 不在未知 Metadata 语义下假设每次切序列都会变化。

---

## Phase 1：修复嵌套 iframe 序列区域捕获

**目的：** 确保主页面、popup、单层和多层 iframe 都通过同一 marker-aware series harvest 路径。

**文件：**

- Modify: `test/test_batch_capture_replicate.py`
- Modify: `test/test_capture_snapshot.py` 或 `test/test_replica_regions.py`
- Modify: `batch_capture_replicate.py` 中 `LiveCaptureSession._capture()` 的 marker region 路由
- Verify: `capture_snapshot.py` 中 `capture_series_interaction_region()` 与 `capture_marker_interaction_region()`

### Task 1.1：先写失败测试

- [ ] 创建两层 iframe fixture，序列 target 位于最内层 Frame，列表包含多个 item。
- [ ] 调用 `LiveCaptureSession.before(..., marker_label="序列选择")`。
- [ ] 从 `topology.json` 加载最内层 `ReplicaDocument`。
- [ ] 断言该 document 有 `region_type="series"`。
- [ ] 断言 region 包含全部 item，并具有非空 `SeriesCollectionEvidence`。
- [ ] 断言 source scrollTop 在 capture 后恢复。

### Task 1.2：运行测试并确认 RED

```powershell
D:/Anaconda/envs/codegen-marker/python.exe -m unittest test.test_batch_capture_replicate.BatchCaptureReplicateTests.test_nested_frame_series_uses_scroll_harvest -v
```

预期：失败，因为嵌套 frame 的非 Metadata marker 当前走 `capture_interaction_region(target_locator.locator("xpath=.."), ...)`。

### Task 1.3：复制 Metadata owning-document 模式修复路由

- [ ] 从 `LiveCaptureSession._capture()` 的 Metadata owning-document 分支（当前约 325–332 行）复制 scope 模式。
- [ ] 对 `marker_label == "序列选择"` 调用 `capture_marker_interaction_region()`，使其进入 `capture_series_interaction_region()`。
- [ ] 保持 generic/layout/WLWW/canvas 现有路径不变。
- [ ] 不通过 top-level DOM 访问子 iframe。

### Task 1.4：验证 GREEN 和回归

```powershell
D:/Anaconda/envs/codegen-marker/python.exe -m unittest test.test_batch_capture_replicate test.test_capture_snapshot test.test_replica_regions -v
```

**验收：** 主文档与嵌套 iframe 都产生相同语义的 series region；现有 Metadata nested-frame 测试仍通过。

**反模式守卫：** 不在这一阶段引入探索循环或 schema v2；保持补丁单一职责。

---

## Phase 2：序列发现、稳定身份与完整性证据

**目的：** 将现有 scroll harvest 拆成可被普通 snapshot 和自动探索共同调用的确定性 discovery API。

**文件：**

- Modify: `replica_models.py` 中 `RegionMember`、`SeriesCollectionEvidence` 附近
- Modify: `capture_snapshot.py` 中 `capture_series_interaction_region()`
- Modify: `test/test_replica_regions.py`
- Optional Modify: `skills/_shared/viewers.yaml`

### Task 2.1：定义 discovery 数据契约

建议新增内部/可序列化模型，最终名称由实现者保持一致：

```python
@dataclass
class SeriesDescriptor:
    series_key: str
    label: str
    ordinal: int
    document_id: str
    member_id: str
    stable_attributes: dict[str, str]
    selected: bool
    explicit_frame_count: int | None
    inferred_frame_count: int | None
    activation: str | None
```

- [ ] `series_key` 优先使用 `data-series-uid/data-series/data-uid/value/稳定 id`。
- [ ] 没有稳定属性时，使用规范化文本 + Frame path/document id + 同名 occurrence index。
- [ ] 若 Metadata 后续取得 SeriesInstanceUID，用 SHA-256 前缀升级内部关联，但不改公开文件名为原始 UID。
- [ ] `SeriesCollectionEvidence` 增加或关联 `discovered_count`、`reached_end`、`warning`；旧字段保持兼容。

### Task 2.2：写 discovery 失败测试

- [ ] 普通列表：全部 item、DOM 顺序、选中态正确。
- [ ] 虚拟列表：滚动节点复用但三个逻辑序列都能去重收集。
- [ ] 同名序列：生成不同 `series_key`。
- [ ] 无稳定属性：fallback key 在同一次 capture 内确定。
- [ ] 达到底部：`reached_end=true`。
- [ ] 超过步数或时间预算：`reached_end=false` 且 warning=`series_virtualized_partial`。
- [ ] 捕获结束：恢复原 `scrollTop`。

### Task 2.3：实现 `discover_series_candidates()`

建议签名：

```python
def discover_series_candidates(
    root_locator: Any,
    document_id: str,
    max_scroll_steps: int = 40,
    max_duration_s: float = 10.0,
) -> tuple[list[SeriesDescriptor], list[RegionMember], SeriesCollectionEvidence]:
    ...
```

- [ ] 从 `capture_series_interaction_region()` 复制现有滚动、去重和 finally 恢复逻辑。
- [ ] 将现有 `capture_series_interaction_region()` 改为调用 discovery API，而不是保留第二套算法。
- [ ] 每步滚动后使用有界 DOM 稳定等待，不只固定 sleep。
- [ ] 连续两步无新增、到达底部、40 步或 10 秒任一条件满足即结束。
- [ ] 不把 `body` 中所有带 CT/Body 文本的元素当成列表 item；先使用结构容器和 viewer adapter selectors。

### Task 2.4：验证

```powershell
D:/Anaconda/envs/codegen-marker/python.exe -m unittest test.test_replica_regions test.test_capture_snapshot -v
```

**验收：** descriptor 数量与 region members 可审计对应；`source_member_id`/`member_id` 唯一；partial 不会被误报 complete。

**反模式守卫：** 不缓存 Locator；descriptor 只能保存稳定描述，不保存跨滚动后会失效的元素引用。

---

## Phase 3：Manifest schema v2 与分支模型

**目的：** 用 O(N) 的 branch route 描述多序列状态，不把每个状态复制成 N 条 action/transition。

**文件：**

- Modify: `replica_models.py` 中 `ReplicaTransition`、`ReplicaState`、`ReplicaFlow.to_dict()/from_dict()`
- Modify: `replay_helpers.py` 中 manifest read/write helpers
- Modify: `pipeline_validation.py` 中 `validate_manifest()`
- Modify: `test/test_replica_manifest.py`
- Modify: `test/test_pipeline_validation.py`

### Task 3.1：写 schema v2 失败测试

- [ ] v2 flow 含两个 `SeriesBranch`，各自引用 Viewer state 和 Metadata state。
- [ ] 直接调用 `ReplicaFlow.to_dict()` 后断言新增的 `series_branches` 与 `series_expansion` 已完整序列化。
- [ ] JSON write/read 后 branch 字段完全一致，覆盖 `to_dict()`、`write_manifest()`、`read_manifest()`、`from_dict()` 整条 round-trip。
- [ ] v1 fixture 仍可读取，`series_branches=[]`。
- [ ] 明确保留现状回归：当前 `ReplicaFlow.from_dict()` 以 `schema_version != 1` 硬拒绝 v2；实现后 v1 必须走默认值兼容路径，v2 才进入新增解码路径，未知版本仍抛错。
- [ ] 重复 `series_key`、不存在的 state、错误 source member、非法 capture status 被 validation 拒绝。
- [ ] captured branch 必须有 `viewer_state_id`；failed branch 可以没有 state，但必须有 warning/reason。

### Task 3.2：新增 branch 模型

建议模型：

```python
@dataclass
class SeriesBranch:
    branch_id: str
    series_key: str
    label: str
    ordinal: int
    document_id: str
    source_member_id: str
    selector: LocatorRecipe | None
    activation: str
    viewer_state_id: str | None
    metadata_state_id: str | None
    return_state_id: str | None
    capture_status: str
    warning: str | None

@dataclass
class SeriesExpansionEvidence:
    discovered_count: int
    captured_count: int
    partial_count: int
    failed_count: int
    reached_end: bool
    total_duration_ms: int
    warning: str | None
```

- [ ] `ReplicaFlow` 增加 `series_branches` 和 `series_expansion`。
- [ ] schema_version 升为 2；将当前 `schema_version != 1` 的硬判断改成明确支持 `{1, 2}`，其他版本继续拒绝。
- [ ] v1 缺少字段时填空列表/None，不回写伪造数据。
- [ ] 确认现有 `to_dict()` 的 `asdict(self)` 能覆盖新增 dataclass 字段，并用 Task 3.1 的直接断言和 manifest round-trip 双重验证。
- [ ] 所有 state ID 和 branch ID 保持 manifest 内稳定且唯一。

### Task 3.3：扩展 validation

- [ ] 验证 `series_key` 唯一。
- [ ] 验证 branch 引用的 viewer/metadata/return state 存在。
- [ ] 验证 `metadata_state_id` 存在时 `return_state_id == viewer_state_id`。
- [ ] 验证 `captured + partial + failed == discovered`。
- [ ] `reached_end=false` 时 flow 必须有 `series_virtualized_partial` 或等价 warning。
- [ ] 输出 `series_discovered/captured/partial/failed` metrics。

### Task 3.4：验证

```powershell
D:/Anaconda/envs/codegen-marker/python.exe -m unittest test.test_replica_manifest test.test_pipeline_validation -v
```

**反模式守卫：** 不用 ordinal 推断 Metadata 返回状态；分支返回必须显式建模。

---

## Phase 4：录制模板识别与探索触发协议

**目的：** 从一个人工示教流程中确定序列激活、Metadata 打开和 Metadata 关闭动作，并在安全边界触发探索。

**文件：**

- Modify: `replica_annotations.json` schema handling（实际代码位置以 `main_gui.py`/pipeline model 为准）
- Modify: `pipeline_models.py`
- Modify: `pipeline_preflight.py`
- Modify: `rewrite_script.py` 中 `parse_action_plan()`
- Modify: `batch_capture_replicate.py` 中 `instrument_marked_actions()` 和 runtime hook exports
- Modify: `test/test_replay_script.py` 或 `test/test_batch_capture_replicate.py`

### Task 4.1：增加 opt-in 配置

建议配置：

```json
{
  "expand_all_series": true,
  "max_series": 40,
  "per_series_timeout_s": 20,
  "total_series_timeout_s": 600,
  "viewer_capture_mode": "first_stable_frame"
}
```

- [ ] 默认 `expand_all_series=false`，旧录制行为不变。
- [ ] preflight 校验必须存在一个序列选择模板。
- [ ] MVP 中必须识别 Metadata open 和 close；缺少 close 时 preflight 失败或明确降级为 series-only，不静默猜测。
- [ ] 预算必须为正数并有产品级上限。

### Task 4.2：写插桩失败测试

- [ ] 构造 processed script：序列 click/dblclick → Metadata open → Metadata close → browser close。
- [ ] 断言正常 before/after hook 保持原顺序且原 action 只执行一次。
- [ ] 明确采用独立的 `capture_hook_expand_series(...)`：它不是 `capture_hook_after()` 内部的隐式副作用，而是由 `instrument_marked_actions()` 在被指定为触发点的 Metadata close action 成功执行后，紧跟该 action 的既有 `capture_hook_after()`，写入同一个生成式 `else:` 分支。
- [ ] 断言生成顺序严格为：原 close action（仅一次）→ 该 action 的 `capture_hook_after()` → 独立 `capture_hook_expand_series()`；close action 抛错时二者均不得触发探索。
- [ ] 断言 expansion hook 位于最后一个模板动作成功路径之后、`context.close()`/`browser.close()` 之前。
- [ ] `expand_all_series=false` 时不插 expansion hook。
- [ ] 缺少模板动作时 preflight 给出可读错误，不生成错误脚本。

### Task 4.3：实现模板分类

- [ ] 从 `parse_action_plan()` 的 marker groups 读取序列和 Metadata 动作，不从 completed script 反推。
- [ ] 在 live session 中根据 Metadata panel before/after visibility 将动作分类为 open/close。
- [ ] 继承人工序列 action 的 click 或 dblclick，不自行改变。
- [ ] `instrument_marked_actions()` 的 import header 显式加入 `capture_hook_expand_series`；该 hook 只传稳定 action recipe/locator factory/page var，不执行 LLM 决策。
- [ ] `capture_hook_expand_series` 只负责调用 session 的 expansion 入口；既有 `capture_hook_after` 继续只负责单 action after snapshot，避免两个职责混在一起。

### Task 4.4：验证

```powershell
D:/Anaconda/envs/codegen-marker/python.exe -m unittest test.test_batch_capture_replicate test.test_pipeline_preflight -v
```

**反模式守卫：** 不在浏览器已关闭后的 manifest build 阶段尝试访问网页；探索必须发生在 instrumented replay 子进程内。

---

## Phase 5：单序列事务与全序列探索器

**目的：** 在同一已登录 session 中串行采集所有 descriptor，并做到组合等待、失败隔离和状态恢复。

**文件：**

- Modify: `batch_capture_replicate.py` 中 `LiveCaptureSession`、现有 Metadata readiness helpers 和新增 expansion hook
- Modify: `capture_snapshot.py`
- Create: `capture_readiness.py`
- Create: `test/test_capture_readiness.py`
- Reuse pattern: `skills/_shared/canvas_capture.py` 中 `_canvas_hash()` / `_wait_for_frame_change()`
- Reuse pattern: `batch_capture_replicate.py` 中现有 `_metadata_panel_signature()` / `_wait_for_metadata_panel_state()`
- Create: `test/test_multi_series_capture.py`

### Task 5.1：定义原始采集目录

建议：

```text
capture/
  series_branches/
    {safe_series_key}/
      descriptor.json
      viewer/
        topology.json
        ...screenshots...
      metadata/
        topology.json
        ...screenshots...
      metadata_rows.json
      status.json
  series_capture_manifest.json
```

- [ ] `safe_series_key` 使用内部 hash/slug，不含原始 UID、患者姓名和检查号。
- [ ] `status.json` 保存 `captured/partial/failed/skipped_duplicate`、失败 stage 和异常类型。
- [ ] 不在日志输出完整 Metadata 文本。

### Task 5.2：先提升 Metadata readiness 为共享 public helper

- [ ] 将 `batch_capture_replicate._metadata_panel_signature()` 和 `_wait_for_metadata_panel_state()` 的算法复制到无循环依赖的 `capture_readiness.py`，命名为 public API，例如 `metadata_panel_signature()` 与 `wait_for_metadata_panel_state()`。
- [ ] `batch_capture_replicate.py` 改为导入并调用 public helper；删除旧 private 实现，避免两套逻辑漂移。
- [ ] 在 `test/test_capture_readiness.py` 复制现有异步面板稳定测试，覆盖内容延迟加载、signature 连续稳定、超时和 locator 失效。
- [ ] 运行既有 Metadata 测试，确认函数提升没有改变行为。

```powershell
D:/Anaconda/envs/codegen-marker/python.exe -m unittest test.test_capture_readiness test.test_batch_capture_replicate -v
```

### Task 5.3：写单序列事务失败测试

测试 `capture_one_series(...)` 或最终采用的等价 API：

- [ ] 每次调用前重新解析 Page/Frame 和序列 locator。
- [ ] 虚拟列表滚动后能重新定位目标。
- [ ] 继承 click/dblclick。
- [ ] 等待 selected-state + Viewer 变化/稳定。
- [ ] 捕获 Viewer topology、截图和相关 region DOM。
- [ ] 打开 Metadata，等待 signature 稳定，捕获完整 outerHTML/解析行。
- [ ] 关闭 Metadata 后面板不可见。
- [ ] finally 恢复 scrollTop 和可操作 hub。

### Task 5.4：实现组合就绪条件

序列切换成功至少需要以下证据中的两个，且不得只靠固定 sleep：

- 目标 `aria-selected=true` 或 active/current class 改变；
- 当前序列名称或帧计数匹配目标；
- canvas hash 相对前一序列变化；
- Viewer DOM fingerprint 稳定；
- Viewer 截图通过非空/非全黑检查。

- [ ] 从 `skills/_shared/canvas_capture.py` 的 `_canvas_hash()` / `_wait_for_frame_change()` 复制 canvas hash/轮询模式，不导入其私有函数作为长期公共 API；如最终跨模块复用，应与 Metadata readiness 一样提升为共享 public helper。
- [ ] 第一次动作无任何变化证据时最多重试一次。
- [ ] 若 viewer 本来已经是目标序列，允许 selected-state + 稳定证据直接成功，不强制 hash 变化。

### Task 5.5：实现 Metadata 事务

- [ ] 复用 Task 5.2 提升后的 public `wait_for_metadata_panel_state()`，不直接跨模块导入旧 private 名称，也不另写第二套等待器。
- [ ] 捕获完整 panel `outerHTML`，同时保存本地解析的 tag/value 供审计。
- [ ] 至少提取并校验 SeriesNumber、SeriesDescription、SeriesInstanceUID（存在时）。
- [ ] 使用 Metadata UID hash 校验临时 descriptor，但原 UID 不进入文件名。
- [ ] Metadata 没变化但 Viewer 已成功时将 branch 标为 partial，而不是整条 failed。

### Task 5.6：实现 `LiveCaptureSession.finalize_series_branches()`

- [ ] 保存最初 selected series、scrollTop、panel open/closed 状态。
- [ ] 在总预算内依 ordinal 串行遍历 descriptor。
- [ ] 每个 descriptor 独立 try/finally。
- [ ] 单条失败写 status 并继续。
- [ ] 连续三条都因 hub 无法恢复失败时允许一次受控 reload/bootstrap 恢复；第二次仍失败则停止并标记 overall partial/failed。
- [ ] 最后恢复原始序列、scrollTop 和 panel 状态。
- [ ] 写 `series_capture_manifest.json`，满足计数守恒。

### Task 5.7：运行 fixture 测试

```powershell
D:/Anaconda/envs/codegen-marker/python.exe -m unittest test.test_multi_series_capture -v
```

**验收：** 三个 fixture 序列全部有终态；一个 Metadata 超时只影响对应 branch；探索后原状态恢复。

**反模式守卫：** 不在同一 Page 并行探索；不跨患者/检查导航；不把失败项静默从 discovered 列表删除。

---

## Phase 6：Branch snapshots 合并为 ReplicaFlow

**目的：** 将探索证据构造成普通 `ReplicaState`/`ReplicaDocument`，并建立 O(N) 的 `SeriesBranch` 路由。

**文件：**

- Modify: `batch_capture_replicate.py` 中 `_load_snapshot_state()`、`build_flow_from_snapshots()` 及相邻 snapshot loaders
- Modify: `test/test_batch_capture_replicate.py`
- Modify: `test/test_pipeline_validation.py`

### Task 6.1：写 flow 构建失败测试

- [ ] 输入一个录制主路径和两个成功 branch snapshots。
- [ ] 输出保留原 entry/录制 states。
- [ ] 每个 branch 有唯一 Viewer state。
- [ ] 每个 Metadata 成功 branch 有唯一 Metadata state。
- [ ] Viewer state 中存在 Metadata trigger `ActionTarget` 和到该 Metadata state 的 transition。
- [ ] Metadata close transition 显式返回同 branch 的 Viewer state。
- [ ] failed branch 保留在 `series_branches`，但不引用不存在的 state。
- [ ] branch state ordinal 仅用于稳定输出，不能决定语义返回路径。

### Task 6.2：实现 branch loader/builder

- [ ] 增加 `_load_series_branch_snapshots()` 或等价集中入口。
- [ ] 复用同属 `batch_capture_replicate.py` 的 `_load_snapshot_state()` 及 `ReplicaDocument.from_dict()`，不误从 `build_replica.py` 导入，也不写第二套 topology decoder。
- [ ] 为 synthetic action ID 使用稳定命名，例如 `series:{branch_id}:activate/meta_open/meta_close`。
- [ ] 仅在 captured/partial 且具有有效 Viewer snapshot 时创建 Viewer state。
- [ ] 将 discovery、capture 和 validation warnings 汇总到 `ReplicaFlow.warnings`。

### Task 6.3：验证

```powershell
D:/Anaconda/envs/codegen-marker/python.exe -m unittest test.test_batch_capture_replicate test.test_pipeline_validation -v
```

**反模式守卫：** 不用截图 hash 自动合并语义不同但视觉相同的序列；去重只能复用 asset，不能合并 branch identity。

---

## Phase 7：Builder 与 Runtime 多序列路由

**目的：** 让所有成功序列在任何 Viewer state 中都是真正可点击的，并正确进入对应 Viewer/Metadata state。

**文件：**

- Modify: `build_replica.py` 中 `RUNTIME`
- Modify: `build_replica.py` 中 `_render_document()`
- Modify: `build_replica.py` 中 `build_replica()` 的 state/document 输出与 route map 组装
- Modify: `test/test_build_replica.py`
- Modify: `test/test_replica_runtime.py`
- Modify: `test/test_replica_e2e.py`

### Task 7.1：写 runtime 失败测试

- [ ] 构建 A/B/C 三个 series branches。
- [ ] 从 A 点击 B，URL/state 进入 `viewer_state_B`。
- [ ] B 对应 option `aria-selected=true`，其他为 false。
- [ ] 页面显示 B 独有 Viewer DOM 和 B 截图 asset。
- [ ] 从 B 点击 Metadata，进入 `metadata_state_B`。
- [ ] Metadata B 包含 B 独有 tag/value。
- [ ] 关闭 Metadata 后准确回到 B，而不是前一个 ordinal state。
- [ ] 从 B 可直接点击 C，不必先返回统一 hub。
- [ ] failed branch 可见但 `aria-disabled=true`，不会假跳转。

### Task 7.2：为 series member 绑定 route key

- [ ] 在 `_render_document()` 的 region member 渲染处识别 `region_type="series"`。
- [ ] 使用 `SeriesBranch.source_member_id` 给节点增加 `data-replica-series-key`。
- [ ] 保留原始 role/aria-selected；缺失时生成可访问的 `role="option"`。
- [ ] 不生成透明负坐标 hitbox；虚拟列表离屏成员使用正常可滚动布局。

### Task 7.3：注入 O(N) route map

- [ ] 每个含 series region 的 Viewer document 注入同一份安全 JSON route map。
- [ ] runtime 点击 `[data-replica-series-key]` 时查 route 并导航。
- [ ] 保留当前 runtime 对已录制、已有 `data-replica-action` transition 的序列 target 的正常导航；新增 route map 只补齐未录制、没有 transition 的普通 series members。
- [ ] 导航前更新 aria-selected，只作为即时反馈；最终内容以目标 state 为准。
- [ ] route 缺失或 branch failed 时不导航，并保持可审计 disabled 状态。

### Task 7.4：Metadata 显式返回

- [ ] 删除/停用对“前一个 ordinal state”的分支返回推断。
- [ ] builder 使用 branch `return_state_id` 生成 close URL。
- [ ] 录制主路径中非 branch Metadata 继续使用现有兼容行为，直到显式迁移。

### Task 7.5：验证

```powershell
D:/Anaconda/envs/codegen-marker/python.exe -m unittest test.test_build_replica test.test_replica_runtime test.test_replica_e2e -v
```

**反模式守卫：** 不把 aria-selected 变化当作内容切换成功；必须断言目标 state、独有 DOM 和 asset 都变化。

---

## Phase 8：GUI、Pipeline 事件与报告

**目的：** 让用户明确选择全序列探索、看到预算和覆盖率，并可区分 complete/partial/failed。

**文件：**

- Modify: `main_gui.py`
- Modify: `pipeline_models.py`
- Modify: `pipeline_orchestrator.py`
- Modify: `orchestrator_events.py`
- Modify: `pipeline_report.py`
- Modify: `pipeline_preflight.py`
- Modify: `test/test_replica_gui.py`
- Modify: `test/test_pipeline_orchestrator.py`
- Modify: `test/test_orchestrator_events.py`
- Modify: `test/test_pipeline_report.py`
- Modify: `test/test_pipeline_preflight.py`

以上测试文件已在 2026-08-14 计划修订时确认存在；实施前仍需运行 `rg --files test` 防止跨 commit 漂移。

### Task 8.1：增加用户配置

- [ ] “自动探索全部序列”默认关闭。
- [ ] 展示最大序列数、单序列超时、总超时和 capture mode。
- [ ] MVP capture mode 只提供/默认 `first_stable_frame`；不要显示尚未实现的全帧选项为可用。
- [ ] 配置写入 run 输入/annotation，旧 run 读取时有安全默认值。

### Task 8.2：增加进度事件

建议事件：

```text
series_discovery_started
series_discovered
series_capture_started
series_capture_completed
series_capture_partial
series_capture_failed
series_expansion_completed
```

- [ ] 每条事件只输出安全 branch id、ordinal、计数和错误类型，不输出患者文本或完整 Metadata。
- [ ] GUI 显示 `discovered / captured / partial / failed`。
- [ ] 用户取消时停止下一条序列，当前条执行 finally 恢复后退出。

### Task 8.3：报告覆盖率

- [ ] complete：`reached_end=true` 且无 partial/failed。
- [ ] partial：列表未确认到底、某 branch partial/failed、预算耗尽或恢复失败。
- [ ] failed：没有任何可用 Viewer branch，或探索基础设施失败。
- [ ] 报告列出安全 branch id 和失败 stage，不嵌患者 Metadata/截图。

### Task 8.4：验证

```powershell
D:/Anaconda/envs/codegen-marker/python.exe -m unittest test.test_replica_gui test.test_pipeline_orchestrator test.test_orchestrator_events test.test_pipeline_report test.test_pipeline_preflight -v
```

**反模式守卫：** 不因部分失败把整个正常录制主路径删除；Replica 仍可构建，但总状态必须诚实降级。

---

## Phase 9：安全、资产预算与断点策略

**目的：** 控制真实站压力、患者数据暴露、采集时长和产物体积。

**文件：**

- Modify: `pipeline_preflight.py`
- Modify: `pipeline_validation.py`
- Modify: `replay_helpers.py`
- Modify: `.gitignore`（若缺少对应规则）
- Modify: `docs/PIPELINE_RUNBOOK.md`
- Modify: `docs/MULTI_HOSPITAL_REPLICA_RUNBOOK.md`

### Task 9.1：预算和速率

- [ ] 串行采集，默认单序列 20 秒、总计 10 分钟、最多 40 条；真实默认值以 Phase 0 Spike 校准。
- [ ] 每次切换后等待稳定，不进行无间隔快速点击。
- [ ] 达到总预算时停止新 branch，剩余 descriptor 标为 `skipped_budget`/partial。
- [ ] 单序列只保存一个稳定 Viewer screenshot；重复 asset 继续使用现有 SHA-256 去重。

### Task 9.2：患者数据保护

- [ ] `capture/series_branches`、manifest、replica、截图和 metadata 继续视为敏感医疗数据。
- [ ] URL/token 属性沿用 `sanitize_html()` 和 URL redaction。
- [ ] 日志、事件、公开报告不包含患者姓名、检查号、UID、Metadata 原文。
- [ ] 验证相关目录被 `.gitignore` 覆盖。

### Task 9.3：断点和失败恢复边界

- [ ] MVP 可以先只支持整个 exploration 重跑；若实现 branch resume，必须校验 source script hash、annotation hash、descriptor fingerprint 和 viewer config 版本。
- [ ] 不复用另一个患者/检查的 branch snapshot。
- [ ] reload/bootstrap 恢复只允许一次且必须留审计 warning。

### Task 9.4：验证

- [ ] 构造 50 条序列 fixture，确认 max_series/总预算有效。
- [ ] 检查生成日志不含测试 UID/患者字段。
- [ ] 检查重复 Viewer asset 只存一份 hash 文件。
- [ ] 检查 `git status --short` 不出现 out/capture/replica 患者产物。

---

## Phase 10：最终验证与真实站验收

**目的：** 证明实现符合文档、无已知反模式、旧流程不回归，并在真实 viewer 上闭环。

### Task 10.1：文档/API 一致性审查

- [ ] 对照 Phase 0 设计文档检查每个新增模型、配置项、事件名和状态语义。
- [ ] 检查所有调用使用真实存在的 Playwright API。
- [ ] 检查 schema v1/v2 的读取、写入和 validation 行为一致。
- [ ] 检查所有 branch 返回使用显式 `return_state_id`。

### Task 10.2：反模式 grep

```powershell
rg -n "contentDocument|contentWindow.*document" batch_capture_replicate.py capture_snapshot.py skills
rg -n "time\.sleep\(|wait_for_timeout\(" batch_capture_replicate.py capture_snapshot.py
rg -n "SeriesInstanceUID|PatientName|Accession" pipeline_report.py orchestrator_events.py
```

- [ ] 第一条不得出现新增 iframe DOM 访问反模式。
- [ ] 第二条逐项确认固定等待只作短暂兜底，不是唯一成功条件。
- [ ] 第三条确认事件/报告没有输出敏感值。

### Task 10.3：Focused tests

```powershell
D:/Anaconda/envs/codegen-marker/python.exe -m unittest test.test_replica_regions test.test_capture_snapshot test.test_batch_capture_replicate test.test_multi_series_capture test.test_replica_manifest test.test_build_replica test.test_replica_runtime test.test_replica_e2e test.test_pipeline_validation -v
```

预期：全部 PASS。

### Task 10.4：完整回归

```powershell
D:/Anaconda/envs/codegen-marker/python.exe -m unittest discover -s test -v
D:/Anaconda/envs/codegen-marker/python.exe -m py_compile capture_snapshot.py replica_models.py batch_capture_replicate.py build_replica.py pipeline_validation.py
git diff --check
```

预期：全部退出码 0；若存在预先已有失败，必须提供与本改动无关的证据，不能简单忽略。

### Task 10.5：离线 E2E 验收

- [ ] fixture Replica 断网运行时外部 HTTP(S) 请求数为 0。
- [ ] 点击每个 captured branch 都进入正确 Viewer state。
- [ ] `aria-selected`、独有 Viewer DOM、截图 hash 三者与目标 branch 一致。
- [ ] 每个 Metadata 按钮打开对应 branch 的完整可滚动面板。
- [ ] `extract_meta_from_frame()` 能从离线 Metadata DOM 解析预期行。
- [ ] Metadata 关闭返回同一 Viewer branch。
- [ ] failed/partial branch 不会伪装成成功。

### Task 10.6：真实站 smoke test

对 cxhospital 和 uicloud 各选一个 3–5 序列的检查：

- [ ] discovered 数量与真实列表一致，或明确记录 `reached_end=false`。
- [ ] `captured + partial + failed == discovered`。
- [ ] 每个成功 branch 的 Metadata identity 与目标序列一致。
- [ ] 复刻页面可任意点击 3–5 个序列，不需要按采集顺序。
- [ ] 关闭 Metadata 返回正确序列。
- [ ] 原录制路径、Adapter 生成和非 expansion capture 仍工作。

---

## 二、完成定义

只有同时满足以下条件，MVP 才算完成：

- [ ] 用户只录制一个完整序列模板，无需人工逐个点击其余序列。
- [ ] 所有在预算内完整发现的序列都有明确 capture 终态。
- [ ] 所有成功序列在离线 Replica 中可从任意 Viewer branch 点击进入。
- [ ] 每个成功序列展示自己的 Viewer 静态视觉状态、语义 DOM 和 Metadata DOM。
- [ ] Metadata 关闭准确返回当前序列。
- [ ] iframe/popup/虚拟列表/同名序列/单项失败均有测试。
- [ ] v1 manifest 和旧录制流程不回归。
- [ ] partial/failed/预算耗尽均可见、可审计，不静默缺失。
- [ ] 离线 Replica 不依赖真实站网络或原站 JavaScript。
- [ ] 患者数据不进入日志、公共报告、配置和源码仓库。

## 三、明确不在本计划 MVP 内

- 所有序列的逐帧 DICOM 截图和帧级交互。
- 离线运行原站 DICOM/WebGL viewer。
- 自动诊断或自动选择“最佳”医学序列。
- 跨患者、跨检查、跨页面的无界 crawler。
- Shadow DOM/canvas-only 序列栏的无条件完整枚举；无法读 DOM 时只能作为后续受限 VL fallback，并必须标 partial。

## 四、交给下一轮审阅 agent 的重点问题

1. schema v2 的 `SeriesBranch` 是否足够，还是应把 Metadata route 独立成通用 branch transition？
2. O(N) 全局 `series_key -> state` route 是否与现有 generated-page/iframe URL 结构完全兼容？
3. 独立 `capture_hook_expand_series()` 紧跟 Metadata close 的既有 `capture_hook_after()`、并位于同一个成功 `else:` 分支的机制是否足够稳健；是否仍有证据要求显式新 marker？
4. 如何在不缓存 Locator 的前提下，用 descriptor 在虚拟列表中可靠重新定位同名项？
5. “selected + canvas/DOM 两类证据”是否会在现有三类 Viewer 中产生假失败？
6. Metadata 没有 SeriesInstanceUID 或面板是 image-level 时，identity 校验如何降级？
7. branch capture 原始目录和 manifest 是否有不必要的数据重复？
8. schema v1 兼容是否应由 v2 reader 完成，还是需要显式 migration 函数？
9. 一次 reload/bootstrap 恢复是否安全，是否可能重复提交或跨检查？
10. MVP 当前强制人工录制 Metadata close；是否有足够跨 Viewer 证据允许后续把 viewer adapter close selector 作为显式降级，而不降低恢复可靠性？

审阅者应按以下格式反馈：Sources consulted、Concrete findings、必须修改项、建议修改项、Confidence + known gaps。没有文件/行号或测试证据的结论不应直接进入实施。
