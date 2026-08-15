# 多序列可点击 Replica 设计契约

日期：2026-08-14
状态：Phase 0.1 设计冻结（契约基线，供后续实现与 Phase 10 一致性审查对照）
范围：`D:\00-Project\04-codegencopy`
前置输入：`docs/superpowers/plans/2026-08-14-multi-series-replica-expansion.md`、`docs/REPLICA_DESIGN.md`、`replica_models.py`、`capture_snapshot.py`、`batch_capture_replicate.py`、`build_replica.py`、`capture_readiness.py`

## 0. 文档用途与约束

本文件是 Phase 0.1 的**设计契约**，不是实现计划。它冻结多序列可点击 Replica 的术语、人工录制模板、MVP 输出边界、数据契约与完成/部分/失败语义，作为以下两方的对齐基准：

1. Phase 1–9 的实现 agent（以本文件 + 主实施计划为准编码）；
2. Phase 10 的一致性审查 agent（用本文件的冻结术语与 API 名核对实现是否偏离）。

本文件**不修改任何 `.py` / `test` 文件**。后续若真实站 Spike（Phase 0.2）推翻本文件中的任一未知项断言，必须回到这里修订契约并注明修订记录，而不是在实现代码里悄悄偏离。

### 术语来源协调

本文件优先采用 `replica_models.py` 与 `capture_snapshot.py` 现有代码中已落地的类名、字段名与函数名作为**权威名称**（它们已在实际代码中定义，见 §10 对齐表）。`docs/REPLICA_DESIGN.md` 中关于 Interaction Region、序列区域、Metadata 区域、状态产生规则与坐标滚动容器的既有约定（录制模板 marker→「序列选择」→「Meta 信息工具」）与 `capture_snapshot.marker_region_type` 的 region_type 取值保持一致；本文档沿用其语义并在此重新钉死，避免跨阶段漂移。

| REPLICA_DESIGN.md 章节语义 | 本文档承接的契约 |
|---|---|
| Interaction Region（5.1 通用规则） | §4 数据契约中的 `InteractionRegion` / `RegionMember` / `SeriesCollectionEvidence` |
| 序列区域（5.2 序列区域） | §3.2「series hub」+ §4 `SeriesDescriptor` / `capture_series_interaction_region` 的 scroll harvest |
| Metadata 区域（5.3 Metadata 区域） | §3.5「metadata state」+ §4 `capture_marker_panel_region` / `capture_readiness` 的等待范式 |
| 状态产生规则（6.3 状态产生规则） | §6 探索时序中每序列「状态证据」组合就绪条件 |
| 坐标与滚动容器（7.2.1 + REPLICA_DESIGN 滚动） | §4 `coordinate_space` 三值 + §6 descriptor 只保存稳定描述、不缓存 Locator |

---

## 1. 目标重申

用户只人工录制**一个**序列的完整流程；live capture 在同一已登录浏览器会话中自动发现并串行采集其余可发现序列；在离线 Replica 中，每个成功采集的序列都能被点击，并展示该序列对应的 Viewer 静态视觉状态、序列/Viewer 语义 DOM 与完整 Metadata DOM。

`processed_script_{hospital}.py` 始终是 Replica live capture 的**唯一示教来源**，绝不改为执行 `completed_*.py`（后者含自动选序列、循环翻帧等行为，会污染人工示教状态机）。

---

## 2. 术语冻结（本文件的最高优先级定义）

以下术语在本计划全过程中语义唯一。任何实现不得在其它词义下复用同名概念。

### 2.1 series hub（序列中枢）

承载序列候选列表的**滚动容器 + 其成员列表**。它是用户录制「序列选择」action 时目标所属的那个系列列表（可能位于主文档、popup 或嵌套 iframe 内）。series hub 具有：

- 一个滚动容器（`Math.max(1, element.clientHeight)` 步进滚动；`scrollHeight > clientHeight` 视为可能虚拟化）；
- 若干 `series member`（候选条目，即 `RegionMember`，`semantic_type` 为序列项 tag）；
- 一个 `SeriesCollectionEvidence`（描述枚举完整性）。

**约束**：series hub 是「可被枚举的候选来源」，不是「全部序列」的断言。枚举不完整（`reached_end=false`）时不得宣称「全部序列」。

### 2.2 series descriptor（序列描述符）

对**一个**已发现的序列候选的**稳定、无定位器**描述（`replica_models.SeriesDescriptor`）：

- 只保存稳定描述（`series_key`、`label`、`ordinal`、`document_id`、`member_id`、`stable_attributes`、`selected`、`explicit/inferred_frame_count`、`activation`）；
- **绝不**保存 Locator、ElementHandle 或绝对坐标——虚拟列表滚动复用 DOM 节点后，这些引用会失效；
- `series_key` 是序列的稳定身份：优先取 `data-series-uid / data-series / data-uid / value / 稳定 id`；无稳定属性时取「规范化文本 + Frame path/document id + 同名 occurrence index」（格式 `{document_id}::{normalized_text}::x{occurrence}`）；
- `activation` 继承人工录制动作的 `click` / `dblclick`，探索器不自行改变。

### 2.3 viewer state（Viewer 静态状态）

序列激活并稳定后，该序列在 Viewer 区域所处的**一个**可复刻静态状态。MVP 中每个成功序列恰好产出一个 viewer state，包含：

- 该序列的 Viewer 稳定截图（JPEG 视觉资产）；
- 序列/Viewer 语义 DOM（canvas hitbox、序列标签、选中态）；
- 必要的 `ActionTarget`（含 Metadata 触发 target 与到 metadata state 的 transition）。

它**不**包含逐帧 DICOM 内容，不承诺医学影像像素随序列在离线端动态渲染。

### 2.4 metadata state（Metadata 状态）

打开 Metadata 面板并等其内容稳定后捕获的状态。包含：

- 完整 Metadata 面板 `outerHTML`（经 sanitize）；
- 本地解析的 tag/value 行（供审计）；
- 至少提取并校验 `SeriesNumber` / `SeriesDescription` / `SeriesInstanceUID`（存在时）；
- 从 metadata state 到其所属 viewer state 的**显式返回 transition**（`return_state_id`）。

面板上 `aria-selected` 不作为 Metadata 内容就绪的唯一证据，必须用内容 signature 稳定等待（§6）。

### 2.5 series branch（序列分支）

一个可发现序列与其捕获结果的**路由单元**（`replica_models.SeriesBranch`）。它把一个 source `SeriesDescriptor`（经 `source_member_id` 关联的 region member）绑定到：

- 该序列的 viewer state（`viewer_state_id`）；
- 可选 metadata state（`metadata_state_id`）；
- 关闭 Metadata 后**显式**返回的 viewer state（`return_state_id`，绝不从 ordinal 推断）；
- 激活方式（`activation`）、捕获状态（`capture_status`）与 warning。

### 2.6 complete / partial / failed（捕获终态）

针对**单条 branch**或**整次发现**，定义如下（§7 展开成裁决规则）：

- **complete**：该序列（或整次探索）对全部目标取得了可用 Viewer 终态（及可用 Metadata 终态，若被要求）；
- **partial**：部分目标成功、部分缺失；或有证据缺失但并非整条失败（例：Viewer 成功但 Metadata 无变化、列表未枚举到底、预算耗尽）；
- **failed**：序列没有任何可用 Viewer 终态，或探索基础设施失败。

---

## 3. 人工录制模板契约（冻结）

### 3.1 固定录制动作序列

用户人工录制**且仅录制**如下动作序列（这是 Shaping 的前置模板，默认 `expand_all_series=false` 时的旧行为保持不变）：

```text
选择一个序列 → 等待 Viewer → 打开 Metadata → 等待内容 → 关闭 Metadata
```

展开为 marker 动作组：

1. **「序列选择」marker**：点击（或双击，按真实站动作）一个序列条目；这是唯一被继承激活方式（click/dblclick）的示教动作。
2. **等待 Viewer**：录制脚本的等待逻辑保证 Viewer 切换到目标序列（录制层面）；live capture 用 §6 的组合证据二次确认。
3. **「Meta 信息工具」marker（open）**：点击打开 Metadata 面板。
4. **等待内容**：录制脚本等待面板内容出现；live capture 用内容 signature 稳定等待（§6）。
5. **「Meta 信息工具」marker（close）**：点击关闭 Metadata 面板——**必须显式录制**，且必须是不同于 open 的、更靠后的动作。

### 3.2 模板必须完整

`RecordingTemplate.complete`（`batch_capture_replicate.classify_recording_template` 的产物）要求：

- 存在一个序列选择 action（继承 click/dblclick）；
- 存在 Metadata open 与 Metadata close 两个**不同**的 Meta 信息工具 action，close 晚于 open。

`instrument_marked_actions` 在 `expand_all_series=true` 且模板不完整时**直接抛错**（`ValueError`），绝不静默降级或猜测 open/close。

### 3.3 探索触发点（时序契约）

独立 expansion hook 的注入时序**严格固定**为：

```text
原 close action（仅执行一次）
  → 该 close action 的 capture_hook_after(...)
  → capture_hook_expand_series(page, series_locator_factory, series_action_id, close_action_id)
```

- 三者位于**同一个**生成式 `else:` 成功分支（`instrument_marked_actions` 第 545–551 行的结构），顺序不能调换；
- close action 抛错时进入 `except` 分支，`capture_hook_after` 与 `capture_hook_expand_series` **都不得触发探索**；
- expansion hook 必须在最后一个模板动作成功路径之后、`context.close()` / `browser.close()` **之前**；
- hook 只传稳定 recipe（locator factory / action id / page var），不做 LLM 决策；
- `<expand_all_series=false>` 时完全不插 expansion hook，旧录制行为逐字节不变。

实现落点：`capture_hook_expand_series`（模块级导出，`batch_capture_replicate.py`）→ 委托 `LiveCaptureSession.expand_series(...)`（Phase 5 在此安装有界串行探索事务）。

---

## 4. 数据契约（模型字段冻结）

以下模型已在 `replica_models.py` 中定义；本文件在此钉死语义，后续实现不得改字段名或含义（Phase 10 按此核对）。

### 4.1 `SeriesDescriptor`

```python
@dataclass
class SeriesDescriptor:
    series_key: str               # 稳定身份；见 §2.2；重复 series_key 非法
    label: str
    ordinal: int                  # 在 discovery 顺序中的 0-based 位置
    document_id: str              # 所属 Frame/Page document id
    member_id: str                # 关联到对应 RegionMember
    stable_attributes: dict[str, str]
    selected: bool
    explicit_frame_count: int | None
    inferred_frame_count: int | None
    activation: str | None        # "click" | "dblclick" | None
```

- descriptor 数量与 region members 可审计对应（`member_id` 一一对应）；
- descriptor 只存稳定描述，不存 Locator / 元素句柄 / 坐标。

### 4.2 `SeriesCollectionEvidence`

```python
@dataclass
class SeriesCollectionEvidence:
    collection_mode: str          # visible / scroll_harvest
    virtualized: bool
    visible_count: int
    collected_count: int
    harvest_steps: int
    reached_end: bool             # 见 §5「全部」定义
    warning: str | None           # series_virtualized_partial 等
    discovered_count: int = 0
```

### 4.3 `SeriesBranch`

```python
@dataclass
class SeriesBranch:
    branch_id: str
    series_key: str
    label: str
    ordinal: int
    document_id: str
    source_member_id: str         # 关联到 SeriesDescriptor.member_id
    selector: LocatorRecipe | None
    activation: str               # "click" | "dblclick"
    viewer_state_id: str | None
    metadata_state_id: str | None
    return_state_id: str | None   # close 后显式返回的 viewer state；存在 metadata_state_id 时必须 == viewer_state_id
    capture_status: str           # captured|partial|failed|skipped_budget|skipped_duplicate
    warning: str | None
```

### 4.4 `SeriesExpansionEvidence`

```python
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

守恒律：`captured + partial + failed == discovered`（validation 强制，见 §7.2）。

### 4.5 `ReplicaFlow` 挂载

`ReplicaFlow` 增加两字段并升 `schema_version=2`：

```python
@dataclass
class ReplicaFlow:
    # ... 既有字段不变 ...
    series_branches: list[SeriesBranch] = field(default_factory=list)
    series_expansion: SeriesExpansionEvidence | None = None
```

- `from_dict()` 接受 `{1, 2}`，其它版本拒绝；v1 无 series 字段时解码为「空列表 / None」默认值，**不回写伪造数据**；
- 所有 state_id / branch_id 在 manifest 内唯一且稳定。

### 4.6 区域坐标与滚动容器

- `Rect.coordinate_space` 只用三值：`page_viewport_css` / `frame_viewport_css` / `region_content_css`；
- 序列成员相对容器内容坐标，不得为视口外/离屏虚拟成员生成负坐标透明 hitbox——离屏成员由本地正常可滚动布局承载；
- `capture_snapshot.discover_series_candidates` 滚动后 `finally` 恢复原 `scrollTop`（多条退出路径都要恢复）。

---

## 5. 「全部」的精确定义（MVP 完整性裁定）

**complete**（整次发现可报告）当且仅当**同时**满足：

1. discovery 的 `SeriesCollectionEvidence.reached_end == true`（列表确认到底），**并且**
2. 每个 descriptor 都有**终态**（captured/partial/failed 三者之一，绝不静默从 discovered 列表删除）。

只要 `reached_end=false`（未确认到底）或任一 descriptor 无终态，报告必须为 partial/failed，不得谎报 complete。`reached_end=false` 时 flow 必须携带 warning `series_virtualized_partial` 或等价项。

单条 branch 的「终态」判定见 §7.1。

---

## 6. 探索时序与就绪条件（Phase 5 组合证据）

### 6.1 单序列事务（`capture_one_series` 或等价 API，Phase 5.3 落地）

每个 descriptor 独立 try/finally，按以下顺序在**同一已登录 session** 内串行：

1. **重新解析**：每次调用前重新解析 Page/Frame 与序列 locator（不缓存跨越滚动的 Locator）；
2. **继承激活**：click 或 dblclick（来自 descriptor.activation / RecordingTemplate.series_action）；
3. **等待选中态**：`aria-selected=true` 或 active/current class 改变；
4. **等待 Viewer 变化/稳定**（组合证据，见 §6.2）；
5. **捕获 Viewer**：topology、稳定截图、相关区域 DOM；
6. **打开 Metadata**：执行 open action；
7. **等待 signature 稳定**：`capture_readiness.wait_for_metadata_panel_state`；
8. **捕获 Metadata**：完整 panel outerHTML + 解析行；
9. **关闭 Metadata**：执行 close action，断言面板不可见；
10. **finally 恢复**：恢复 `scrollTop` 与可操作的 series hub。

### 6.2 组合就绪条件（序列切换成功至少满足两条，禁固定 sleep 作为唯一依据）

下列证据中至少取两条判定「已切到目标序列」：

- 目标 `aria-selected=true` 或 active/current class 改变；
- 当前序列名称或帧计数匹配目标；
- canvas hash 相对前一序列变化（复用 `skills._shared.canvas_capture` 的 hash 轮询模式，不把其私有函数当长期公共 API；跨模块复用须与 Metadata readiness 一样提升为共享 public helper）；
- Viewer DOM fingerprint 稳定；
- Viewer 截图通过非空/非全黑检查。

细则：

- 第一次动作无任何变化证据时**最多重试一次**；
- 若 viewer 本来已是目标序列，允许 selected-state + 稳定证据直接成功，**不强制** hash 变化；
- 序列切换成功**不等价**于 `aria-selected` 变化——必须断言目标 state、独有 DOM 与 asset 都到位（见 main 计划 Phase 7 反模式守卫）。

### 6.3 Metadata 事务与 partial 语义

- 打开面板后调用 public `wait_for_metadata_panel_state`，不跨模块直接 import 旧 private 名，不另写第二套等待器；
- signature 连续稳定（`stable_s`）方算内容就绪；`None` signature 复位稳定窗口；
- **partial 判定**：若 Metadata 面板没有变化但 Viewer 已成功，branch 标 `partial`（取 warning 说明），**不是整条 failed**；若 Viewer 无可用终态，则该 branch 为 `failed`。

### 6.4 全序列探索器（`LiveCaptureSession.finalize_series_branches`）

在总预算内按 ordinal 串行遍历 descriptor：

- 保存最初 selected series、`scrollTop`、panel open/closed 状态；
- 每个 descriptor 独立 try/finally，单条失败写 status 并继续；
- **连续三条**因 hub 无法恢复失败时，允许**一次**受控 reload/bootstrap 恢复；第二次仍失败则停止，overall 记 partial/failed，必须留审计 warning；
- 最后恢复原始序列、`scrollTop` 与 panel 状态；
- 写 `series_capture_manifest.json`，满足 §4.4 计数守恒。

### 6.5 反模式守卫（探索期）

- 不在同一 Page 上并行点击多个序列；
- 不缓存虚拟列表首次枚举得到的 Locator / ElementHandle / 坐标；
- 不跨患者/检查导航；
- 不把失败项静默从 discovered 列表删除；
- 探索必须发生在 instrumented replay 子进程内，浏览器关闭后的 manifest-build 阶段不访问网页。

---

## 7. 完成 / 部分 / 失败定义（冻结裁决）

### 7.1 单 branch「终态」

| capture_status | 判定条件 |
|---|---|
| `captured` | 有可用 viewer_state_id；若模板要求 Metadata，还需有可用 metadata_state_id + 显式 return_state_id |
| `partial` | Viewer 成功但 Metadata 无变化；或返回/关闭恢复不完整；或列表未确认到底（配合 flow warning） |
| `failed` | 无任何可用 Viewer state；可无 state 但必须有 warning/reason |
| `skipped_budget` | 因总预算耗尽而未尝试 |
| `skipped_duplicate` | 因与已采集序列身份重复而跳过 |

captured branch 必须有 `viewer_state_id`；failed branch 可以没有 state 但必须有 warning/reason（validation 强制）。

### 7.2 整次探索「覆盖率」裁决（复用 main 计划 Phase 8.3）

- **complete**：`reached_end=true` 且无 partial/failed/skipped_budget（§5 的「全部」成立）；
- **partial**：列表未确认到底、某 branch 为 partial/failed、预算耗尽、或恢复失败；
- **failed**：没有任何可用 Viewer branch，或探索基础设施失败。

报告必须列出安全 branch id 与失败 stage，**不嵌**患者 Metadata / 截图 / UID。不因部分失败删除正常录制主路径——Replica 仍可构建，但总状态必须诚实降级。

---

## 8. 真实站未知项：既定答案与 fallback（Phase 0.2 待实测校准）

下表回答 Phase 0 的未知项。灰色项为**已冻结的契约决策**（不依赖单站实测即可定稿）；带「Spike 校准」的项以 Phase 0.2 结果为准，Spike 无法确认时执行既定 fallback。

| 未知项 | 既定决策（契约） |
|---|---|
| **click vs dblclick** | 两者并存。`activation` 继承人工录制具体动作，探索器不得自行改换；无法从录制确定性判断时按录制原样执行（`RecordingTemplate.series_action`） |
| **iframe 是否重建** | 保守假设会重建。**每轮探索必须重新发现 Page/Frame** 与序列 locator（§6.1 第 1 步）；不缓存跨轮 Frame/文档引用 |
| **v1→v2 schema 兼容** | 由 v2 reader 完成向后兼容：`from_dict` 接受 `{1,2}`；v1 解码为默认空值；未知版本抛错（§4.5）。MVP 不引入额外显式 migration 函数 |
| **partial 语义** | Metadata 没变化但 Viewer 成功 → `partial`，非 failed；列表未枚举到底 → flow 级 `partial` + `series_virtualized_partial` warning（§6.3 / §7） |
| **reload/bootstrap 恢复** | 只允许**一次**受控恢复，且必须留审计 warning；第二连失败即停（§6.4） |
| **Metadata identity 校验** | 优先 SeriesInstanceUID（用 SHA-256 前缀做内部关联，原 UID 不进文件名）；无 UID 或面板为 image-level 时降级为 SeriesNumber/SeriesDescription 一致即接受，并记 warning |
| **克隆了 UID 的文件名** | 公开文件名/日志/报告一律不含患者姓名、检查号、token 或原始 SeriesInstanceUID |
| **虚拟列表完整枚举** | Shadow DOM / canvas-only 序列栏无法读 DOM 时，只能作后续受限 VL fallback 并必须标 partial；不作为 MVP 无条件枚举目标 |

---

## 9. 架构概览（数据流）

```text
processed_script_{hospital}.py   （唯一示教来源）
        │
        v
rewrite_script.parse_action_plan() → ActionPlan
        │
        v
classify_recording_template(): RecordingTemplate (series_action / metadata_open / metadata_close)
        │
        v
instrument_marked_actions(): 插桩，注入 capture_hook_before/capture_hook_after/
        │                    capture_hook_expand_series（仅 close 成功 else: 分支，expand 开启时）
        v
隔离子进程重放（batch_capture_replicate.run_live_capture / LiveCaptureSession）
   ├─ capture_snapshot.discover_series_candidates()
   │    → (SeriesDescriptor[], RegionMember[], SeriesCollectionEvidence)
   ├─ capture_hook_expand_series() → session.expand_series()           [Phase 4 触发]
   │    → finalize_series_branches()：按 ordinal 串行采集每 descriptor   [Phase 5]
   │        └─ capture_one_series(): 组合就绪条件 + Metadata 事务（capture_readiness）
   │              → capture/series_branches/{safe_series_key}/ ...
   │                 {descriptor.json, viewer/, metadata/, metadata_rows.json, status.json}
   │        → series_capture_manifest.json
   └─ 普通录制状态 → build_flow_from_snapshots()

ReplicaFlow（schema v2：states + series_branches[] + series_expansion）
        │
        v
build_replica.build_replica()
   ├─ _render_document()：series region member 标 data-replica-series-key（Phase 7）
   └─ 注入 O(N) route map：series_key -> (viewer_state_id, metadata_state_id, return_state_id)

离线 Replica runtime（RUNTIME）
   ├─ 点击已录制、有 data-replica-action transition 的 target：正常导航（既有）
   └─ 点击 [data-replica-series-key]：查 route map → 进入目标 viewer/metadata state；
      failed branch → aria-disabled=true，不假跳转
```

关键路由原则：

- 全局使用 `series_key -> viewer state（及 metadata/return state）` 的 **O(N) route map**，不当成 N 条复制 action/transition；
- 每个含 series region 的 Viewer document 注入同一份安全 JSON route map；
- route map 只**补齐**未录制、没有 `data-replica-action` transition 的普通 series member；已录制 target 保留既有导航；
- 关闭 Metadata 使用 branch `return_state_id` 生成 close URL，**不**用「前一个 ordinal state」推断（Phase 7.4 显式迁移；录制主路径中非 branch Metadata 保持现有兼容行为直到显式迁移）。

---

## 10. 参考代码对齐基准（Phase 10 一致性审查锚点）

以下函数/类名是设计契约与实现的共同基准，以**函数/类名**为主（不以行号为准）。审查时核对实现是否仍由这些权威入口承担职责。

| 职责 | 权威函数/类 |
|---|---|
| 序列 scroll-harvest 唯一算法 | `capture_snapshot.discover_series_candidates(root_locator, document_id, max_scroll_steps, max_duration_s)` |
| series region 打包 | `capture_snapshot.capture_series_interaction_region(root_locator, document_id, max_scroll_steps)` |
| Metadata 面板捕获 | `capture_snapshot.capture_marker_panel_region(scope, candidates, document_id)` |
| Metadata 就绪（public helper） | `capture_readiness.metadata_panel_signature(locator_factory)`、`capture_readiness.wait_for_metadata_panel_state(page, locator_factory, timeout_s, stable_s)` |
| 普通交互区域捕获 | `capture_snapshot.capture_interaction_region`、`capture_marker_interaction_region` |
| target/locator 快照 | `capture_snapshot.capture_locator_snapshot(locator, coordinate_space=...)`、`capture_selector_closure` |
| 拓扑/iframe | `capture_snapshot.capture_page_topology(named_pages, output_root)` |
| canvas hash 等待（复用模式） | `skills/_shared/canvas_capture._canvas_hash(scope)`、`_wait_for_frame_change(scope, previous_hash)` |
| 模板分类 | `batch_capture_replicate.classify_recording_template(plan)`、`RecordingTemplate.complete` |
| 插桩 | `batch_capture_replicate.instrument_marked_actions(source, ..., expansion_config=None)` |
| 运行时 hooks | `capture_hook_before`、`capture_hook_after`、`capture_hook_expand_series`、`capture_hook_failed` |
| live session | `batch_capture_replicate.LiveCaptureSession`（`before`/`after`/`expand_series`） |
| flow 构建 | `batch_capture_replicate.build_flow_from_snapshots`、`_load_snapshot_state`、`_load_target_snapshot`、`_load_selector_closure` |
| 模型 | `replica_models.SeriesDescriptor` / `SeriesCollectionEvidence` / `SeriesBranch` / `SeriesExpansionEvidence` / `ReplicaFlow` / `ReplicaState` / `ReplicaTransition` / `RegionMember` / `InteractionRegion` |
| replica 构建 | `build_replica.build_replica(flow, source_root, output_root)`、`_render_document`、模块内 `RUNTIME` |

> 复用结论：Metadata readiness 已在 `capture_readiness.py` 独立为无循环依赖的 public helper，`batch_capture_replicate` 改为导入（`batch_capture_replicate` 第 24 行导入两函数）；canvas hash 轮询仍为 `skills/_shared/canvas_capture` 的私有函数，仅**复制模式**使用，不跨模块 import 其私有名（除非按 §6.2 提升为 public helper）。

---

## 11. 隐私与资产预算（契约重申）

- `safe_series_key` / `safe branch id` 使用内部 hash/slug，不含原始 UID、患者姓名、检查号；
- 日志、事件、公开报告不输出患者姓名 / 检查号 / UID / Metadata 原文；
- 单序列只保存一个稳定 Viewer screenshot；重复 asset 用现有 SHA-256 去重；
- 达到总预算时停止新 branch，剩余 descriptor 标 `skipped_budget` / partial；
- 反复用 `rg` 守卫：`contentDocument/contentWindow`（禁止）、`time.sleep/wait_for_timeout`（只作短暂兜底）、`SeriesInstanceUID/PatientName/Accession`（事件/报告不得出现）。

---

## 12. 明确不在本设计契约内（沿用主计划「三、不在 MVP 内」）

- 所有序列的逐帧 DICOM 截图与帧级交互；
- 离线运行原站 DICOM/WebGL viewer；
- 自动诊断 / 自动选择「最佳」序列；
- 跨患者 / 跨检查 / 跨页面的无界 crawler；
- Shadow DOM / canvas-only 序列栏的无条件完整枚举（无 DOM 只能受限 VL fallback 并标 partial）。

---

## 13. 修订记录

| 日期 | 变更 |
|---|---|
| 2026-08-14 | Phase 0.1 初稿冻结：术语、录制模板、MVP 输出边界、完成/部分/失败语义、未知项答案，与现有代码 API 对齐（含 `capture_readiness` public helper、`capture_hook_expand_series` 触发时序、`SeriesDescriptor/SeriesBranch/SeriesExpansionEvidence` 模型已落地状态）。 |
