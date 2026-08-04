# 可交互流程复刻 Web Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `subagent-driven-development`（推荐）或 `executing-plans` 按任务执行。所有步骤使用 checkbox 追踪；每个阶段必须先写失败测试，再实现，再运行完整验证。

## 环境要求

本计划所有命令必须在 conda 环境 `codegen-marker` 下执行；**禁止**直接使用系统 Python（Windows 默认 3.7 缺少 PyQt6 / lxml 等预编译 wheel，会导致 `pip install` 失败或运行时报 `ModuleNotFoundError`）。

- 环境名：`codegen-marker`
- 解释器路径：`D:/Anaconda/envs/codegen-marker/python.exe`
- pip 路径：`D:/Anaconda/envs/codegen-marker/Scripts/pip.exe`
- playwright 路径：`D:/Anaconda/envs/codegen-marker/Scripts/playwright.exe`

启动 shell 后先激活环境（推荐）：

```bash
conda activate codegen-marker
```

若不便 `activate`，所有 `python` / `pip` / `playwright` 命令一律使用绝对路径调用。本计划下文（特别是第 10.2 节 GUI 导出、第 12 节「实施任务」）所有 `python ...` 命令实际应展开为：

```bash
D:/Anaconda/envs/codegen-marker/python.exe <原 python 后面的参数>
```

新增依赖（Task 0：`Pillow`；Task 3：`lxml` / `cssselect`）必须装在本环境里，不要落到 system Python：

```bash
D:/Anaconda/envs/codegen-marker/Scripts/pip.exe install lxml>=5.0 cssselect>=1.2 Pillow>=10.0
D:/Anaconda/envs/codegen-marker/Scripts/playwright.exe install chromium
```

> **一致性提醒**：本节是本项目所有 Python 命令的权威约定。第 12 节中所有 `python` / `pip` / `playwright` 命令在执行时都必须按本节展开为绝对路径（或先 `conda activate codegen-marker`）。

---

**Goal:** 用户只需录制并标注一次真实业务流程，即可生成一个离线运行的 Web 复刻版本；它以截图还原视觉，保留真实 page/popup/iframe 层级，并将被点击的关键 DOM 和被读取的交互区域复刻为 Playwright 可定位、可点击、可填写、可提取的本地元素。

**Architecture:** 录制脚本中的 marker 用来界定业务动作组，Playwright action 用来确定实际目标。导出时在隔离子进程中重放一次完整流程，在关键 action 前后抓取页面状态、文档拓扑、截图、目标 DOM 和区域 DOM，生成 `ReplicaFlow` 状态图。复刻 Web 通过本地 HTTP 服务运行；每个真实 Page/Popup/Frame 对应一个本地 HTML 文档，截图作为视觉底层，关键 DOM 作为交互覆盖层，受控的 `replica_runtime.js` 负责状态跳转，不运行原站脚本。

**Tech Stack:** Python 3.10+、PyQt6、Playwright Sync API、标准库 `ast`/`tokenize`/`dataclasses`/`http.server`、lxml、cssselect、Pillow、HTML/CSS/受控原生 JavaScript、`unittest`。

---

## 0. 产品定义

### 0.1 “完整复刻”的精确定义

“完整”指**录制路径完整**，不指真实网站功能完整。对于用户实际走过的流程，复刻 Web 必须保留：

- 每一个有意义的视觉状态；
- 当前操作发生在主页面还是 popup；
- iframe 的 id、name、父子层级和嵌套顺序；
- 被 click/dblclick/fill/press/select 的目标 DOM；
- adapter 需要读取的报告、序列列表、Metadata 面板、WL/WW 对话框等区域 DOM；
- action 的前后状态和转移方式；
- 原始 locator 所依赖的 id、class、name、role、ARIA、testid、文本和相邻元素数量。

不要求复刻：

- 未录制路径上的任意交互；
- 真实 DICOM 数据加载、序列播放和 WebGL 渲染；
- canvas 逐帧翻页的中间视觉态（MVP 只保留 canvas 静态图与可点击 hitbox，不建模连续滚片的每一帧）；
- 真正的 WL/WW 像素运算；
- 原站 Vue/React 运行时和接口；
- 所有第三方字体、动画和远程资源；
- 任意 viewport 下的响应式还原。MVP 使用录制时 viewport。

### 0.2 第一优先级验收

以一次包含下列步骤的真实录制为目标样本：

```text
查看报告
  -> 点击影像（同页或 popup）
  -> 可选：切换序列布局
  -> 选择诊断序列
  -> 打开/读取 DICOM Metadata
  -> 调整 WL/WW
  -> 点击或定位 canvas
```

导出后，adapter 开发者必须能够在断网环境中：

1. 打开一个本地 HTTP URL；
2. 使用顶层 locator、popup、单层 iframe 和嵌套 iframe；
3. 定位全部被标注的关键元素；
4. 点击关键元素并进入对应下一视觉状态；
5. 对本地 input 执行 `fill()`，对按钮执行 `click()`；
6. 从序列列表、报告区域和 Metadata 区域读取文本；
7. 使用 canvas 的原 id/class/尺寸定位并点击静态画布区域；
8. 不向真实医院站点发起请求。

---

## 1. 核心设计决策

### 1.1 使用混合复刻，不复制完整应用

复刻文档由三层组成：

```text
视觉层：当前 document 的截图
结构层：真实 page/popup/iframe 拓扑 + 关键 DOM/区域 DOM
行为层：受控本地状态机，只执行记录过的状态转移
```

截图覆盖未建模区域；DOM 覆盖层只承担 locator、click、fill、press、文本提取和状态推进。原站 JavaScript 全部禁用。

### 1.2 iframe 必须保留为 iframe

旧方案中的：

```html
<iframe> -> <div><img></div>
```

废弃。新方案生成真正的本地 iframe：

```html
<iframe
  id="iframe"
  name="viewerHost"
  src="documents/doc_frame_001/index.html"
></iframe>
```

嵌套 frame 继续在子文档内生成 iframe。这样 adapter 可以继续测试 `.content_frame`、`frame_locator()`、frame id/name 和 frame 内 locator。

### 1.3 popup 必须按真实行为复刻

- 真实 action 打开 popup：本地目标使用 `window.open()`，Playwright `expect_popup()` 必须可捕获。
- 真实 action 同页导航：本地 runtime 更新顶层 state，不打开 popup。
- popup 内仍可包含本地 iframe 树。

### 1.4 通过 localhost 提供 Web，不以 file:// 为主入口

`file://` 在 iframe、popup、模块脚本和同源访问上存在差异。复刻默认通过标准库 `ThreadingHTTPServer` 绑定 `127.0.0.1` 随机端口：

```text
http://127.0.0.1:<port>/flows/<flow-id>/index.html
```

运行时只允许 localhost；真实 HTTP(S) 回退默认关闭。

### 1.5 marker 标记业务动作组，action 决定真实 DOM

marker 不再只是截图点。marker 的含义是：

> 从当前 marker 到下一个 marker 之间的 Playwright action 属于同一业务动作组，并使用该 marker 类型对应的 DOM 区域捕获策略。

例如：

```python
# [MARKER: 窗宽窗位 WL/WW]
page.locator(...).click()
page.get_by_role("spinbutton").first.fill("2000")
page.get_by_role("spinbutton").nth(1).fill("0")
page.get_by_role("button", name="确定").click()
```

该 marker 产生一个 WL/WW 动作组，包含工具按钮、两个输入框和确认按钮，并在有视觉变化的 action 前后产生状态。

### 1.6 捕获阶段与离线阶段的依赖不同

系统有两个运行环境，计划和测试必须明确区分：

| 阶段 | 网络 | 登录/凭据 | 真实站点在线 | 产物 |
|---|---|---|---|---|
| 真实捕获 | 必需（live 模式） | 可能必需；MFA 可人工完成 | 必需 | snapshot、DOM、状态图 |
| 离线复刻/adapter 开发 | 禁止外部网络 | 不需要 | 不需要 | localhost Web |

真实捕获不是纯离线操作。运行 `batch_capture_replicate.py --mode live` 前必须满足：

- 真实 URL 当前有效；
- 用户有权访问目标医院系统；
- 登录、验证码、MFA 或临时 token 可完成；
- 站点和 viewer 当前可用；
- 用户接受截图和 DOM 可能包含患者信息。

CI 和自动化测试不得依赖医院站点。Task 0 使用自包含 `state_diff_spike` fixture；Task 3 以后使用完整 `replica_flow` fixture server。live 模式只作为人工验收和真实样本校准。

### 1.7 Live 登录模式

`batch_capture_replicate.py --mode live` 必须显式选择登录方式：

| 模式 | 参数 | 行为 |
|---|---|---|
| 脚本登录 | `--auth-mode scripted` | processed script 的 bootstrap 自己完成登录 |
| 人工登录 | `--auth-mode interactive` | headed browser 停在登录阶段，GUI/CLI 收到继续命令后开始 marked capture |
| 临时会话 | `--auth-mode storage-state --storage-state <path>` | context 加载用户提供的 storage state；该文件不复制、不写 manifest |

interactive 模式通过 JSON Lines/stdin 协议：

```json
{"event":"auth_required","message":"请完成登录后继续"}
{"command":"continue_after_auth"}
```

等待期间不抓 screenshot/DOM。登录完成后 runner 校验首个 marked action 的 Page/Frame/target 可解析，再进入捕获；超时或用户取消返回 `authentication_cancelled`。

---

## 2. 总体数据流

```text
录制阶段
  Playwright codegen + marker
        |
        v
processed_script_<hospital>.py
        |
        v
解析阶段
  marker 分组 + AST action 解析 + popup/frame locator 解析
        |
        v
ActionPlan
        |
        v
真实重放捕获阶段（隔离子进程）
  before_action: screenshot + topology + target DOM + region DOM
  execute action
  after_action: 判断视觉/拓扑变化，产生下一 ReplicaState
        |
        v
ReplicaFlow manifest + raw snapshots
        |
        v
构建阶段
  每个 state/document 生成 HTML + screenshot + overlay DOM
  生成 replica_runtime.js + 本地 server/replay 脚本
        |
        v
离线 adapter 开发
  localhost 页面 + popup + iframe + locators + 状态转移
```

---

## 3. 文件与模块边界

使用六个根目录新模块，避免共享数据协议散落在捕获、构建和运行时文件中，也避免把复刻逻辑放进现有 codegen 管理器：

| 文件 | 单一职责 |
|---|---|
| `replica_models.py` | 所有跨模块 dataclass、schema version、JSON 序列化、路径和类型校验 |
| `capture_snapshot.py` | 状态差分、document 拓扑捕获、截图、目标 DOM/区域 DOM 提取、HTML 消毒 |
| `build_replica.py` | 将 `ReplicaFlow` 构建为本地 Web 文件树，并生成 CSS/JS runtime |
| `rewrite_script.py` | 解析 marker/action/frame/popup，生成 ActionPlan；生成指向本地 Web 的 replay 脚本 |
| `replay_helpers.py` | manifest JSON、URL 脱敏、本地 HTTP server、Python 侧运行辅助 |
| `batch_capture_replicate.py` | CLI/子进程入口；插桩、真实重放、捕获、构建、进度 JSON Lines |

现有文件修改：

| 文件 | 修改范围 |
|---|---|
| `main_gui.py` | 保存 replica anchor、导出按钮、QProcess 进度；不做捕获和构建 |
| `requirements_codegen_marker.txt` | Task 0 新增 `Pillow>=10.0`；Task 3 新增 `lxml>=5.0`、`cssselect>=1.2` |
| `.gitignore` | 若项目初始化 Git，忽略含患者信息的 snapshot/replica 产物 |

明确不修改：`codegen_manager.py`、`markers.py` 的既有 marker 文本、`agent.py`、`auto_gen.py`、`skills/` 和真实网站 `completed_*.py` 生成流程。

---

## 4. 数据模型

所有 JSON 路径均相对 flow 根目录；禁止将 `Path` 直接写入 JSON。所有 schema 包含 `schema_version`。

```python
from dataclasses import dataclass, field

@dataclass
class Rect:
    x: float
    y: float
    width: float
    height: float
    coordinate_space: str           # page_viewport_css / frame_viewport_css / region_content_css

@dataclass
class Point:
    x: float
    y: float
    coordinate_space: str           # page_viewport_css / frame_viewport_css

@dataclass
class FrameHop:
    selector: str
    frame_id: str | None
    frame_name: str | None

@dataclass
class LocatorRecipe:
    source_expression: str
    page_var: str                    # page / page1 / page2
    frame_chain: list[FrameHop]
    locator_kind: str                # css / role / text / test_id / label / keyboard / mouse_xy / none / unknown
    locator_args: dict[str, object]
    ordinal_op: str | None           # first / last / nth
    ordinal_value: int | None

@dataclass
class SelectorClosure:
    action_id: str
    root_outer_html: str
    required_ancestor_count: int
    required_sibling_count: int
    accessible_name_sources: list[str]

@dataclass
class BootstrapPlan:
    source_start_line: int
    source_end_line: int
    skipped_in_offline_replay: bool
    entry_page_bindings: dict[str, str]

@dataclass
class PopupExpectation:
    context_line: int
    source_page_var: str
    info_var: str                    # page1_info
    result_page_var: str             # page1
    body_action_ids: list[str]

@dataclass
class CaptureTimingProfile:
    locator_wait_ms: int = 5000
    scroll_into_view_ms: int = 3000
    visual_stability_ms: int = 3000
    dom_retry_count: int = 3
    dom_retry_interval_ms: int = 150
    action_budget_ms: int = 12000
    marker_budget_ms: int = 60000
    flow_budget_ms: int = 900000
    virtual_scroll_max_steps: int = 40
    virtual_scroll_budget_ms: int = 10000

@dataclass
class DomNodeSnapshot:
    tag_name: str
    text: str
    attributes: dict[str, str]
    rect: Rect
    outer_html: str
    computed_style: dict[str, str]

@dataclass
class ActionTarget:
    action_id: str
    marker_id: str
    action_type: str                 # click/dblclick/fill/press/select_option/hover/keyboard/mouse
    action_source_kind: str          # locator / keyboard / mouse_xy
    action_args: dict[str, object]
    locator: LocatorRecipe | None
    dom: DomNodeSnapshot | None
    selector_closure: SelectorClosure | None
    point: Point | None
    key: str | None
    replay_policy: str               # execute / explicit_skip
    skip_reason: str | None
    document_id: str
    transition_id: str | None

@dataclass
class RegionMember:
    member_id: str
    semantic_type: str
    dom: DomNodeSnapshot

@dataclass
class SeriesCollectionEvidence:
    collection_mode: str             # visible / scroll_harvest
    virtualized: bool
    visible_count: int
    collected_count: int
    harvest_steps: int
    reached_end: bool
    warning: str | None

@dataclass
class InteractionRegion:
    region_id: str
    region_type: str                 # report/series/layout/meta/wlww/canvas/generic
    document_id: str
    root: DomNodeSnapshot
    members: list[RegionMember]
    series_collection: SeriesCollectionEvidence | None

@dataclass
class ReplicaDocument:
    document_id: str
    page_id: str
    page_var: str
    page_kind: str                   # main / popup
    parent_document_id: str | None
    frame_selector: str | None
    frame_id: str | None
    frame_name: str | None
    viewport: dict[str, int]
    device_scale_factor: float
    screenshot_scale: str           # 固定为 css
    scroll_x: float
    scroll_y: float
    screenshot_asset_relpath: str   # 必须指向 assets/by-hash/<sha256>.jpeg
    screenshot_sha256: str
    screenshot_size_bytes: int
    targets: list[ActionTarget] = field(default_factory=list)
    regions: list[InteractionRegion] = field(default_factory=list)

@dataclass
class ReplicaPage:
    page_id: str
    page_var: str
    page_kind: str                   # main / popup
    opener_page_id: str | None
    window_name: str | None
    entry_document_id: str
    is_active: bool
    is_closed: bool

@dataclass
class ReplicaTransition:
    transition_id: str
    action_id: str
    from_state_id: str
    to_state_id: str | None
    source_page_var: str
    target_page_var: str
    mode: str                        # same_page / popup / state_update / none

@dataclass
class StateEvidence:
    topology_changed: bool
    url_changed: bool
    popup_changed: bool
    region_dom_changed: bool
    regional_changed_pixel_ratio: float
    regional_mean_abs_diff: float
    global_changed_pixel_ratio: float
    dynamic_mask_count: int
    decision_reason: str

@dataclass
class ReplicaState:
    state_id: str
    ordinal: int
    source_url: str
    active_page_var: str
    pages: list[ReplicaPage]
    documents: list[ReplicaDocument]
    transitions: list[ReplicaTransition]
    evidence: StateEvidence

@dataclass
class ReplicaFlow:
    schema_version: int
    flow_id: str
    source_script_relpath: str
    source_script_sha256: str
    created_at: str
    viewport: dict[str, int]
    bootstrap: BootstrapPlan
    popup_expectations: list[PopupExpectation]
    timing_profile: CaptureTimingProfile
    entry_state_id: str
    states: list[ReplicaState]
    warnings: list[str]
```

### 4.1 稳定身份规则与现有代码变更

- 当前 `main_gui.py` 的 `_marker_anchors` 只有 `codegen_idx`、`fingerprint`、`items`，**不存在 `marker_id`**。Task 1 必须新增该字段；本文后续提到的 `marker_id` 都是目标设计，不是既有能力。
- 扩展后的 anchor 为：`marker_id`、`codegen_idx`、`fingerprint`、`items`。`relocate_markers()` 的“按偏移 + 指纹重定位”语义保持不变，但必须透传新增字段。
- 一个 marker 模板可能包含多行 `marker_items`。`marker_id` 绑定整个 anchor，同时写入每个 marker item，保证任一行重定位后仍能追溯同一 marker。
- 删除多行 marker 中任意一行时，删除该 anchor 的全部 marker items，避免剩余附属说明行成为无主文本。
- `marker_id`：GUI 插入 marker 时新生成 UUID，保存后不变。
- `action_id`：`a_<marker ordinal>_<action ordinal>`。
- `state_id`：`s_<ordinal:03d>`，仅表示录制路径顺序。
- `document_id`：根据 page kind + frame chain hash 生成，不能依赖 `page.frames` 临时顺序。
- `transition_id`：`t_<action_id>`。

### 4.2 未标记 action 的归属

- 第一个 marker 之前的 `goto`、登录、验证码、导航等 action 属于 `bootstrap`：真实重放时照常执行，但不插入捕获 hook、不保存 screenshot/DOM、不创建 state。
- bootstrap 创建的 popup/frame 在第一个合法 marked action 前会作为 entry state 拓扑被捕获。
- 两个 marker 之间的 action 归属前一个 marker 组。
- 最后一个 marker 到 codegen 清理段之间的业务 action 归属最后一个 marker；`context.close()`、`browser.close()`、`page.close()` 等 teardown 不属于任何动作组。
- 位于 `def run()` 外或 Page 创建前的 marker 无有效浏览器作用域，标记为 invalid，不捕获登录页或用后续页面代替。
- 默认禁止捕获登录页、密码框、验证码和认证 token。只有显式 `--allow-sensitive-bootstrap-capture` 才能改变此规则；MVP GUI 不暴露该开关。

### 4.3 Capture bootstrap 与 Offline bootstrap

bootstrap 在真实捕获和离线 replay 中使用不同策略，不能共用同一段源码行为。

**Capture bootstrap：**

```text
goto 真实 URL
  -> 执行登录、验证码、确认和前置导航
  -> 到达第一个合法 marked action
  -> 从此处开始捕获 ReplicaFlow.entry_state
```

**Offline bootstrap：**

```text
创建 browser/context
  -> 跳过 source_start_line..source_end_line 的真实 bootstrap action
  -> 打开本地 entry state
  -> 恢复 page/page1/page2 变量绑定
  -> 从第一个 marked action 开始运行
```

`rewrite_script.py` 必须整块移除第一个真实 `page.goto()` 之后、首个 marked action 之前的 bootstrap action；不得把本地 flow URL 打开后再执行登录 input。browser/context/page 创建代码保留。

当 entry state 只有 main Page：

```python
pages = runtime.open_entry_pages(context, existing_page=page)
page = pages["page"]
```

当 bootstrap 已创建 popup，首个 marked action 使用 `page1`：

```python
pages = runtime.open_entry_pages(context, existing_page=page)
page = pages["page"]
page1 = pages["page1"]
```

`BootstrapPlan.entry_page_bindings` 保存 source page variable 到 `ReplicaPage.page_id` 的映射。生成脚本必须通过静态检查，确保首个 marked action 使用的所有 page variable 已绑定。

---

## 5. Marker 与区域捕获策略

### 5.1 通用规则

每个 action 前必须捕获目标 DOM。只有目标节点不足以支持 adapter 时，按 marker 类型捕获最近的稳定区域根节点和必要成员。

| Marker | Action Targets | Interaction Region | 必须保存的成员 |
|---|---|---|---|
| 报告截图 | 报告入口或报告容器 | `report` | 报告正文、标题、关键字段容器 |
| 序列布局切换 | 布局按钮、布局选项 | `layout` | 所有可见布局项及文本/ARIA |
| 序列选择 | 被选择的序列项 | `series` | 可见序列列表、每个序列文本、选中态、层数/层厚文本 |
| 窗宽窗位 WL/WW | 工具按钮、两个 input、确认按钮 | `wlww` | 标签、所有 spinbutton、当前 value、确认/取消 |
| 影像画布交互 | canvas 或 overlay canvas | `canvas` | canvas id/class/width/height、可点击矩形、静态 viewer 图 |
| Meta 信息工具 | DICOM 按钮、关闭按钮 | `meta` | 面板标题、所有 tag/value 行、滚动容器 |

### 5.2 序列区域

序列列表不能只保存被选中的一个 item。必须在目标所属 Frame 上使用 `frame.evaluate()`，先尝试 viewer adapter selector，再使用已有序列识别规则回退。保存：

- 全部可见候选的文本和 DOM；
- `aria-selected`、selected/active/current class；
- bounding rect；
- `_parse_slice_count()` 结果；
- `_infer_frames_from_thickness()` 结果；
- 原始 DOM 顺序。

捕获时必须检测序列容器是否可能虚拟化：

```text
scrollHeight > clientHeight * 1.5
且滚动后可见 DOM 节点被复用/文本集合变化
```

疑似虚拟化时，MVP 执行有界 scroll harvest：

1. 保存原 `scrollTop`；
2. 按 `clientHeight * 0.8` 逐步滚动；
3. 每步等待最多 `timing.dom_retry_interval_ms` 后收集当前渲染 item；
4. 按稳定 series id；没有 id 时按规范化文本 + 关键属性去重；
5. 到达底部、连续两步无新增、40 步或 10 秒任一条件即停止；
6. 恢复原 `scrollTop`，重新将真实 action target 滚入视口后再执行 action。

`SeriesCollectionEvidence` 必须记录 visible_count、collected_count、harvest_steps、reached_end 和 collection_mode。无法确认到达末尾时写：

```text
series_virtualized_partial
```

并将 region 标记为 partial。adapter 可以读取已采集候选，但不得把它们宣称为完整序列列表。

本地生成：

```html
<div class="replica-series-list" role="listbox">
  <button
    class="series-item"
    role="option"
    aria-selected="false"
    data-replay-series-id="series_001"
  >Body 1.0 CE</button>
</div>
```

### 5.3 Metadata 区域

Metadata 的内容不是 click target，但属于 adapter 提取目标。DICOM 按钮点击后，在 after-action 状态捕获面板根节点；按真实 DOM 保存 table rows、flex rows 或 tree nodes。每行本地生成可读文本和结构，不仅嵌在 screenshot 中。

### 5.4 WL/WW 区域

必须生成真实 `<input>` 元素并保留：`type`、`role=spinbutton`、name、placeholder、value、顺序和标签关系。WL/WW 时序固定如下，不允许实现时自行选择 region 挂载位置：

1. 工具按钮 click 的 before-hook：在 `s_wlww_closed` 保存工具按钮 target，不要求 dialog region 存在。
2. 工具按钮 click 执行完成后：等待对话框稳定；创建 `s_wlww_open`，从 after-hook 捕获完整 `wlww` region。
3. 两个 spinbutton 的 before-hook：从 `s_wlww_open` 的 region 中解析并保存 input targets；它们都挂在 `s_wlww_open`。
4. `fill()`：本地 replica 只修改 input value，不创建新视觉 state；真实捕获仍记录 action 参数和执行后的实际 value。
5. 确认按钮 before-hook：确认 target 挂在 `s_wlww_open`。
6. 确认 click after-hook：等待 viewer/canvas 区域稳定，创建 `s_wlww_applied`。

如果工具按钮点击后未找到 dialog root，该 marker 组为 partial，不能生成空 input 或把 input 误挂在 `s_wlww_closed`。

### 5.5 Canvas 区域

不复刻渲染算法，但生成匹配原 selector 的透明 `<canvas>`，设置原 width/height、id/class 和绝对位置。视觉内容由位于 canvas 下方的 `<img>` 提供：

```html
<img class="canvas-visual" src="/flows/<flow-id>/assets/by-hash/<sha256>.jpeg" alt="">
<canvas id="overlaycanvas-0_0" class="replica-canvas-hitbox"></canvas>
```

build 期不依赖 JavaScript `drawImage()`。adapter 可以 locator/click/wheel/keyboard focus，但画面不动态滚片。

---

## 6. Action 解析与插桩

### 6.1 支持的 Playwright action

MVP 支持三类 action：

```text
Locator actions：click, dblclick, fill, press, select_option, check, uncheck, hover
Keyboard actions：page.keyboard.press(...)
Mouse actions：page.mouse.move/dblclick/click/wheel(...)
```

Locator action 有 LocatorRecipe/DOM/Selector Closure。keyboard/mouse_xy 没有 receiver locator，`ActionTarget.locator/dom/selector_closure` 允许为 None：

- `page.keyboard.press(key)`：保存 page_var、当前 document、key 和 before-hook 时的 activeElement；本地有有效焦点时执行，否则写 `keyboard_no_focus` 并按 replay_policy 处理。
- `page.mouse.dblclick(x, y)` / click/move/wheel：保存固定 viewport 下的 Point/delta；本地 viewport 与录制 viewport一致时执行。
- 无法可靠复刻的非 locator action 必须 `replay_policy="explicit_skip"` 并保存 skip_reason，禁止静默丢弃。

当前两份 processed 样本没有顶层 `page.keyboard/page.mouse` action，但 `completed_uicloud.py` 和 `completed_cxhospital.py` 的序列选择、翻帧管线已大量使用 dblclick、keyboard、move 和 wheel。因此这些 action 属于 adapter 验证范围，不能因 processed 样本暂时未出现而从模型中省略。

`with page.expect_popup() as page1_info: ...; page1 = page1_info.value` 不是普通 action Call。解析器必须单独处理 AST `With` + 后续 `Assign`，生成 `PopupExpectation`，把 context body 内 action ids、source page、info var 和 result page var 绑定起来。

识别 receiver locator 链和 action 参数。例如：

```python
page.locator("#iframe").content_frame.locator(
    'iframe[name="imageFrame"]'
).content_frame.get_by_text("Body 1.0 CE").first.click()
```

解析为：

```text
page_var = page
frame_chain = [#iframe, iframe[name="imageFrame"]]
locator_kind = text
locator_args = {text: Body 1.0 CE}
ordinal_op = first
action_type = click
```

`select_option` 的 MVP 范围只包括原生 `<select>`。捕获时必须保存所有 `<option>` 的 value、label、text、selected、disabled 和顺序，并在本地生成真实 `<select>`。自定义下拉框不会被视为 `select_option`；它按 click/hover + generic/layout region 状态流捕获。

### 6.1.1 Selector Closure

保存目标节点的属性不等于原 locator 可执行。每个 ActionPlan 必须分析并捕获让原 locator 成立的最小 DOM 闭包：

- locator 链中使用的祖先容器；
- CSS `>`、空格后代、相邻/兄弟组合符要求的层级；
- `.first/.last/.nth()` 和 `nth-child()` 所需的同级节点数量与顺序；
- `get_by_role(name=...)` 计算 accessible name 所需的 label、aria-label、aria-labelledby 节点；
- 父 locator 链，例如 `get_by_test_id(...).get_by_role(...)` 的父子关系。

构建后必须对每个 marked action 执行原 locator 兼容检查：

```python
locator = resolve_locator_from_recipe(page_bindings, action.locator)
self.assertEqual(locator.count(), 1)
self.assertTrue(locator.is_visible())
```

验收只有两种合法结果：

```text
original_locator_compatible = true
或
locator_mapping.json 中存在该 action 的显式映射和原因
```

不允许“属性存在但原 locator 命不中”的第三种状态。

### 6.1.2 Locator 兼容风险基线与门槛

现有两份 processed 样本的静态基线已经确认 locator 不是以简单 CSS 为主：

| 样本 | marked locator actions | role | text | first | nth | 结构链 |
|---|---:|---:|---:|---:|---:|---|
| uicloud | 14 | 4 | 1 | 0 | 0 | testid→role 1；nth-child 1；expect_popup 1 |
| cxhospital | 13 | 5 | 3 | 7 | 2 | 双层 iframe 13；直接子代 CSS 1 |

Task 2 必须生成 `locator_risk_report.json`，按 action 分类：

```text
simple：单 id/class/testid/text，预计可直接重建
aria_dependent：role/name 依赖 accessible name 子树
ordinal_dependent：first/nth/nth-child 依赖兄弟数量和顺序
structural_chain：父子/后代/iframe/testid 链依赖 DOM 拓扑
non_locator：keyboard/mouse_xy，无 Selector Closure
```

静态报告只用于预估，不等于真实兼容。Task 6 构建后计算实际指标：

```text
critical_action_original_locator_hit_rate = 100%
all_marked_locator_action_direct_hit_rate >= 90%
locator_mapping_rate <= 10%
```

critical action 指每个业务 marker 中决定流程前进的打开、选择、确认、Metadata、canvas 主 action。若达不到门槛，不得靠增加 mapping 把总覆盖率包装成成功；必须回到 Selector Closure/region 建模调整，或明确降低产品承诺后重新审批计划。

### 6.2 插桩形式

用 `tokenize` 保留 marker 注释位置，用 AST 确定完整 Call 节点边界。对 marked action 插入：

```python
__replica_before_action__(
    "a_003_001",
    page.locator(...).content_frame.get_by_text("Body 1.0 CE").first,
    locals(),
)
page.locator(...).content_frame.get_by_text("Body 1.0 CE").first.click()
__replica_after_action__("a_003_001", locals())
```

receiver locator 可重复构造，但不得重复执行 click/fill。插桩脚本只在隔离子进程中生成和执行，不覆盖 processed script。

before-hook 不得假设瞬态元素已经存在。捕获顺序固定为：

1. 读取 `CaptureTimingProfile`，禁止在 hook 内散落固定 timeout。
2. 如果 ActionPlan 声明该目标依赖之前的 hover action，先确认 hover prelude 已成功执行且鼠标仍位于触发元素；必要时只重放该 hover，不重放 click/fill。
3. 根据原 action 参数判断目标需 `attached` 还是 `visible`；普通 click/fill 使用 visible，`force=True` 使用 attached。
4. `locator.wait_for(state=..., timeout=timing.locator_wait_ms)`。
5. `locator.scroll_into_view_if_needed(timeout=timing.scroll_into_view_ms)`，使真实 action target 和截图处于同一 viewport。
6. DOM evaluate 按 `timing.dom_retry_count/dom_retry_interval_ms` 重试；仅对 detached/context destroyed 这类瞬态错误重试。
7. 若菜单必须 hover 才出现，但源码和 ActionPlan 均不存在可重放 hover prelude，记录 `missing_hover_prelude`，action 捕获为 partial。
8. hook 失败时记录 `capture_status=partial`，然后仍执行原 action，让 Playwright 自己的 auto-wait 决定真实 action 是否成功。捕获失败不得改变录制脚本原有执行语义。
9. 原 action 失败时记录 action failure，并按 marker 组的安全边界决定终止；不得伪造 after state。

整体预算由 `CaptureTimingProfile` 控制：

```text
单 action：默认 12 秒
单 marker group：默认 60 秒
完整 flow：默认 15 分钟
虚拟列表滚动采集：默认最多 40 步或 10 秒
```

达到上级预算时立即终止下级等待，记录 `action_budget_exceeded`、`marker_budget_exceeded` 或 `flow_budget_exceeded`；不得继续叠加 locator + scroll + stability 的独立最长超时。

### 6.3 状态产生规则

状态切分**不以整页 screenshot hash 作为唯一或首要依据**。决策按下列优先级执行：

1. **结构证据**：URL、Page 数、popup、Frame 树任一变化，必建新 state。
2. **Marker 时序契约**：下表标记为 `always-after` 的 action，成功执行后必建 state，即使视觉差分低于阈值。
3. **区域 DOM 证据**：目标 Interaction Region 的消毒后 DOM fingerprint、可见成员、value、aria-selected、dialog visibility 任一变化，建新 state。
4. **区域视觉证据**：只比较与当前 marker 相关的区域，不用全屏均值掩盖局部关键变化。
5. **全局视觉证据**：只作为无区域信息时的最后回退，且阈值高于区域差分。

Marker 时序契约：

| Action | State policy |
|---|---|
| 点击影像并同页导航/打开 popup | `always-after` |
| 打开布局菜单 | region DOM 或菜单 visibility 变化后建 state |
| 选择布局项 | `always-after` |
| 选择序列 | `always-after` |
| 打开/关闭 Metadata | `always-after` |
| 打开 WL/WW 对话框 | `always-after` |
| fill/check/uncheck | 默认 `none`，只更新值 |
| WL/WW 确认 | `always-after` |
| 普通 canvas focus click | 默认 `none`；若 viewer region DOM/视觉满足阈值则建 state |

### 6.4 视觉稳定与差分算法

所有阈值先由 Task 0 spike 校准；MVP 初始默认值如下，必须写入可序列化 `StateDiffProfile`，不得散落硬编码：

```python
@dataclass
class StateDiffProfile:
    pixel_channel_threshold: int = 12
    regional_changed_ratio: float = 0.02
    regional_mean_abs_diff: float = 3.5
    global_changed_ratio: float = 0.08
    stability_interval_ms: int = 200
    stability_rounds: int = 2
```

视觉稳定总时限使用 `CaptureTimingProfile.visual_stability_ms`；StateDiffProfile 只定义差分阈值和采样节奏，避免同一 timeout 在两个 profile 中重复。

截图统一使用 Playwright `scale="css"`，Pillow 转灰度后做 1px Gaussian blur，再计算：

- `changed_pixel_ratio`：绝对差值大于 `pixel_channel_threshold` 的像素比例；
- `mean_abs_diff`：区域平均绝对灰度差；
- region crop：使用 Interaction Region rect；序列/WLWW action 额外比较 viewer/canvas rect；
- global diff：屏蔽动态区域后比较全 viewport。

区域满足下列任一条件即视为视觉变化：

```text
changed_pixel_ratio >= 0.02
mean_abs_diff >= 3.5
```

全局图只有 `changed_pixel_ratio >= 0.08` 才能单独触发 state。截图差分只负责补充证据，不能否决 `always-after` 或 DOM/topology 证据。

### 6.5 动态区域屏蔽与稳定采样

捕获前后各等待两个连续稳定样本。动态 mask 来源：

- `[aria-busy="true"]`、`.loading`、`.spinner`、`.cursor`、`.clock`、`[data-replay-ignore]`；
- `getComputedStyle(el).animationName != "none"` 的可见元素；
- viewer adapter 明确声明的时间戳/角标 selector。

目标 DOM、当前 Interaction Region 和 viewer canvas 永远不能被通用 mask 覆盖。若在 `CaptureTimingProfile.visual_stability_ms` 内无法稳定，采集 3 张样本并选择与另外两张累计差异最小的 medoid 作为代表图，同时写入 `visual_unstable` warning。

每个 state 必须保存 `StateEvidence`，记录 topology/DOM/区域差分/全局差分、mask 数和最终 `decision_reason`，便于排查多建或漏建 state。

---

## 7. 捕获实现

### 7.1 Document 拓扑

每次捕获同时枚举：

- 当前 main Page；
- 所有已打开 popup Page；
- 每个 Page 下的 Frame 树；
- iframe 元素在父 document 中的 rect、id、name 和 selector。

Frame DOM 访问必须通过 Playwright `Frame.evaluate()`，不得使用顶层 `page.evaluate()` + `contentDocument`。

### 7.2 截图对齐

Playwright `Frame` 没有 `screenshot()` API。每个 document 的截图必须区分 Page 和 Frame，并区分“差分图”和“展示资产图”。

**Page document：**直接调用 Page screenshot。

**Frame document：**从所属顶层 Page 截取 iframe content viewport。对每层 iframe 保存父 document 中的 `getBoundingClientRect()`、`clientLeft/clientTop/clientWidth/clientHeight`，累积祖先 frame 的 content origin：

```text
frame_page_x = parent_content_x + iframe_rect.x + iframe.clientLeft
frame_page_y = parent_content_y + iframe_rect.y + iframe.clientTop
frame_width  = iframe.clientWidth
frame_height = iframe.clientHeight
```

然后在所属 Page 上使用 `clip`：

```python
page.screenshot(
    type="png",
    full_page=False,
    scale="css",
    clip={
        "x": frame_page_x,
        "y": frame_page_y,
        "width": frame_width,
        "height": frame_height,
    },
    animations="disabled",
    caret="hide",
)
```

必须区分两套坐标：

```text
Frame screenshot clip：累积祖先 iframe content offset，坐标属于顶层 Page。
Frame overlay rect：不累积 offset，坐标属于该 Frame 自己的 viewport。
```

对于有 CSS transform 的 iframe，MVP 只支持无旋转、无倾斜的轴对齐缩放；发现 rotation/skew 时将 document 标记为 partial 并记录 `unsupported_frame_transform`，不得假装精确对齐。

### 7.2.1 差分图与展示图

状态差分使用无损 PNG bytes：

```python
diff_png = page.screenshot(
    type="png",
    full_page=False,
    scale="css",
    animations="disabled",
    caret="hide",
    clip=clip_or_none,
)
```

最终 replica 视觉资产使用 JPEG：

```python
asset_jpeg = page.screenshot(
    type="jpeg",
    quality=90,
    full_page=False,
    scale="css",
    animations="disabled",
    caret="hide",
    clip=clip_or_none,
)
```

`StateEvidence` 只能由 PNG/raw bytes 计算；JPEG 只用于 HTML 背景和磁盘空间控制。

`scale="css"` 保证截图一像素对应一个 CSS px，不受 devicePixelRatio 影响；DPR 仍写入 manifest 供诊断。父 document 构建时按 iframe element rect 放置子 `<iframe>`，子 document overlay 则使用自身 viewport rect。

同时记录 `window.scrollX/window.scrollY`。MVP replica 固定复现捕获时 viewport 和滚动位置：背景图表示当前 viewport，overlay 使用 client rect，不将 document scroll 再次加到 rect。

滚动容器区域需要额外保存 `scrollTop/scrollLeft/scrollWidth/scrollHeight`。序列列表等区域的成员保存相对容器内容坐标：

```text
member_content_y = member_rect.y - root_rect.y + root.scrollTop
```

before-hook 先将真实 action target 滚入视口。视口外但属于序列/Metadata 区域的成员可以保留语义 DOM 和内容坐标；没有有效可见 rect 的成员不得生成负坐标 hitbox，而应由本地 scrollable region 的正常布局生成。

### 7.3 Target DOM

通过传入的 Locator 获取：

```javascript
el => ({
  tagName: el.tagName.toLowerCase(),
  text: (el.innerText || el.textContent || '').trim(),
  attributes: Object.fromEntries([...el.attributes].map(a => [a.name, a.value])),
  rect: (() => {
    const r = el.getBoundingClientRect();
    return {x:r.x, y:r.y, width:r.width, height:r.height};
  })(),
  outerHTML: el.outerHTML,
  computedStyle: (() => {
    const s = getComputedStyle(el);
    return {
      display:s.display, position:s.position, color:s.color,
      backgroundColor:s.backgroundColor, fontSize:s.fontSize,
      fontWeight:s.fontWeight, textAlign:s.textAlign,
      border:s.border, borderRadius:s.borderRadius
    };
  })()
})
```

目标不可见、detached 或多匹配无法根据 `.first/.nth` 消歧时，该 action 捕获失败并写入明确错误；不得静默生成空 hitbox。

### 7.4 HTML 消毒

保留 locator 相关属性和文本，删除：

- `script`、原事件属性、远程 script src；
- `<link rel="stylesheet">`、CSS `@import`；
- inline style 和 `<style>` 中的远程 `url(http://...)` / `url(https://...)`；
- `javascript:` 的 `href`、`src`、`formaction`；
- `<base>`、`<object>`、`<embed>` 以及会发起外部加载的未知 active content；
- meta refresh；
- Vue/React 运行时私有属性；
- token、Authorization、带敏感 query 的 href/src；
- 原站表单提交地址。

复刻 HTML 只运行项目生成的 `replica_runtime.js`。

`DomNodeSnapshot.outer_html` 在写入内存模型和磁盘前必须先经过同一套 sanitizer；禁止保存一份未消毒 raw outerHTML 再另外生成 sanitized HTML。密码 input、hidden token、authorization-like attribute 和带敏感 query 的 URL value 必须删除或脱敏。

---

## 8. Web 构建结构

```text
out/{hospital}/replicas/<flow-id>/
  index.html
  manifest.json
  assets/
    replica.css
    replica_runtime.js
    by-hash/<sha256>.jpeg
  states/
    s_001/
      pages/
        page/index.html
        page1/index.html
      documents/
        doc_frame_001/index.html
        doc_frame_002/index.html
    s_002/
      ...
```

顶层 `index.html` 加载 entry state 的 main `ReplicaPage`。每个 top-level Page 有独立入口；popup transition 根据 `ReplicaTransition.target_page_var` 打开对应 `states/<state>/pages/<page-var>/index.html`。Frame document 由所属 Page/Frame HTML 通过本地 iframe 引用。

状态 transition 的作用窗口必须明确：

```text
same_page：导航 source_page_var 对应窗口
popup：由 source_page_var 执行 window.open(target_page_var URL)
state_update：只更新 source_page_var 当前 DOM/ARIA/value
none：不导航
```

每个 document HTML 的基础结构：

```html
<div class="replica-viewport" data-document-id="doc_frame_002">
  <img class="replica-background" src="/flows/<flow-id>/assets/by-hash/<sha256>.jpeg" alt="">
  <div class="replica-overlay-layer">
    <!-- rebuilt targets and regions -->
  </div>
</div>
<script src="/flows/<flow-id>/assets/replica_runtime.js"></script>
```

示例中的 `<flow-id>/<sha256>` 由 builder 写成 manifest 中的实际值，不允许生成 `background.jpeg` 这种绕过去重目录的旁路文件。`ReplicaDocument.screenshot_asset_relpath` 是最终 URL 相对 flow 根目录的引用，而不是 document 私有图片路径。

### 8.1 资产去重与体积限制

所有 JPEG 背景按 SHA256 写入 `assets/by-hash/`，state/document 只引用 hash 资产。完全相同的 main/frame 截图不得重复复制。manifest 保存每个资产的 sha256 和 size。

```text
flow 总资产 >= 50 MB：输出 warning
flow 总资产 >= 200 MB：GUI 要求用户确认后才完成导出
```

### 8.2 Overlay 规则

- target/region 按原 rect 绝对定位；
- normal 模式透明覆盖截图，不改变视觉；
- `?debug=1` 时显示彩色边框、action id、region type；
- 元素必须有非零尺寸并处于 Playwright 可见状态；
- 透明文字保留在 DOM 中，`inner_text()` 和 `get_by_text()` 可用；
- 需要提取内容的 region 使用真实文本节点，不只使用 aria-label。

### 8.3 受控 runtime

`replica_runtime.js` 只提供：

```text
register action
same-page state navigation
popup window.open
input value/checked 状态保留
aria-selected 状态更新
debug overlay
action log
```

任何未记录 action 只写日志，不访问网络、不推测下一状态。

---

## 9. Replay 与 adapter 使用方式

生成：

```text
out/{hospital}/serve_replica_{hospital}.py
out/{hospital}/replay_{hospital}.py
```

`serve_replica` 启动 localhost server 并打印入口 URL。`replay` 启动 server、创建 browser/context，然后按 `BootstrapPlan` 删除真实 bootstrap action，打开本地 entry state，并恢复 source page variables：

```python
pages = runtime.open_entry_pages(context, existing_page=page)
page = pages["page"]
page1 = pages.get("page1")
```

只有首个 marked action 实际引用的 page variables 才生成绑定语句。offline replay 不执行真实登录 input、验证码、确认或登录前导航。进入首个 marked action 后，其余 locator/action 尽量保持原样。

只有下列情况允许明确改写 locator：

- 原站 locator 含随机生成的 nth-child，且捕获 manifest 已记录稳定等价 locator；
- 原 locator 依赖未复刻的非关键祖先；
- action 被用户显式标记为 adapter 映射点。

所有改写必须写入 `locator_mapping.json`：原 locator、复刻 locator、原因和 action id，禁止静默替换。

---

## 10. GUI 集成

### 10.1 保存

`main_gui.py` 在 `_insert_marker()` 中生成 `marker_id` 并写入内部 anchor。在 `_on_save()` 中：

- 保存 processed script；
- 扫描实际 marker 行号；
- 同目录保存 `replica_annotations.json`；
- 保存 source script SHA256。

当前 GUI 没有 `_hospital/_hospital_dir`，输出根目录从用户保存的 processed script 路径推导。

### 10.2 导出

新增“📦 导出可交互复刻”按钮，通过 `QProcess` 启动：

```text
D:/Anaconda/envs/codegen-marker/python.exe batch_capture_replicate.py
  --script <processed.py>
  --annotations <replica_annotations.json>
  --out-root <script-parent>
```

stdout 为 JSON Lines：

```json
{"event":"parse_finished","actions":12,"markers":6}
{"event":"action_capture_started","action_id":"a_003_001"}
{"event":"state_captured","state_id":"s_004","documents":3}
{"event":"build_finished","entry_url":"http://127.0.0.1:<port>/flows/..."}
{"event":"finished","flow_id":"flow_..."}
```

GUI 必须显示当前阶段、action、成功/partial/failed 数量；长任务不得阻塞 Qt 主线程。

---

## 11. 隐私与安全

- 截图、DOM、报告、Metadata 和 URL 都可能包含患者信息，产物视为敏感数据。
- live 捕获要求真实联网、有效登录/凭据和站点在线；这些是运行前置条件，不是 replica 的离线能力。捕获失败必须区分 network、authentication、authorization、site unavailable 和 selector failure。
- bootstrap action 默认只执行不捕获。登录页、密码框、验证码、token 页面和位于有效 Page scope 之前的 marker 不生成 snapshot/DOM。
- 控制台和 GUI 使用 `redact_url()` 隐藏 token、query value、检查号。
- runtime 拦截外部导航；只允许 `127.0.0.1` 和本地静态资源。
- 构建时删除 form action、远程 script、原事件 handler、认证 header 和 storage 数据。
- 默认不复制 cookie/localStorage/sessionStorage；若流程必须登录，真实重放阶段由用户现场登录或显式 storage state 提供，storage state 不写入 replica。
- `snapshots/`、`replicas/`、annotations 和 manifest 不应提交到源码仓库。
- Task 0 的真实 cxhospital spike 图片和 DOM 只允许写入本地 `out/cxhospital/spike_state_diff/`，评审文档只保存匿名化指标，不复制患者文本或截图。

---

## 12. 实施任务

### Task 0：状态差分与真实重放可行性 Spike

**Files:**
- Create: `replica_models.py`（Task 0 先定义 `StateDiffProfile`、`DiffMetrics`；Task 1 再扩展其余模型）
- Create: `capture_snapshot.py`（Task 0 先实现 `compute_image_diff()`、`wait_for_visual_stability()`；Task 3 再扩展 DOM 捕获）
- Modify: `requirements_codegen_marker.txt`
- Create: `test/test_state_diff_spike.py`
- Create: `docs/REPLICA_STATE_DIFF_SPIKE.md`
- Create: `test/fixtures/state_diff_spike/index.html`
- Create: `test/fixtures/state_diff_spike/viewer.html`
- Create: `test/fixtures/state_diff_spike/spinner.css`
- Manual sample: `out/cxhospital/processed_script_cxhospital.py`

- [ ] Task 0 自己创建最小 fixture，不依赖 Task 3/4 尚未创建的 `test/fixtures/replica_flow/`。
- [ ] 在 `requirements_codegen_marker.txt` 新增 `Pillow>=10.0`，用于 PNG 差分和 Gaussian blur。
- [ ] 在本地 fixture server 上实现一次最小重放：bootstrap → viewer → 序列变化；采集 idle 30 次、spinner/时间戳噪声、序列切换、Metadata 打开和 WL/WW 确认的 before/after 图。
- [ ] 实现并用第 6.4 节算法输出每类样本的 regional/global ratio、mean diff、DOM fingerprint 和最终 decision；`compute_image_diff()` 必须接受 CSS-px mask rect 列表并返回 `DiffMetrics`。
- [ ] 差分输入使用 PNG bytes；JPEG 仅作为展示资产，不参与 threshold 校准。
- [ ] 验收 fixture：30 次 idle 和动态角标不产生 state；序列、Metadata、WL/WW 三类关键变化检测率 100%；没有只因 global noise 产生的 state。
- [ ] 在用户具备联网、合法登录和站点可用条件时，手工运行 `processed_script_cxhospital.py` live spike；bootstrap 登录动作不捕获，首个有效 viewer marker 才开始采集。
- [ ] live spike 只在 `out/cxhospital/spike_state_diff/` 保存敏感原始产物；`docs/REPLICA_STATE_DIFF_SPIKE.md` 只记录匿名化指标、最终阈值、动态 mask selector 和已知不稳定区域。
- [ ] 如果 live 登录/token 已失效，记录 `live_spike=blocked_by_environment`，fixture 结果仍可决定是否进入 Task 1；进入真实医院验收前必须补做 live spike。
- [ ] 运行：`D:/Anaconda/envs/codegen-marker/python.exe -m unittest test.test_state_diff_spike -v`，预期全部 PASS。
- [ ] 只有 spike 满足上述 false-positive/critical-detection 标准后，才开始 Task 1–11。

### Task 1：Manifest 与注释模型

**Files:**
- Modify: `replica_models.py`
- Modify: `replay_helpers.py`
- Modify: `main_gui.py`
- Create: `test/test_replica_manifest.py`

- [ ] 写失败测试：`ReplicaFlow` JSON round-trip 后路径仍为相对字符串；marker id 稳定；script hash 不匹配时报错。
- [ ] 运行：`D:/Anaconda/envs/codegen-marker/python.exe -m unittest test.test_replica_manifest -v`，确认因模型不存在失败。
- [ ] 在 `replica_models.py` 实现第 4 节全部 dataclass、`to_dict/from_dict` 和 schema 校验；其他模块禁止重复定义同名模型。
- [ ] 在 `replay_helpers.py` 实现 `sha256_file()`、`redact_url()` 和 manifest 文件读写包装。
- [ ] 扩展 `_marker_anchors` 结构新增 `marker_id`，为同一 marker 的全部 `marker_items` 绑定同一个 id；更新 relocate/delete/save 测试，保持“按 codegen offset + fingerprint 重定位”的语义不变。
- [ ] 删除多行 marker 的任意一行时，整组 marker items 与 anchor 一并删除。
- [ ] 再运行测试，预期全部 PASS。

### Task 2：Marker 分组与 ActionPlan 解析

**Files:**
- Modify: `rewrite_script.py`
- Create: `test/test_replica_action_parser.py`
- Fixture: `out/uicloud/processed_script_uicloud.py`
- Fixture: `out/cxhospital/processed_script_cxhospital.py`

- [ ] 写失败测试：解析 uicloud popup、cxhospital 双层 iframe、`.first/.nth()`、click/fill/locator.press/hover/select_option；每个 action 归属正确 marker；首个 marker 前 bootstrap 行范围和首个 marked action 所需 page variables 正确。
- [ ] 写失败测试：解析 `page.keyboard.press()`、`page.mouse.move/click/dblclick/wheel()` 为 non-locator action，Point/key/replay_policy 明确，不要求 Selector Closure。
- [ ] 写失败测试：将 `with page.expect_popup() as page1_info` 和紧随其后的 `page1 = page1_info.value` 解析为一个 PopupExpectation，绑定 body action ids 与 page1。
- [ ] 对 uicloud/cxhospital processed 脚本生成 `locator_risk_report.json`，输出 simple/aria/ordinal/structural/non-locator 数量和预计直命中风险；静态分类失败则 Task 2 不通过。
- [ ] 运行：`D:/Anaconda/envs/codegen-marker/python.exe -m unittest test.test_replica_action_parser -v`，确认失败。
- [ ] 用 tokenize 找 marker，用 AST 找 action Call 和 receiver chain；输出 `ActionPlan`，不执行源码。
- [ ] 添加 `ast.parse(instrumented_source)` 断言，防止插桩破坏缩进。
- [ ] 再运行测试，预期全部 PASS。

### Task 3：本地 fixture 与单文档捕获

**Files:**
- Create: `test/fixtures/replica_flow/report.html`
- Create: `test/fixtures/replica_flow/viewer.html`
- Create: `test/test_capture_snapshot.py`
- Modify: `capture_snapshot.py`
- Modify: `requirements_codegen_marker.txt`

- [ ] 建立包含报告按钮、文本、input、canvas 的本地 fixture。
- [ ] 写失败测试：截图、target rect、id/class/role/text、computed style、消毒 HTML 均被保存；target 的 ancestor/sibling/accessibility SelectorClosure 可让原 locator 成立。
- [ ] 写失败测试：原生 `<select>` 的全部 option value/label/text/selected/disabled/顺序被捕获并可由 `select_option()` 操作；自定义下拉不冒充原生 select。
- [ ] 新增 `lxml>=5.0`、`cssselect>=1.2`；Pillow 已在 Task 0 添加，不重复声明。
- [ ] 扩展 `capture_snapshot.py` 时保留 Task 0 已落地的 `compute_image_diff()` / `wait_for_visual_stability()`，从 `replica_models.py` 导入 `StateDiffProfile` / `DiffMetrics`，只新增 DOM 捕获与消毒逻辑。
- [ ] 实现单 document 捕获和 DOM 消毒。
- [ ] 运行：`D:/Anaconda/envs/codegen-marker/python.exe -m unittest test.test_capture_snapshot -v`，预期全部 PASS。

### Task 4：Popup 与嵌套 iframe 拓扑

**Files:**
- Create: `test/fixtures/replica_flow/host.html`
- Create: `test/fixtures/replica_flow/frame_outer.html`
- Create: `test/fixtures/replica_flow/frame_inner.html`
- Create: `test/fixtures/replica_flow/popup.html`
- Create: `test/test_replica_topology.py`
- Modify: `capture_snapshot.py`

- [ ] 写失败测试：main → popup、`#iframe` → `iframe[name=imageFrame]` 的 ReplicaPage/document tree、opener、page_id/page_var、rect、id/name 被捕获。
- [ ] 写失败测试：Frame 没有 screenshot API 时，使用顶层 Page `clip` + 累积 content offset 得到与 Frame CSS rect 对齐的 PNG/JPEG；iframe border 不造成 overlay 偏移。
- [ ] 验证 frame HTML 使用 `Frame.evaluate()`，源码中不出现 `contentDocument`。
- [ ] 实现 document id 和父子拓扑捕获。
- [ ] 运行：`D:/Anaconda/envs/codegen-marker/python.exe -m unittest test.test_replica_topology -v`，预期全部 PASS。

### Task 5：Marker 专用 Interaction Region

**Files:**
- Modify: `capture_snapshot.py`
- Create: `test/test_replica_regions.py`

- [ ] 写失败测试覆盖 report、layout、series、meta、wlww、canvas 六种策略。
- [ ] 序列测试必须包含多个 item、选中态、帧数和层厚；Metadata 测试必须包含多行 tag/value；WL/WW 必须含两个 spinbutton。
- [ ] 增加虚拟滚动序列 fixture：滚动时复用 DOM 节点。验证 scroll harvest 去重、恢复原 scrollTop、reached_end，以及超预算时 `series_virtualized_partial` warning。
- [ ] 实现 adapter selector 优先、结构/文本回退的区域提取。
- [ ] 运行：`D:/Anaconda/envs/codegen-marker/python.exe -m unittest test.test_replica_regions -v`，预期全部 PASS。

### Task 6：复刻 Web 构建器

**Files:**
- Modify: `build_replica.py`
- Create: `test/test_build_replica.py`

- [ ] 写失败测试：生成自包含 flow 目录、真实 iframe、popup target、截图背景、overlay DOM、debug 模式。
- [ ] 从 ActionTarget 读取捕获阶段生成的 SelectorClosure 并重建最小 DOM；在生成页面上实际执行原 locator，要求 `count()==1` 且 visible，不能只断言属性字符串存在。Builder 不得凭空猜测线上祖先/兄弟结构。
- [ ] 若原 locator 无法兼容，测试要求 `locator_mapping.json` 中存在 action id、原 locator、替代 locator 和原因。
- [ ] 测试相同 screenshot SHA256 只生成一个 `assets/by-hash/` 文件，并验证 50MB/200MB 体积阈值事件。
- [ ] 实现 document HTML、CSS 和 assets 复制；iframe `src` 指向本地子 document。
- [ ] 运行：`D:/Anaconda/envs/codegen-marker/python.exe -m unittest test.test_build_replica -v`，预期全部 PASS。

### Task 7：本地状态机与交互

**Files:**
- Modify: `build_replica.py`
- Create: `test/test_replica_runtime.py`

- [ ] 写 Playwright e2e：click 同页转移、`expect_popup()`、fill 保留 value、确认后转移、序列 aria-selected 更新。
- [ ] 先运行并确认因 runtime 不存在失败。
- [ ] 生成最小 `replica_runtime.js`，只允许 manifest 声明的 transition。
- [ ] 运行：`D:/Anaconda/envs/codegen-marker/python.exe -m unittest test.test_replica_runtime -v`，预期全部 PASS。

### Task 8：真实重放插桩与批量捕获

**Files:**
- Modify: `batch_capture_replicate.py`
- Modify: `rewrite_script.py`
- Create: `test/test_batch_capture_replicate.py`
- Use fixture server: `test/fixtures/replica_flow/host.html`
- Manual live sample: `out/cxhospital/processed_script_cxhospital.py`

- [ ] 测试脚本的初始 goto 指向 Task 3/4 本地 fixture server；CI 不访问真实医院站点。
- [ ] 写失败测试：bootstrap action 照常执行但不捕获；marked action 前后 hook 顺序正确；action 只执行一次；StateEvidence 按 topology/marker contract/DOM/region/global 优先级决策；fill 默认不导航。
- [ ] 写失败测试：before-hook 等待瞬态元素、滚入视口、detached 重试；hook 捕获失败时原 action 仍由 Playwright auto-wait 执行。
- [ ] 写失败测试：hover prelude 后菜单 action 可捕获；缺失 hover prelude 时产生 `missing_hover_prelude` partial，而不是等待到流程总超时。
- [ ] 写失败测试：CaptureTimingProfile 的 action/marker/flow 上级预算能截断下级 wait/scroll/stability；超时原因可审计。
- [ ] 实现隔离子进程 runner 和 JSON Lines 事件。
- [ ] 单个 action 失败时写 partial/failed 并继续可安全继续的 action；脚本语法/manifest 错配时立即失败。
- [ ] live 模式启动前检查 network/login/site 前置条件并输出分类错误；默认拒绝捕获 bootstrap 和登录页 DOM。
- [ ] 实现 `scripted/interactive/storage-state` 三种 auth mode；interactive 测试用 stdin 发送 `continue_after_auth`，storage state 路径不得写入 manifest。
- [ ] 运行：`D:/Anaconda/envs/codegen-marker/python.exe -m unittest test.test_batch_capture_replicate -v`，预期全部 PASS。

### Task 9：本地 Server 与 replay 脚本

**Files:**
- Modify: `replay_helpers.py`
- Modify: `rewrite_script.py`
- Create: `test/test_replay_script.py`

- [ ] 写失败测试：server 只绑定 127.0.0.1；入口 URL 可访问；offline bootstrap 整块被移除；entry `page/page1` 变量根据 BootstrapPlan 恢复；首个 marked action 可执行。
- [ ] 写失败测试：popup transition 打开 target_page_var 对应页面；popup 内后续 transition 只导航 popup，不导航 opener。
- [ ] 实现 `ReplicaServer` context manager 和生成 `serve_replica_*.py/replay_*.py`。
- [ ] 生成脚本必须通过 `ast.parse()`。
- [ ] 运行：`D:/Anaconda/envs/codegen-marker/python.exe -m unittest test.test_replay_script -v`，预期全部 PASS。

### Task 10：GUI 导出

**Files:**
- Modify: `main_gui.py`
- Create: `test/test_replica_gui.py`

- [ ] 写 Qt 测试：未停止/未保存/无 marker 时按钮禁用；QProcess 事件更新状态；失败信息可见。
- [ ] 实现按钮、保存路径推导、annotations 落盘、QProcess 生命周期和取消。
- [ ] 不在 Qt 主线程执行 Playwright 或 HTML 构建。
- [ ] 运行：`D:/Anaconda/envs/codegen-marker/python.exe -m unittest test.test_replica_gui -v`，预期全部 PASS。

### Task 11：完整离线 E2E

**Files:**
- Create: `test/test_replica_e2e.py`
- Use fixtures: `test/fixtures/replica_flow/`

- [ ] 录制 fixture 的完整流程：bootstrap 登录 → 报告 → popup viewer → iframe → 序列 → Metadata → WL/WW → canvas。
- [ ] 导出 replica，启动 localhost，阻止所有非 localhost 请求。
- [ ] 验证离线 replay 没有执行 bootstrap 登录 locator，并正确恢复 page/page1。
- [ ] 使用原始 frame/popup locator 完成同一路径并提取 Metadata 文本。
- [ ] 对所有 marked action 汇总 selector compatibility：原 locator 命中或显式 mapping 覆盖率必须为 100%。
- [ ] 运行：`D:/Anaconda/envs/codegen-marker/python.exe -m unittest test.test_replica_e2e -v`，预期全部 PASS 且外部请求数为 0。
- [ ] 运行完整回归：`D:/Anaconda/envs/codegen-marker/python.exe -m unittest discover -s test -v`。

---

## 13. 分阶段交付

### Phase 0：正确性 Spike

完成 Task 0。验收：动态噪声不造 state，序列/Metadata/WLWW 关键变化不漏检；fixture 自动化结果存在，真实 cxhospital live 结果已完成或被明确记录为环境阻塞。

### Phase 1A：单文档关键 DOM

完成 Task 1、2、3。验收：截图背景上复刻 button/input/canvas，原顶层 locator 可命中并操作。

### Phase 1B：iframe/popup 与专用区域

完成 Task 4、5、6。验收：双层 iframe locator、序列列表、Metadata、WL/WW 区域在本地 HTML 中存在且可读取。

### Phase 1C：可交互流程状态机

完成 Task 7、8、9。验收：点击产生同页或 popup 状态转移，fill/confirm 行为正确，完整 flow 可由 replay 脚本运行。

### Phase 1D：GUI 和端到端

完成 Task 10、11。验收：一次 GUI 录制与标注可导出可交互 Web；断网条件下 adapter 流程跑通。

---

## 14. 最终验收清单

- [ ] 一个完整录制产生一个 `ReplicaFlow` 和一个 Web 入口。
- [ ] Offline replay 跳过真实 bootstrap；本地 entry page/page1/page2 变量绑定完整。
- [ ] 每个 state 保存可审计 `StateEvidence`；整页 hash 不是唯一状态依据。
- [ ] 动态 spinner/时间戳/角标不会凭空产生 state。
- [ ] 序列切换、Metadata 打开和 WL/WW 确认不会因局部变化过小而漏 state。
- [ ] 页面视觉由真实截图还原。
- [ ] popup 行为与真实流程一致。
- [ ] ReplicaPage 保存 opener、page_var、window name 和 active/closed 状态；transition 明确作用窗口。
- [ ] 单层/嵌套 iframe id/name/层级一致。
- [ ] Frame viewport 截图使用 Page clip + 累积 content offset，并与 Frame CSS rect 对齐。
- [ ] 每个 marked action 保存目标 DOM。
- [ ] 序列、报告、Metadata、WL/WW 保存完整 interaction region。
- [ ] 原 id/class/name/role/ARIA/testid/text 被保留。
- [ ] 原 locator 或有记录的 mapping 可命中关键目标。
- [ ] 每个 marked action 的原 locator 在构建后实际执行 `count()==1` 且 visible，或存在显式 mapping。
- [ ] critical action 原 locator 直接命中率为 100%；全部 marked locator action 直接命中率 ≥ 90%；mapping 率 ≤ 10%。
- [ ] `keyboard.press`、mouse move/click/dblclick/wheel 均有 execute 或 explicit_skip 记录，不存在静默丢弃。
- [ ] `expect_popup` 上下文、body action 和 pageN 赋值解析覆盖率为 100%。
- [ ] click/dblclick/fill/press/select 在本地可执行。
- [ ] 原生 select 的 options 完整可操作；自定义下拉按 click/region 处理。
- [ ] 点击后进入 manifest 指定状态。
- [ ] input value 和 aria-selected 状态正确。
- [ ] canvas selector、尺寸和点击区域可用。
- [ ] 虚拟化序列 region 保存 SeriesCollectionEvidence；未到末尾时必须出现 `series_virtualized_partial`。
- [ ] Rect.coordinate_space 只使用 page_viewport_css/frame_viewport_css/region_content_css 三个明确值。
- [ ] 所有 HTML/图片/JS/CSS 从 localhost 提供。
- [ ] 状态差分使用 PNG/raw bytes，JPEG 只作为视觉资产。
- [ ] 相同 JPEG SHA256 只存一份；50MB/200MB 体积策略生效。
- [ ] CaptureTimingProfile 的 action/marker/flow 总预算生效，超时原因可审计。
- [ ] 外部 HTTP(S) 请求为 0。
- [ ] CI 重放只使用本地 fixture；live 捕获明确要求联网、登录和站点可用。
- [ ] bootstrap/login action 照常执行但默认不保存 DOM 或截图。
- [ ] 产物不包含可执行的原站脚本和认证状态。
- [ ] 单元测试和完整离线 E2E 全部通过。

---

## 15. Anti-pattern Guards

- 不把 iframe 替换成普通 div 后仍宣称支持 FrameLocator。
- 不只保存点击坐标而丢弃 DOM 属性和 frame chain。
- 不把所有 `get_by_*` 当 CSS selector。
- 不通过 `contentDocument` 访问 iframe DOM。
- 不在 GUI 进程中 `importlib.exec_module()` 执行录制脚本。
- 不在打开本地 entry state 后继续执行真实登录 bootstrap action。
- 不假设 `Frame` 存在 screenshot API；Frame 图像必须通过所属 Page clip 或经验证的 iframe content-box 方案获得。
- 不用扁平 document 列表代替 Page/popup opener 关系。
- 不以“目标属性存在”代替原 locator 实际可执行验证。
- 不用 mapping 覆盖率 100% 掩盖原 locator 直接命中率过低。
- 不强制 keyboard/mouse_xy action 构造虚假的 DOM Locator。
- 不把 `expect_popup` 当作普通 Call，忽略 With body 和 pageN 赋值。
- 不把“当前渲染的序列 item”宣称为虚拟列表全部成员。
- 不为自定义下拉伪造原生 `<select>` 语义。
- 不在每个 hook 中独立耗尽 wait/scroll/stability 最大超时，忽略 marker/flow 总预算。
- 不用 JPEG 压缩资产计算像素状态差分。
- 不运行或复制原站 JavaScript。
- 不以完整执行后的最后一张图代表所有状态。
- 不以整页 screenshot hash 作为唯一状态切分依据。
- 不让动态 mask 覆盖当前 action region、序列列表或 viewer canvas。
- 不在 DPR 未归一化时直接用 CSS rect 覆盖 device-pixel screenshot。
- 不为视口外元素生成负坐标透明 hitbox。
- 不对 popup 块做逐行正则注释。
- 不静默修改 locator；所有 mapping 必须可审计。
- 不把真实 URL/token 输出到普通日志。

---

**计划版本**：v2.3  
**更新日期**：2026-07-29  
**当前优先级**：单次完整标注生成可点击、可定位、可供 adapter 开发的交互式复刻 Web。
