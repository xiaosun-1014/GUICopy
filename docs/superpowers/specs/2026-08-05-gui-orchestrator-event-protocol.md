# GUI ↔ Orchestrator 事件协议（JSONL）规格

日期：2026-08-05  
状态：已批准（D1–D4 均已确认）；2026-08-05 补齐终态规则、payload 规范化、agent 子协议  
上游：`docs/superpowers/specs/2026-08-04-one-recording-adapter-replica-pipeline-design.md` §5  
配套：`docs/superpowers/specs/2026-08-05-gui-orchestrator-agent-protocol.md`（agent 子协议）

## 1. 目的与范围

`main_gui.py` 用 `QProcess` 启动 orchestrator（`pipeline.py`），orchestrator
按阶段再起子进程（`agent.py` / live capture / offline validation）。本文档
定义 GUI 与 orchestrator 之间基于 **stdout 单行 JSONL 事件 + stdin 单行
JSON 命令**的交互协议，使 GUI 能实时呈现当前阶段、当前 marker/action、
已用时间、success/partial/failed 数量，并下发登录继续/取消/中止命令，
同时保持 GUI 线程响应。

本协议只覆盖 **GUI ↔ orchestrator** 这一段。orchestrator 与各子进程之间的
协议按既有 `batch_capture_replicate.py` JSONL 事件约定，前置条件见 §4。

## 2. 传输约定

- **编码**：单行 UTF-8 JSON，`\n` 结尾，**每行 `flush()`**。
- **stdout**：仅承载事件（JSONL）；人类可读日志走 stderr 或被包装为
  `log` 事件，不得混入 stdout 破坏解析。
- **stdin**：仅承载命令（单行 JSON）；orchestrator 逐行读取。
- **GUI 解析策略**：逐行 `json.loads`，未知字段忽略、未知 `event` 种类
  跳过（前向兼容）。
- **时序**：orchestrator 启动后**先发一条 `ready` 确认**（见 §5.11），再进入
  首阶段并开始输出其它事件，供 GUI 确认链路可用。

## 3. 事件分类总表

| 事件（`event`） | 来源 | 用途 | GUI 消费 |
|---|---|---|---|
| `stage_started` | orchestrator | 阶段开始 | 更新阶段指示 |
| `stage_finished` | orchestrator | 阶段结束（含状态） | 更新阶段指示 |
| `progress` | orchestrator | 当前 marker/action 渲染 token | 更新当前项 + 计时 |
| `marker_result` | orchestrator（聚合） | 单 marker 明细 | 按 marker_id **upsert**（见 §5.4） |
| `summary` | orchestrator | 权威计数快照（`scope=markers`） | **覆盖**全部计数 |
| `auth_required` / `auth_completed` | **payload 转发**自 live capture | 交互式登录 | 显示/隐藏登录继续 |
| `capture_*` / `build_*` | **payload 转发**自 live capture | 捕获/构建里程碑 | 显示详细进度 |
| `action_failed` | **payload 转发**自 live capture | 单 action 失败 | 标红 |
| `log` | orchestrator | 普通日志行（人工可读） | 追加日志面板 |
| `fatal` | orchestrator | 不可继续的错误（最多一条，见 §5.9） | 展示错误 |
| `completed` | orchestrator | **唯一业务终态**（含产物入口） | 结束 + 显示入口 |
| `ready` | orchestrator | 启动握手（首条输出） | 确认链路可用 |

GUI **消费 `marker_result` 用于更新明细，但业务计数一律以 `summary` 覆盖为准**（D3），不做盲目累加。透传（`auth_*`/`capture_*`/`build_*`/`action_failed`）按「payload 规范化转发」注入统一 envelope，见 §4。

## 4. 事件转发的组织方式（依据已确认的「透传 + 自增」决策）

「原样透传 + 增加 source」并不自洽：只要增加/覆盖 `version/ts/run_id/stage/source`
字段，就不再是字节级原样转发，且子进程若不慎覆盖保留字段会造成碰撞。
因此改为 **payload 规范化转发**：

- 子进程产出的原始 JSON 对象整体放入统一 envelope 的 `payload` 字段，
  orchestrator **不再直接在 child dict 上 `setdefault`**；
- 顶层 `event` 复制 child 事件名（`auth_required`、`capture_started`…），
  使 GUI 仍能按顶层 `event` 统一分发；
- `version/ts/run_id/stage/source` 由 orchestrator 填写；child **不得**出现在
  顶层保留字段位置，也不能覆盖它们；
- child payload schema 仍由 child 自持（child 是各自唯一事实源）；child 协议
  变更不需要重写 orchestrator 转发逻辑；
- GUI 业务逻辑读顶层标准字段；详细信息从 `payload` 读取。
- **保留终态名不转发**：`completed`、`fatal` 是 orchestrator 保留的唯一业务终态/错误名。live-capture 子进程自身会在 stdout 发
  `{"event":"completed","entrypoint":...}` 与 `{"event":"failed",...}`
  （`batch_capture_replicate.py:559/599/493/597`）。orchestrator 转发时**不得**把这些 child 事件名复制到顶层 `event`，
  必须改名（如改发 `capture_completed` / `capture_failed`）或剥离，否则 GUI 会把子进程的阶段完成误判为 run 终态。
  其它保留名同理：child 事件名不得与任何 orchestrator 级事件名（`completed`/`fatal`）撞名。

```jsonc
{
  "version": 1, "ts": "...", "run_id": "...", "stage": "capturing_live",
  "event": "auth_required",
  "source": "subprocess:batch_capture_replicate",
  "payload": { "event": "auth_required", "message": "请完成登录后继续" }
}
```

- **orchestrator 自增**：`stage_started/finished/progress/marker_result/summary/
  completed/fatal/log/ready` 等 orchestrator 级事件由 orchestrator 生成，
  直接使用统一 envelope，不带 `payload`。
- **合成（adapter_generation 阶段）**：`agent.py` 通过 D1 的 `--emit-jsonl`
  （见配套 agent 子协议）输出自身子进程事件，orchestrator 将其规范化后映射
  为 `progress` 与 `marker_result`。**禁止** orchestrator 解析 agent 的 stderr
  文本（中文/emoji/重试混杂/无法稳定携带 marker UUID/无法区分「本次尝试失败
  与 marker 最终失败」）。
- **payload 亦须脱敏**：child 原始 JSON 进入 `payload` 前必须以与
  `message`/`suggestion` 相同的脱敏规则处理（URL query 值、患者标识、token、
  cookie、storage state 不得出现在 payload 任何字段）。脱敏不因「payload 由
  child 自持」而被豁免——orchestrator 在转发前对 payload 做同一脱敏。

## 5. 事件 schema 详表

所有 orchestrator 生成的事件共享信封，payload 转发事件使用 `source` + `payload`：

```jsonc
{
  "version": 1,            // 协议版本，恒为 1
  "ts": "2026-08-05T00:00:00.000Z",  // ISO8601（UTC）
  "run_id": "20260805_003000_abcdef",
  "stage": "adapter_generation",     // 当前阶段名（见下）
  "event": "<kind>",                 // 见 §3 表
  "source": "orchestrator",          // orchestrator 缺省；payload 转发填 child 名，
                                     //   如 "subprocess:batch_capture_replicate"
  "payload": {}                      // 仅在 payload 转发事件中存在，承载 child 原始 JSON
}
```

阶段名（`stage`）为事件字段里的固定枚举，**不是**父文档 §7 状态机的一一映射
（事件枚举是「进行中/已发生」的编排阶段，状态机还含 `draft`、`awaiting_auth`
等更细粒度）：

`preflight / generating_adapter / awaiting_auth / capturing_live /
building_replica / validating_replica / validating_adapter / report`

- `report` 是 **虚拟阶段**：只出现在 `stage`/`stage_finished`/`summary`/
  `completed` 的事件字段里，对应 Stage 7 报告写作，但**不**写入
  `pipeline_state.json`（报告写作不进入持久化状态机）。
- `draft` 状态不入事件枚举（处于 draft 时 orchestrator 尚未启动，无事件）。
- 事件枚举与状态机以此说明为一致口径，不在两处重复列举冲突。

### 5.1 `stage_started`

```jsonc
{ "version": 1, "ts": "...", "run_id": "...", "stage": "generating_adapter",
  "event": "stage_started" }
```

### 5.2 `stage_finished`

```jsonc
{ "version": 1, "ts": "...", "run_id": "...", "stage": "capturing_live",
  "event": "stage_finished",
  "status": "success",   // success | partial | failed | skipped | cancelled
                           // 与 PipelineStatus 一致，不使用 "ok"（避免无意义转换）
  "duration_ms": 45210 }  // 该阶段耗时，供 GUI 展示
```

### 5.3 `progress`

每个渲染 token（当前 marker / 当前 action）发一条；GUI 覆盖显示并刷新计时。

```jsonc
{ "version": 1, "ts": "...", "run_id": "...", "stage": "capturing_live",
  "event": "progress",
  "kind": "marker",                 // marker | action | auth | generic
  "label": "影像画布交互",           // marker label 或 action 描述
  "marker_id": "aaff5f38-...",       // 有则带（marker 级）
  "action_id": "a_000_003" }         // 有则带（action 级）
```

### 5.4 `marker_result`

单 marker 的当前最新明细。**同一 marker 跨阶段会多次发出**（如
`generating_adapter → success`、`validating_replica → success`、
`validating_adapter → partial`），因此 **GUI 不得盲目累加**。

正确语义（D3）：GUI 维护 `marker_id → marker_result` 映射，收到即 **upsert**：

```python
marker_results[event["marker_id"]] = event
```

然后基于最新值重算计数（success/partial/failed/skipped）。最终的业务统计以
`summary`（§5.5）为准，GUI 收到 `summary` 时**覆盖**而非叠加。这样同一步骤
从 success 变为 partial 时，统计正确地由 `success=1,partial=0` 更新为
`success=0,partial=1`。

```jsonc
{ "version": 1, "ts": "...", "run_id": "...", "stage": "validating_adapter",
  "event": "marker_result",
  "marker_id": "aaff5f38-...",
  "label": "影像画布交互",
  "status": "partial",           // success | partial | failed | skipped
  "locator_risk": "structural",  // 本 marker 关键 locator 最高风险（§11 分类）
  "artifact_brief": { "canvas_frames": 278, "report_jpeg": true },
  "note": "canvas 动态帧为 unsupported，按能力收缩判 partial" }
```

### 5.5 `summary`

**权威计数快照**，语义是「每个 marker 当前最新状态的累计」，**不是**事件数、
阶段数、尝试次数或 action 数。GUI 收到即覆盖全部计数（D3）。每个
`stage_finished` 后跟随一张（`stage_finished` → `summary`），状态为
success/partial/failed/skipped 均发送；最终 `completed` 再携带同一份。

```jsonc
{ "version": 1, "ts": "...", "run_id": "...", "stage": "report",
  "event": "summary", "scope": "markers",
  "success": 2, "partial": 1, "failed": 0, "skipped": 0 }
```

首版仅实现 `scope=markers`。`scope` 字段为将来阶段级计数（`scope=stages`）
预留，现不启用。

### 5.6 `capture_*` / `build_*`（payload 转发示例）

来自 live capture 子进程，顶层 `source` + `payload` 标注，child payload
schema 由子进程定义：

```jsonc
{ "version": 1, "ts": "...", "run_id": "...",
  "stage": "building_replica",
  "event": "build_finished",
  "source": "subprocess:batch_capture_replicate",
  "payload": { "event": "build_finished", "entrypoint": ".../replica/index.html" } }
```

### 5.7 `auth_required` / `auth_completed`（payload 转发） 

沿用既有事件名，顶层 `event` 复制 child 名、原始内容放 `payload`。GUI 收到
`auth_required` → 显示「登录完成，继续」按钮；收到 `auth_completed` → 隐藏。

```jsonc
{ "version": 1, "ts": "...", "run_id": "...", "stage": "awaiting_auth",
  "event": "auth_required",
  "source": "subprocess:batch_capture_replicate",
  "payload": { "event": "auth_required" } }
```

### 5.8 `log`

人类可读日志（orchestrator 或子进程 stderr 收集后），不上报业务状态。

```jsonc
{ "version": 1, "ts": "...", "run_id": "...", "stage": "capturing_live",
  "event": "log", "level": "warn", "message": "series region 部分采集" }
```

### 5.9 `fatal`

orchestrator 遇到不可继续的错误（如权限泄漏、或某阶段不符合 §10.3 Failed
条件）时发出的**非终态信号**。**`fatal` 不是最后一条 stdout 事件**（见
§5.10 后的 D4 终态规则）：发出 `fatal` 后 orchestrator 必须清理
active child/browser/server、尽可能写 `pipeline_state`/report，然后发送一次
`completed(status=failed)`，再以非零 exit code 退出。一个 run **最多发一条
`fatal`**。

```jsonc
{ "version": 1, "ts": "...", "run_id": "...", "stage": "validating_adapter",
  "event": "fatal", "error_category": "offline_external_request",
  "message": "离线验证发现非 localhost 请求", "suggestion": "检查 completed adapter 是否含真实 URL bootstrap" }
```

`message`/`suggestion` 必须使用 §8 稳定错误分类 + 脱敏后的建议，不含
URL query 值、患者标识、token、cookie、storage state。

### 5.10 `completed`

**唯一业务终态**（success/partial/failed/cancelled 之一），是正常协议路径上
的**最后一条 stdout 事件**。业务成功与否只认 `completed.status`。携带最终
产物入口供 GUI 跳转，并携带与最近一张 `summary` 相同的计数快照。

```jsonc
{ "version": 1, "ts": "...", "run_id": "...", "stage": "report",
  "event": "completed", "status": "partial",
  "artifacts": {
    "adapter": ".../adapter/completed_ftimage.py",
    "offline_adapter": ".../adapter/completed_ftimage_offline.py",
    "replica": ".../replica/index.html",
    "report_html": ".../pipeline_report.html",
    "report_json": ".../pipeline_report.json" },
  "summary": { "success": 2, "partial": 1, "failed": 0, "skipped": 0 },
  "run_id": "20260805_003000_abcdef" }
```

### 5.10.1 D4 终态规则（`fatal` 与 `completed` 的关系）

- **`fatal` 是错误警报，不是终态；`completed` 才是唯一业务终态。**
- 一个 run 必须满足：**最多一条 `fatal`，恰好一条 `completed`**；
  `completed` 必为正常协议路径最后一条事件；`fatal` 之后**必须且只允许**
  发送 `summary` 与 `completed`（不允许第二个 `fatal`、不允许先 `completed`
  再 `fatal`、不允许两个 `completed`）。
- 成功路径：`… → stage_finished → summary → completed(status=success|partial)`
  → exit 0。
- 失败路径：`… → fatal → summary → completed(status=failed)` → exit 非 0。
- 取消路径（`cancel`/`abort` 命令）：`… → completed(status=cancelled)`
  → exit（不发出 `fatal`）。
- 若进程崩溃到无法发出 `completed`，GUI 展示
  `protocol_failure: orchestrator exited without completed event`；这**不是**
  业务 success，GUI **不得**仅凭 exit code 推断业务状态。
- **被 fatal 中断的阶段**：D4 规定 `fatal` 之后只许 `summary`+`completed`，
  因此被 fatal 杀死的当前阶段**不会**收到自己的 `stage_finished`。这一规则
  成立——被中断阶段以 `fatal` 作为其终止信号。GUI 对「收到 `stage_started`
  但未收到对应 `stage_finished`」的阶段一律显示为 `interrupted`（中断），
  不悬挂为「进行中」。

### 5.11 `ready`

启动握手，orchestrator 的**首条输出**，语义为「链路就绪」（原计划 `pong`
没有对应 `ping`，改名 `ready` 更准确）：

```jsonc
{ "event": "ready", "version": 1, "run_id": "..." }
```

GUI 收到 `ready` 前不渲染最终状态；收到后才开始把后续事件映射到界面。
仅当未来确需显式探测才引入 `{"command":"ping"}` 及其 `pong` 响应，首版不实现。

## 6. 命令协议（GUI → orchestrator stdin）

| 命令 | 用途 |
|---|---|
| `{"command":"continue_after_auth"}` | 交互式登录完成，命令 live capture 继续（沿用既有语义） |
| `{"command":"cancel","reason":"user_cancelled"}` | 请求优雅退出：先让 orchestrator 请求子进程优雅退出，等待宽限期后强制终止 |
| `{"command":"abort","reason":"ui_close"}` | 强制终止：orchestrator 立即 kill 全部子进程并清理 server/浏览器后退出（用于 GUI 关闭而无宽限等待） |

命令下发的唯一目标方在当前阶段决定：`awaiting_auth` 阶段的
`continue_after_auth` 透传给 live capture 子进程；`cancel`/`abort` 由
orchestrator 本级处理（也携带到子进程）。GUI 关闭时（`closeEvent`）若
orchestrator 仍在运行，先发 `abort` 再等退出并提示任务已被终止（规格 §9）。

`ping` 为本协议的探测命令，仅在未来确需显式握手时启用（响应 `pong`）；
首版不实现，避免 `ready` 与 `ping/pong` 语义重复。

### 6.1 命令路由（D2）

- `continue_after_auth`：GUI → orchestrator → **仅**转发给当前 live-capture
  child；其他阶段收到该命令忽略并以 `log(warn)` 提示。
- `cancel`：GUI → orchestrator 设置 cancel flag → 请求 active child 优雅
  退出 → 等待 grace period → 必要时终止**精确进程树** → 写 cancelled 状态
  与报告 → `completed(status=cancelled)`。
- `abort`：GUI → orchestrator 立即终止 active child 精确进程树 → 清理
  browser/server → 写 cancelled 状态与报告 → `completed(status=cancelled)`。

### 6.2 实现约束

- 每条命令单行 JSON、`\n` 结尾；每条事件单行 JSON、`\n` 结尾；每次写后
  `flush()`。
- stdin reader 不得在 orchestration 主线程执行阻塞式 `readline()`；用
  **daemon reader thread + queue**。
- 非法 JSON 命令 → 脱敏 `log(level="warn")` 事件，**不**导致 orchestrator
  崩溃。
- 未知命令 → `log(level="warn")`。
- 命令中**不允许**携带 storage state 内容、密码、token。
- Orchestrator ↔ 子进程沿用同一方向约定：orchestrator → child stdin 命令，
  child → orchestrator stdout 事件，child → orchestrator stderr 人工日志。
  这是同一进程的三个独立 pipe，各自承担一个方向与语义，不是共用一条通道。

## 7. GUI 状态呈现映射

| GUI 元素 | 订阅事件 | 逻辑 |
|---|---|---|
| 当前阶段 | `stage_started` / `stage_finished` / `fatal` | 显示文本 + 阶段完成状态；有 `stage_started` 但无 `stage_finished` 且已出 `fatal` 的阶段显示 `interrupted` |
| 当前 marker/action | `progress` | 覆盖显示 label + 自该事件起计时 |
| 已用时间 | `progress` / `stage_finished` | 距最近 `progress` 的耗时（GUI 侧自计） |
| success/partial/failed 数 | `marker_result` + `summary` | 维护 `marker_id`→result 映射 upsert；呈现统计算 `summary` 为覆盖 |
| 登录继续按钮 | `auth_required` / `auth_completed` | 显示/隐藏（payload 转发） |
| 取消按钮 | — | 常显；点击发 `cancel` |
| 最终入口 | `completed` | 渲染 adapter/replica/report 链接 |
| 日志面板 | `log` / payload 转发子进程事件 | 追加显示 |
| 终止提示 | `fatal` / `completed(status=cancelled)` | 弹窗 |

## 8. 反模式与约束

- GUI **绝不**把 orchestrator（或任何子进程）的 exit code 直接映射为业务
  success；业务终态唯一来自 `completed.status`（规格 §15）。
- orchestrator **绝不**把子进程的原始错误文本（含 URL/患者标识）原样写入
  事件 `message`/`suggestion`；对外一律使用 §8 稳定错误分类 + 脱敏后的
  建议（规格 §12）。
- 事件数量需有界：`progress`/`marker_result` 不得高频刷屏；每 action 最多
  若干条，orchestrator 负责节流（如每 action 最多 1 条 `progress`）。
- 同 `event` 名冲突的判定以 §4「保留终态名不转发」为先：child 的
  `completed`/`failed` 在转发层已被改名/剥离，**不会**进入顶层 `event`，因而
  GUI 不会把子进程阶段完成误判为终态。其余非保留名的 child 事件与
  orchestrator 事件不重名。
- GUI **禁止**对 `marker_result` 盲目累加（同一 marker 跨阶段多次发出会重复
  计数）；必须按 `marker_id` upsert，最终业务统计以 `summary` 覆盖为准（D3）。
- 终态唯一性（D4）：GUI 只认 `completed.status`；`fatal` 之后仍必须有一个
  `completed`；不得允许两个 `completed`、不得先 `completed` 再 `fatal`。
- 事件/命令**禁止**携带 storage state、密码、token；`fatal`/`completed`/
  `log` 的文本字段须为 §8 稳定分类 + 脱敏后内容。

## 9. 待确认决策点（已结案）

以下决策均已批准，实施时按此执行：

- **D1（批准）**：给 `agent.py` 增加 `event_sink` 参数与 `--emit-jsonl` 开关，
  让 adapter 生成阶段产生真正的 JSONL 事件；orchestrator **不**解析 agent
  stderr。细节见配套 `2026-08-05-gui-orchestrator-agent-protocol.md`。
- **D2（批准）**：三方向严格分离——GUI→orchestrator stdin 命令、
  orchestrator→GUI stdout 事件、→GUI stderr 人工日志；orchestrator↔子进程
  同方向。stdin reader 用 daemon 线程 + queue。
- **D3（批准）**：每个 `stage_finished` 后发一张覆盖式 `summary`
  （`scope=markers`）；GUI 对 `marker_result` upsert、对 `summary` 覆盖。
- **D4（批准）**：`fatal` 是错误警报非终态；`completed` 是唯一业务终态；
  每个 run 最多一条 `fatal`、恰好一条 `completed`（§5.10.1）。

## 10. 验收清单（Level 1 单测覆盖点）

- 每条事件单行 JSON 且 `flush()`（测试伪造 orchestrator 子进程吐出多类事件）。
- 未知 `event` 种类被 GUI 跳过、不崩。
- stdin 命令层：以注入的 fake child subprocess 断言——`cancel` 先向 child
  stdin 发优雅退出信号、grace period 期满才 terminate 进程树；`abort` 立即
  terminate 进程树。（`cancel` 与 `abort` 的区分**不可用**「最终
  `completed(status=cancelled)` 相同」断言，必须用「对 child 的终止信号
  序列不同」断言；这才是可观察的差异。）
- stdin reader：非法 JSON 命令 → 脱敏 `log(warn)` 且 orchestrator 不崩；
  未知命令 → `log(warn)`；阻塞式 `readline` 不在主线程。
- `marker_result` 按 `marker_id` upsert + `summary` 覆盖逻辑正确（同一 marker
  从 success→partial 时计数由 `success=1,partial=0` 更新为
  `success=0,partial=1`）。
- `ready` 握手时序：GUI 收到 `completed` 前不发终态。
- D4：`fatal` 后仍且仅发一次 `completed(status=failed)`；被中断阶段无
  `stage_finished` 时 GUI 显示 `interrupted`；进程异常退出且无 `completed`
  时 GUI 显示 `protocol_failure` 且不判定业务成功。
- payload 转发：`auth_required/auth_completed`（顶层 `event` + `payload`）
  驱动 GUI 登录继续按钮显示/隐藏；child 顶层 `completed`/`failed` 事件被改名
  或剥离，**不会**触顶层的 `completed(status=...)`。
- agent 子协议：`agent.py --emit-jsonl` 未带 `--output` 时以 `parser.error`
  失败；默认 CLI 行为（无 `--emit-jsonl`）产出完全不变（见配套 agent 子协议）。
- 脱敏（基于 **seeded registry**）：测试预置一个敏感值 registry（含已知
  患者姓名、accession、URL query 值、token、storage-state 路径），断言
  `fatal`/`completed`/`log` 的 `message`/`suggestion`/`payload` 不含这些
  seeded 值。隐私断言只对 seeded 值成立（§12 已承认无法自动识别任意患者
  文本，因此清单不要求对未知文本断言）。
