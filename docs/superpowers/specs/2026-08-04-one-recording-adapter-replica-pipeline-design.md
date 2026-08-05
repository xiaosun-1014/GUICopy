# 一次录制生成 Adapter 与离线复刻网页：产品化闭环设计

日期：2026-08-04  
状态：已批准；2026-08-05 根据二次审阅补强成功标准与能力边界的自洽  
范围：`D:\00-Project\04-codegencopy`

## 1. 背景

当前仓库已经具备两条可分别工作的链路：

1. `main_gui.py` 录制 Playwright 操作并保存带 marker 的 `processed_script_*.py`；
2. `agent.py` 根据 marker 和 skills 生成 `completed_*.py`；
3. `main_gui.py` 可单独启动 `batch_capture_replicate.py`，捕获录制状态并构建离线 replica。

现有 GUI 导出 replica 时不会调用 `agent.py`，当前也没有使用生成后的 completed adapter 操作 replica 的离线验证步骤。因此，“录制一次”可以作为两条链路的共同输入，但还没有形成一个具有统一状态、统一成功标准和统一报告的一键产品闭环。

Replica 链路已经具备 live/offline-build CLI、前后状态捕获、
popup/iframe/page 拓扑、manifest、JSONL 事件、交互式登录、annotations
hash 校验、错误分类和 GUI QProcess 外壳。产品化工作必须优先复用这些
能力；主要新增量是统一 orchestrator、严格验证/报告，以及 completed
adapter 操作 localhost replica 的 offline runner。不得把已有 replica
能力按全新系统重复实现。

## 2. 产品目标

用户完成一次带 marker 的 GUI 录制后，通过一个 GUI 操作完成：

1. 生成可操作真实站点的 `completed_{hospital}.py`；
2. 重放 processed 脚本并捕获真实页面状态；
3. 构建可交互的本地 replica；
4. 生成只替换 bootstrap 的 offline adapter runner；
5. 在禁止外部网络的环境中验证 completed adapter 的关键操作；
6. 输出统一、可追溯、脱敏的验证报告；
7. 明确区分 `success`、`partial`、`failed` 和 `cancelled`。

首版产品承诺：

> 用户完成一次带 marker 的录制后，可以通过一个 GUI 操作生成线上自动化 adapter、可交互的离线复刻网页和验证报告；系统明确区分成功、部分成功和失败，并能在断网环境中验证关键 adapter 操作。

## 3. 非目标

首版不实现：

- 复制或运行原站 JavaScript；
- 完整模拟 DICOM 渲染引擎；
- 自动破解验证码；
- 将患者数据上传到外部服务；
- 在 Qt GUI 主进程内执行 LLM、录制脚本或 Playwright；
- 并行执行多个真实站点捕获任务；
- 从任意失败点恢复完整浏览器会话；
- 保证未知 viewer 零配置成功；
- 允许 LLM 自由重写整个录制脚本；
- 将 canvas 可点击等同于医学影像动态渲染已完整复刻。

## 4. 核心概念与边界

### 4.1 processed script

`processed_script_{hospital}.py` 是用户示教事实和 marker/action 关系的唯一来源，用于：

- 保存真实录制动作；
- 生成 annotations；
- 生成 completed adapter；
- 对真实网站执行 live capture。

Replica 捕获必须使用 processed script，不使用 completed adapter。后者可能增加自动序列选择、循环翻帧和额外点击，使用它捕获会污染用户示教形成的状态机。

### 4.2 completed adapter

`completed_{hospital}.py` 是用于真实站点自动化的最终脚本：

- Meta 和 canvas 优先走确定性生成；
- 序列选择允许通过受控 LLM 生成；
- WL/WW 和布局切换保留录制的示教动作；
- 共享缺陷通过修改 `skills/` 后重新生成，禁止把医院特例直接补丁到 completed 文件。

### 4.3 replica

Replica 是用于断网开发、定位和回归验证的本地交互环境，负责保留：

- 报告页；
- popup 和 iframe 拓扑；
- 序列候选；
- Metadata 面板；
- WL/WW 输入；
- canvas locator 和基础交互；
- marker 对应的状态转换。

Replica 不保存认证状态，不复制原站脚本，不承诺动态影像像素与真实 viewer 一致。

### 4.4 offline adapter runner

`completed_{hospital}_offline.py` 不重新生成业务逻辑，只替换 completed adapter 的 bootstrap：

- 启动 localhost replica server；
- 用本地入口替换真实 URL；
- 跳过登录和凭据输入；
- 恢复 adapter 使用的 `page/page1/...` 变量；
- 从第一个业务 marker 开始执行；
- 阻止并记录所有非 localhost 请求；
- 记录每个 marker 的 locator、状态和产物结果。

它用于证明 completed adapter 可以操作 replica，而不只是证明 replica 自带的 replay 脚本可以运行。

实现必须复用 `rewrite_script.parse_action_plan()` 生成的
`BootstrapPlan.skipped_in_offline_replay`、`entry_page_bindings`，以及
`generate_replay_script()` / `generate_serve_script()` 已有的本地 server、
page binding、popup 和 locator 恢复模式。新增代码只负责把 completed
adapter 的 marker 业务段接入该骨架，不另写第二套 server、page binding
或 manifest replay 引擎。

## 5. 总体架构

新增独立的 pipeline orchestrator。GUI 负责输入配置、启动后台进程、显示事件和发出取消/继续登录命令。Orchestrator 负责阶段编排、状态持久化、超时、重试、报告和退出清理。

```text
GUI recording + markers
        |
        v
processed script + annotations
        |
        v
Pipeline Orchestrator
  |-- Preflight
  |-- Adapter Generation
  |-- Live Capture
  |-- Replica Build
  |-- Replica Validation
  |-- Offline Adapter Validation
  `-- Verification Report
```

每个高风险执行步骤使用独立子进程。GUI 不直接导入或执行用户录制脚本。

进程部署形态：orchestrator 本身是一个可独立运行的 CLI 脚本
（`pipeline.py`，含 `--input script --annotations ... --output dir` 等入参）。
GUI 用 `QProcess` 启动它（沿用现有 `batch_capture_replicate.py` 的
`readyRead`/`finished` 外壳模式），orchestrator 再按阶段通过子进程分别
调用 `agent.py`、live capture、offline validation；子进程的 JSONL 事件
由 orchestrator 汇总后，以统一事件流转发到 stdout，GUI 解析并在界面呈现，
因此多了一层子进程也能保持 GUI 线程响应与复数登录/取消命令下发不变。

GUI ↔ orchestrator 的事件与命令链路、`fatal`/`completed` 终态规则、
marker 计数的 upsert+summary 覆盖语义，已在配套事件协议规格中定义：
`docs/superpowers/specs/2026-08-05-gui-orchestrator-event-protocol.md`；
其中 adapter 生成阶段的 agent 子协议见
`2026-08-05-gui-orchestrator-agent-protocol.md`。处理这些协议的一致性
问题（D1–D4）后再进入实现。

## 6. Pipeline 阶段

### 6.1 Stage 1：Preflight

检查：

- processed script 可以通过 `ast.parse`；
- 至少存在一个受支持 marker；
- marker ID、行号和 annotations source hash 有效；
- marker 位于合法 Playwright 作用域；
- 所需页面变量可以解析；
- Playwright 和浏览器可用；
- adapter 生成所需的模型配置可用；
- 输出目录可写；
-认证模式配置完整；
- storage state 文件存在但不会被复制；
- 日志和 URL 脱敏器已启用；
- 本次 run ID 和输出目录未发生冲突。

解释器是硬性前置条件：GUI、orchestrator、agent、live capture 和 offline
validation 子进程统一使用
`D:/Anaconda/envs/codegen-marker/python.exe`。该文件不存在时 preflight
失败，不允许静默回退到 system Python 或 GUI 的其他 `sys.executable`。

Preflight 失败时禁止启动浏览器。

### 6.2 Stage 2：Adapter Generation

复用 `agent.py` 的生成逻辑，输出 `completed_{hospital}.py`。完成条件：

- 所有需要补全的 marker 均已处理；
- 不应替换的示教 marker 保持原样；
- 没有残留未实现注释或占位代码；
- 生成 patch 没有越过 marker 边界；
- `ast.parse` 和 `py_compile` 通过；
- 保存模型、prompt hash、skill bundle 的内容 sha256 指纹和有限的脱敏诊断信息；
- 生成失败时不发布伪成功 completed 文件。

关于「skill 版本」：本项目不是 git 仓库，`skills/` 下 SKILL.md 也没有
version 字段，因此用「该 marker 对应 skill bundle 全部文件的拼接
sha256」作为版本指纹（与 source hash / prompt hash 同源），记录到
报告里；不依赖任何版本管理工具。

### 6.3 Stage 3：Live Capture

复用并强化 `batch_capture_replicate.py`：

- 对 processed script 进行隔离插桩；
- 从 annotations 绑定稳定 marker ID；
- 在 action 前后捕获状态；
- 捕获 popup、iframe 和页面变量拓扑；
- 生成 manifest 和 locator 风险报告；
- stdout 与 stderr 同时消费；
- 支持全局、action、页面稳定、登录和退出宽限超时；
- 取消时清理子进程、浏览器和临时 server；
- 不覆盖 processed script。

Marker 身份必须显式映射：GUI annotations 中的 UUID 是跨保存稳定 ID；
`parse_action_plan()` 的 `m_{index:03d}` 只能作为没有 annotations 时的
临时 ID。Live capture 读取 annotations 后，按“真实源代码行号 + 规范化
label”一一匹配 marker group，并把 UUID 写回 group、ActionTarget、
snapshot 路径、manifest 和报告。缺失、重复或 label 不一致属于 preflight
失败，禁止仅校验 source hash 后忽略 annotations 内容。

`annotations.line` 的语义必须钉死为「指向 marker 注释行本身」（而非其
后的首个 action 行），写入 annotations schema 注释并保持与
`parse_action_plan` 的 marker group 行号基准一致，避免出现 1 行的
行列偏移；按“行号 + 规范化 label”双键匹配以消除歧义。

### 6.4 Stage 4：Replica Build

基于 manifest 构建自包含 localhost replica，包括：

- 主入口和状态页面；
- popup 页面；
- 真实 iframe 子文档；
- 基于 hash 去重的 JPEG 资产；
- 最小语义 DOM；
- 允许清单声明的状态转换；
- `serve_replica.py`；
- `replay_replica.py`。

构建阶段不依赖真实网络和原站 JavaScript。

### 6.5 Stage 5：Replica Validation

先验证 replica 本身：

- server 可以启动和停止；
- main、popup、iframe 拓扑可访问；
- 每个 critical action 的 locator 命中数量为 1 且可见；
-声明的状态转换可到达；
- 没有非 localhost 请求；
- manifest、文档和资产引用完整；
- sanitizer 未泄漏凭据、token 或 storage state。

Stage 5 的驱动是 manifest 生成的 `replica/replay_replica.py` 和
manifest locator recipes；它验证 replica 自身忠实承载了已捕获动作，
不执行 completed adapter。

### 6.6 Stage 6：Offline Adapter Validation

生成并执行 offline adapter runner：

- 保持 completed adapter 的 marker 业务逻辑；
- 仅替换真实站点 bootstrap；
- 禁止外部网络；
- 记录每个 marker 的开始、完成、失败、locator 和产物；
- 验证报告截图、patient info、DICOM Meta 和 canvas frames；
- 根据 replica 能力声明区分 locator/点击/状态转换/动态像素验证。

Stage 6 的驱动是新生成的 `completed_{hospital}_offline.py`；它验证
completed adapter 的业务 marker，而不重新执行 manifest replay。

Offline runner 必须在 BrowserContext 上安装强制路由：

```python
context.route("**/*", route_handler)
```

仅允许当前 `ReplicaServer` 的 `http://127.0.0.1:<port>` origin，以及
`data:`、`blob:`、`about:blank`。其他请求先记录脱敏 URL，再 `route.abort()`；
存在任何记录时最终结果为 `offline_external_request`。

**离线隔离覆盖到进程级 egress**：浏览器 route 只拦页面/子进程发起的请求，
不拦 executed adapter 自身进程的 Python 级出站（`requests`/`urllib`/原始
`socket`/`http.client` 等）。因此 offline runner 子进程还必须：

- 以独立子进程执行 adapter，并对该进程施加进程级 egress 阻断或检测（如
  设置受限网络环境、代理/路由层拦截、或 hook 出站调用并记录）；
- 任何非 localhost 的 Python 级出口连接同样先记录脱敏 URL/目标，再阻断，
  与浏览器 route 一并计入 `offline_external_request`；
- 使「外部请求为零」（§10.3）同时涵盖浏览器与进程两条通道，而非只覆盖
  浏览器 route。

Replica 不运行原站 viewer JavaScript，因此报告必须输出能力声明矩阵：

| Adapter 能力 | Replica 验证等级 |
|---|---|
| 普通 locator 唯一性、可见性、点击、fill | supported |
| popup、iframe、manifest 状态转换 | supported |
| 序列 DOM 候选与选择状态 | supported 或 degraded，取决于 region 完整性 |
| Metadata DOM 读取 | supported 或 degraded，取决于捕获行完整性 |
| canvas 元素定位、聚焦、点击 | supported |
| cornerstone/pageTurn 等原站 JS API | unsupported |
| 键盘、滚轮、slider 事件路由 | degraded；只能证明事件可发送 |
| canvas 动态帧像素变化和逐帧医学内容 | unsupported |

Offline adapter 遇到明确为 unsupported 的 viewer JS API 时必须记录降级
证据；不能把 replica 能力边界误报为 adapter 线上失败。需要动态 canvas
帧变化的 marker 最高为 `partial`，除非未来 replica 明确实现并验证对应
动态状态。

Offline runner 按 marker 顺序执行完整 marker 序列（而非只跑单个
marker），前序 marker 正常建立 `seq_frames` / `seq_name` 等运行时局部
变量，后序 canvas / Meta marker 通过 `locals()` 读取，保证与真实运行的
上下文依赖一致。

### 6.6.1 关键能力收缩（critical capability contraction）

`critical marker` 的定义基于 replica 的能力边界收缩：一个 marker 只有当
其承担的关键能力在 replica 中被声明为 `supported` 时才能被计入 critical
集；能力为 `degraded` 或 `unsupported` 的 marker 不进 critical 集，其离线
验证以 `partial` 为上限。成功标准的“所有 critical marker 通过”依据
**收缩后的 critical 集**判定，从而避免“带有 canvas 动态帧的医院永远无法
达到 success”的矛盾（见 §10.1 / §16）。是否计入 critical 与 locator 风险
（§11）独立判定，二者取较低者作为该 marker 的离线结果上限。

### 6.7 Stage 7：Verification Report

输出 `pipeline_report.json` 和 `pipeline_report.html`，包含：

- run ID、输入 hash 和版本；
- 各阶段时间、耗时与状态；
- marker 和 critical action 结果；
- locator 风险分布；
- popup/frame 恢复结果；
- 外部请求列表；
- 产物存在性、格式、数量和大小；
- 隐私检查结果；
- `success/partial/failed/cancelled`；
- 脱敏错误分类和可操作建议；
- 日志和本地产物位置。

报告不内嵌患者姓名、检查号、token、cookie 或 storage state。

## 7. 状态机

持久化文件：

- `pipeline_state.json`：当前稳定状态；
- `pipeline_events.jsonl`：仅追加事件流。

主要状态：

```text
draft
preflight
generating_adapter
awaiting_auth
capturing_live
building_replica
validating_replica
validating_adapter
success
partial
failed
cancelled
```

第一版不恢复中间浏览器会话，但允许基于已有稳定产物重新执行：

- adapter generation；
- replica build；
- replica validation；
- offline adapter validation。

**rerun 的 run_id 语义**：每次重新执行（无论从哪个产物继续）都**新建一个
`run_id`**，通过**引用**复用前一次稳定产物（`capture/manifest.json`、
`adapter/`、`replica/`、`annotations` 等），而不是覆盖历史 run。因此状态机
**不需要**从终态（success/partial/failed/cancelled）出重入边回到活动态——
每次重跑都是独立 run 的生命周期；`pipeline_state.json` 每次只描述当前 run。
「预检 run_id 冲突」（§6.1）据此语义判断：复用产物前先校验其存在性与 hash，
不与历史 run_id 竞争。每个可重跑阶段的产物前置条件（如 offline adapter
validation 需要 `adapter/completed_*_offline.py` 与 `replica/`）在预检时按
run 声明。

## 8. 错误分类和恢复

稳定错误分类：

- `preflight`
- `llm_configuration`
- `adapter_generation`
- `authentication`
- `network`
- `authorization`
- `site_unavailable`
- `selector_failure`
- `page_state_timeout`
- `popup_timeout`
- `frame_resolution`
- `capture_failure`
- `replica_build`
- `offline_external_request`
- `artifact_validation`
- `privacy_violation`
- `cancelled`

每个阶段必须定义：

- 是否允许重试；
- 最大重试次数；
- 是否可以保留产物；
- 是否可以继续为 partial；
- 用户可见的下一步建议；
- 是否必须清理浏览器或 server。

网络、登录、权限和站点不可用不得混为一个错误。Critical action 的 locator 或状态失败不得被普通 warning 掩盖。

## 9. 超时和进程管理

配置至少包括：

- pipeline 总超时；
- adapter 单次模型调用超时；
- live capture 总超时；
- 单 action 超时；
- 页面稳定等待超时；
- popup/frame 等待超时；
- 手动登录等待超时；
-子进程退出宽限期。

实现要求：

- 并行读取 stdout/stderr，避免管道填满；
- JSONL 事件与普通日志分离；
- 取消时先请求优雅退出，再强制终止；
- 验证浏览器和本地 server 不残留；
- GUI 关闭时提示正在运行的任务；
- GUI 始终保持响应。

## 10. 成功标准

### 10.0 run 级终态裁决（completed.status 的聚合规则）

`completed.status` 由以下**优先级阶梯**计算，不是对 §10.1/§10.2/§10.3 的
无优先级 AND/OR 判定：

1. **任一 §10.3 Failed 条件触发 ⇒ `failed`**（失败优先，不得被警告或 partial 掩盖）；
2. 否则，若**没有任何 marker 达到 success 级**（即收缩后 critical 集为空，
   或所有被验证 marker 均为 partial/失败）⇒ `partial`；
3. 否则，若任一 marker 为 `partial` ⇒ `partial`；
4. 仅当全部 critical marker 均为 success 且无任何 partial ⇒ `success`。

此规则与 §6.6.1 收缩一致：**空 critical 集 → `partial`**（不判 success，避免
“没有关键能力被验证为成功却算成功”的空洞）。`completed.status` 由 orchestrator
据此聚合各阶段与 marker 结果得出，不得凭过程推断或 exit code 得出。

### 10.1 Success

必须同时满足：

- processed script 与 annotations hash 一致；
- 所有 marker 被识别；
- completed adapter 生成并通过静态检查；
- live capture 正常完成；
- 所有 critical action 有有效捕获；
- manifest schema 和引用有效；
- replica 构建并启动成功；
- popup/frame 拓扑恢复；
- offline adapter 成功执行所有 critical marker，其中 critical marker 按
  §6.6.1 基于 replica 能力声明收缩（能力为 degraded/unsupported 的 marker
  不进 critical 集，以 partial 为上限）；
- critical locator 唯一且可见；
- 所需产物存在且有效；
- 外部请求为零；
- 隐私检查通过；
- 所有进程正常退出。

### 10.2 Partial

允许保留产物但不能显示完全成功：

- 非关键 action 缺失；
- selector 只能依赖 ordinal 或坐标；
- 序列区域为部分采集；
- Metadata 低于质量门槛但仍有有效数据；
- canvas 只能验证定位和点击，不能验证动态帧变化；
- 非关键辅助产物缺失；
-部分 viewer 语义仅以截图保留。

### 10.3 Failed

任一条件成立即失败：

- adapter 生成或语法失败；
- critical marker 未补全；
- 无法到达首个业务 marker；
- critical action 无有效状态；
- popup/frame 拓扑丢失；
- replica 不能启动；
- offline adapter 无法命中关键 locator；
- offline 验证产生任何外部请求——浏览器 route 或 adapter 进程自身 egress（见 §6.6）；
- 泄漏密码、token、cookie 或 storage state；
- required artifact 为空或不可解析；
- 超时或无法清理进程。

## 11. Locator 风险规则

每个 action 分类：

1. stable id；
2. accessible role/name；
3. stable attribute；
4. text；
5. ordinal/nth；
6. structural selector；
7. absolute coordinate。

Critical action 如果只有高风险 selector 或绝对坐标，最高只能是 `partial`。不能通过增加映射数量把关键 locator 风险包装为成功。

`_always_after` 强制产生新状态不覆盖 locator 风险：若进入该新状态的
critical action 只依赖 ordinal/structural/absolute-coordinate locator，
整个 run 最高仍为 `partial`。

## 12. 隐私与安全

- 密码、验证码和 token 不进入 snapshot、manifest、replica 或报告；
- storage state 只作为显式输入，绝不复制到 run 目录；
- URL query value、患者姓名、检查号和 accession 在日志中脱敏；
-截图、Metadata 和 canvas 标记为敏感本地产物；
- run 输出默认被 `.gitignore` 排除；
- sanitizer 在不含敏感内容的**持久化模型与落盘产物**上执行；澄清：原始 DOM
  快照是由 Playwright 在浏览器侧读入本进程内存后才可净化，因此「净化发生在
  进入内存前」不可能实现。实际时序是「读取原始快照 → sanitize_html 净化 →
  构造持久化模型/落盘」。与下文一致，sanitizer 保证的是**已知凭据模式**与
  可落盘内容不含已识别的敏感值，不承诺任意未知文本零泄漏；
- offline validation 拒绝所有非 localhost 请求；
- 报告只展示脱敏指标和本地路径。

自动隐私扫描的可证明范围是：已知输入秘密、URL query、Authorization/
Bearer/cookie/password/storage-state 等高置信凭据模式。系统不能可靠自动
识别任意 DOM 文本中的所有患者姓名、检查号或 accession，因此：

- 报告绝不内嵌 DOM 文本、Metadata 内容或截图；
- 这些原始产物整体标记为敏感本地数据；
- 可从运行时已知字段构造的患者标识加入本次敏感值 registry；
- 真实医院 smoke test 必须进行人工隐私复核；
- 未经证据不得声称“自动识别并脱敏所有患者文本”。

## 13. 输出目录

```text
out/{hospital}/runs/{run_id}/
├── source/
│   ├── processed_script_{hospital}.py
│   └── replica_annotations.json
├── adapter/
│   ├── completed_{hospital}.py
│   └── completed_{hospital}_offline.py
├── capture/
│   ├── instrumented_replay.py
│   ├── snapshots/
│   ├── manifest.json
│   └── locator_risk_report.json
├── replica/
│   ├── index.html
│   ├── states/
│   ├── documents/
│   ├── assets/
│   ├── serve_replica.py
│   └── replay_replica.py
├── validation/
│   ├── report.jpeg
│   ├── patient_info.json
│   ├── dicom_meta.json
│   ├── canvas_frames/
│   └── external_requests.json
├── logs/
│   ├── pipeline.jsonl
│   ├── adapter_generation.log
│   ├── live_capture.log
│   └── offline_validation.log
├── pipeline_state.json
├── pipeline_report.json
└── pipeline_report.html
```

`out/{hospital}/latest` 只保存指向最近一次成功 run 的小型元数据，不覆盖历史运行。

历史 `out/` 目录中的 `completed_*_vN.py`、`zscloud/`、`ftimage_runner/`
等实验产物不自动迁移、不删除，也不参与 `latest.json` 选择；新 pipeline
只管理 `out/{hospital}/runs/{run_id}/`。

## 14. 测试分层

### Level 1：纯单元测试

覆盖 marker、action plan、bootstrap plan、annotations hash、生成边界、状态机、错误分类、脱敏、artifact validator、报告和 timeout 配置。目标是不启动浏览器并在约 30 秒内完成。

### Level 2：本地 fixture 集成测试

基于现有 replica fixture 覆盖报告、popup、嵌套 iframe、序列、Metadata、WL/WW、canvas 和多状态转换。测试完整 orchestrator，不只测试单个 builder/runtime。

### Level 3：断网 E2E

构建 replica 后阻止所有非 localhost 请求，执行 completed offline adapter，验证 marker、状态和产物，要求外部请求数为零。

### Level 4：真实医院 smoke test

至少覆盖：

- popup viewer；
- 嵌套 iframe viewer；
- 当前主要目标 ftimage。

每个站点执行一次完整的：

```text
录制 -> 保存 -> 一键生成 -> live capture -> replica -> offline adapter -> report
```

真实站点测试不进入普通 CI，只保存脱敏指标。

### Level 5：匿名化回归矩阵

为已支持医院保存匿名化结构 fixture，持续验证 selector、frame topology、序列识别、Meta DOM、canvas 定位、marker 生成和 replica 兼容性。

## 15. 产品交互

GUI 新增单一主操作：

```text
生成 Adapter + 离线复刻
```

GUI 展示：

- 当前阶段；
- 当前 marker/action；
- 已用时间；
- success/partial/failed 数量；
- 登录继续与取消按钮；
-最终 adapter、replica 和报告入口。

GUI 不把子进程 exit code 直接映射成业务成功。最终状态来自 orchestrator 的验证报告。

## 16. 首版发布门槛

首版产品化闭环必须达到：

1. 单元测试全部通过；
2. 本地完整 fixture pipeline 通过；
3. 断网 offline adapter E2E 通过且外部请求为零，判定依据 §6.6.1 收缩后的 critical 集；
4. popup、嵌套 iframe 和 ftimage 三类真实 smoke test 至少各通过一次；其中 ftimage 因 canvas 动态帧为 unsupported 能力，只要其收缩后 critical 集（不含动态帧）全部通过即记为通过，未通过项明确记录为发布 blocker；此处「记为通过」指**发布门槛通过**，ftimage 的 `run.status` 仍按 §10.0 为 `partial`（收缩后 critical 空或仅 supported 能力），二者不冲突；
5. 取消、超时和失败不会残留浏览器/server；
6. 报告不泄漏凭据、token、cookie、storage state 和患者标识；
7. GUI 在所有阶段保持响应；
8. 失败原因可以归入稳定错误分类并提供下一步建议。

## 17. 设计决策摘要

- 采用完整产品化闭环，而非只串联按钮或只优化稳定性；
- 新增 orchestrator，不把编排继续堆进 `main_gui.py`；
- replica 捕获使用 processed script；
- offline adapter 验证 completed adapter；
- 保持真实 adapter、replica 和 offline bootstrap 三者边界；
- critical 使用严格成功门槛，但判定基于 replica 能力收缩后的 critical 集（§6.6.1），unsupported 能力不计入成功标准；
- 真实站点 smoke test 是发布门槛，不用本地 fixture 冒充真实验收；
- 第一版允许从稳定产物重新执行阶段，不恢复浏览器会话；
- run 级终态按 §10.0 优先级裁决（Failed→Partial→Success），空 critical 集判 partial；
- rerun 一律新建 run_id、引用复用产物，不从终态回退活动态；
- 离线隔离强化为进程级 egress 阻断，不只浏览器 route（§6.6）；
- 隐私、安全和外网隔离属于硬性门槛。
