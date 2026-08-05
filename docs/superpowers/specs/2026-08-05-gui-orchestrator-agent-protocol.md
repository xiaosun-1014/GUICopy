# Agent 子协议：`agent.py --emit-jsonl` 与 `event_sink`

日期：2026-08-05  
状态：已批准（D1）  
配套：`docs/superpowers/specs/2026-08-05-gui-orchestrator-event-protocol.md` §4 / §9  
上游：`docs/superpowers/specs/2026-08-04-one-recording-adapter-replica-pipeline-design.md` §6.2

## 1. 目的

让 `agent.py` 的 adapter 生成阶段输出真正的**结构化 JSONL 事件**，供
orchestrator 规范化后映射为 GUI 层的 `progress` / `marker_result`。禁止
orchestrator 解析 agent 的 stderr 人类日志（中文措辞/emoji/重试混杂/无法稳定
携带 marker UUID/无法区分「本次尝试失败」与「marker 最终失败」/日志改动会
意外破坏协议）。

本子协议只定义 **orchestrator ↔ agent** 这一段；agent 不伪装成 GUI↔orchestrator
顶层协议的生产者，它只输出自己的子进程事件，由 orchestrator 补统一 envelope。

## 2. 目标函数现状（已核对 agent.py）

```python
def process_script(script: str, dry_run: bool = False,
                   max_retries: int = 3, model: str = DEFAULT_MODEL) -> str:
```

CLI `main()` 现有参数：`input`、`-o/--output`、`--dry-run`、`--retry`（默认 3）、
`--model`、`--show-prompt`。

生成分支（在 `reversed(markers)` 循环内）：
- **跳过**：无对应 skill 目录的 marker（`"窗宽窗位 WL/WW"`、`"序列布局切换"`）
  直接 `continue`（agent.py:445-446）——需 `marker_skipped` 事件表达「未被处理」。
- **确定性**：`Meta 信息工具` / `影像画布交互` → `_generate_deterministic_*`
  后 `validate_syntax`（agent.py:448-468）——无 LLM 尝试，成功即 `marker_finished`。
- **LLM**：其余 marker → load bundle → 重试循环
  `for attempt in 1..max_retries` → `call_llm` → `extract_code_block` →
  加缩进 → 嵌入 `validate_syntax`；语法通过则 break（agent.py:470-527）。
  其中「序列选择」在 LLM 生成后再 `_wrap_sequence_state_waits` 并二次
  `validate_syntax`（agent.py:530-541），成功以包装后为准。

## 3. D1 兼容规则

### 3.1 默认行为完全不变

```powershell
agent.py input.py
```

保持现状：生成代码输出到 stdout、人类日志走 stderr。**不启用任何事件输出。**

### 3.2 `--emit-jsonl` 必须同时带 `--output`

```powershell
agent.py input.py --emit-jsonl            # 拒绝：stdout 同时承担代码与事件 → 冲突
agent.py input.py --output out.py --emit-jsonl   # 合法
```

参数约束（放 `main()` 里 `parse_args` 之后）：

```python
if args.emit_jsonl and not args.output:
    parser.error("--emit-jsonl requires --output")
```

### 3.3 开启后的通道定义

```text
stdout → 仅单行 JSONL 事件（每条 \n 结尾并 flush()）
stderr → 人工诊断日志
文件   → completed adapter（--output 指定）
```

stdout **不再**输出：生成代码正文、普通中文日志、prompt、traceback、保存路径
提示、空行、分隔线。

### 3.4 与现有参数的组合规则

- `--emit-jsonl` + `--dry-run`：合法。`dry_run` 不进入生成（`process_script`
  第 439-440 行提前 return 原脚本），因此只发 `agent_started` 与
  `agent_finished(status="dry_run")`（见 §4），不发 marker 级事件。
- `--emit-jsonl` + `--show-prompt`：**拒绝**（`--show-prompt` 会把 prompt 打到
  stdout，与「stdout 仅事件」冲突）。约束：
  ```python
  if args.emit_jsonl and args.show_prompt:
      parser.error("--emit-jsonl conflicts with --show-prompt")
  ```
- `--emit-jsonl` + `--retry N`：正常生效，`max_attempts` 反映 `N`。

## 4. Agent 子协议事件

agent 输出的每条事件**不得**包含 `version/ts/run_id/stage/source`（这些由
orchestrator 在规范化时统一填入）。agent 事件自带 `event` 以及必要的业务字段。
所有事件单行 JSON、`\n` 结尾、`flush()`。

### 4.0 marker 标识约定（不用 GUI UUID）

`agent.py` 内部**不产生 UUID**（`parse_markers` 只产出 `name/ts/indent/
line_start/line_end/raw`，无 GUI UUID 字段）。因此 agent 子协议里用
**`line`（源码 1 基行号）+ `label`** 标识 marker，事件里**不带 `marker_id`**。
GUI 的 `marker_id`（UUID）由 orchestrator 在引擎映射阶段按主规格 §6.3 的
「真实源代码行号 + 规范化 label」匹配后回填到 `progress` / `marker_result`，
agent 不需要也不应知道 UUID。任何把 `marker_id` 直接放入 agent 事件的设计
都不成立（agent 发不出来），以本节为准。

| 事件 | 含义 | 何时发出 |
|---|---|---|
| `agent_started` | adapter 生成开始 | 进入 `process_script` 后（必要时携带 `input_sha256`、`model`） |
| `marker_started` | 开始处理某 marker | 对每个被测 marker（含将被跳过的）在 `parse_markers` 后发出 |
| `marker_attempt` | 一次 LLM 尝试开始 | 每个 `attempt`（仅 LLM marker） |
| `marker_skipped` | marker 无 skill、被跳过 | 对无 skill 目录的 marker（agent.py:445-446） |
| `marker_finished` | 单 marker 成功完成 | 确定性成功 或 LLM 最终语法通过（序列选择以包装+二次验证通过为准） |
| `agent_failed` | 生成中止 | `RuntimeError`（LLM 调用失败 / 超过重试 / 确定性语法错） |
| `agent_finished` | 生成正常结束 | 全部 marker 处理完毕，返回已完成脚本 |

### 4.1 `agent_started`

```json
{ "event": "agent_started",
  "input_sha256": "64位哈希", "model": "gpt-4o",
  "marker_count": 3 }
```

### 4.2 `marker_started`

```json
{ "event": "marker_started",
  "label": "序列选择", "line": 42, "generator": "llm" }
```

`generator` 枚举：`llm` | `deterministic` | `skipped`（由是否在
`deterministic_generators` / `MARKER_MAP` 决定）。

### 4.3 `marker_attempt`（仅 LLM marker 的每次尝试）

```json
{ "event": "marker_attempt",
  "label": "序列选择", "line": 42,
  "attempt": 1, "max_attempts": 3,
  "prompt_sha256": "64位哈希" }
```

`prompt_sha256` 为本次实际使用 prompt（首次 = 基础 prompt；后续 = 基础 +
错误修正段）的 sha256，供报告追溯。

### 4.4 `marker_skipped`

```json
{ "event": "marker_skipped",
  "label": "窗宽窗位 WL/WW", "line": 60,
  "reason": "no_skill" }
```

### 4.5 `marker_finished`

```json
{ "event": "marker_finished",
  "label": "序列选择", "line": 42,
  "status": "success", "generator": "llm",
  "attempts": 2, "output_line_count": 219,
  "prompt_sha256": "64位哈希" }
```

确定性 marker 不携带 `attempts`/`prompt_sha256`：

```json
{ "event": "marker_finished",
  "label": "Meta 信息工具", "line": 88,
  "status": "success", "generator": "deterministic", "output_line_count": 30 }
```

- 只有该 marker 的补全**最终成功**（LLM 重试循环 break，或确定性语法通过，
  或序列选择包装后二次验证通过）才发 `marker_finished(status="success")`。
- 若某 marker 重试耗尽导致整个 `process_script` 抛出 → 不发
  `marker_finished`，由 `agent_failed` 表达中止（见 4.7）。

### 4.6 `agent_finished`

```json
{ "event": "agent_finished",
  "status": "success", "output_sha256": "64位哈希" }
```

### 4.7 `agent_failed`

不携带完整模型响应或 prompt（避免 token/大文本进事件流）：

```json
{ "event": "agent_failed",
  "error_category": "adapter_generation",
  "label": "序列选择", "line": 42,
  "status": "generated_code_syntax_invalid",
  "attempt": 2, "max_attempts": 3 }
```

`error_category` 取主规格 §8 稳定分类（`llm_configuration` /
`adapter_generation` 等）；`status` 用短稳定枚举（如
`llm_call_failed` / `generated_code_syntax_invalid` / `exceeded_retries` /
`deterministic_syntax_error`），**不**内嵌原始错误文本或 LLM 响应。

## 5. 实现：`event_sink`

给 `process_script` 增加可选 `event_sink`，默认无操作，使现有调用/测试零改动：

```python
def process_script(
    script: str,
    dry_run: bool = False,
    max_retries: int = 3,
    model: str = DEFAULT_MODEL,
    event_sink: Callable[[Dict[str, object]], None] | None = None,
) -> str:
    notify = event_sink or (lambda event: None)
```

`sink` 接收**不含**统一 envelope 的 agent 业务事件（§4 中的 `event` 对象）。
orchestrator 在子进程侧包一层 stdin 写入器即可消费；单元测试可直接传
`events.append`，不需要解析 stderr。

### 5.1 marker 处理逻辑埋点（对应 agent.py 现有分支）

- `marker_started`：对每个 marker（含将跳过的）在进入 `reversed(markers)`
  循环前发出。
- `marker_skipped`：`not skill_dir.exists(): continue` 分支（agent.py:445-446）。
- 确定性分支（448-468）：生成成功 → `marker_finished(generator="deterministic")`；
  语法错 → `notify(agent_failed(deterministic_syntax_error))` 后 `raise`。
- LLM 分支（470-527）：每次尝试前 `marker_attempt`；尝试内 `call_llm` 抛错 →
  `agent_failed(llm_call_failed)` 后 `raise`；语法通过 break → 对「序列选择」先
  做 `_wrap_sequence_state_waits` + 二次验证，成功才 `marker_finished`；
  重试耗尽 → `agent_failed(exceeded_retries)` 后 `raise`。
- 全部完成后 `agent_finished`。`dry_run` 时（439-440 return 前）只发
  `agent_started` 与 `agent_finished(status="dry_run")`。

## 6. CLI 接线

```python
parser.add_argument("--emit-jsonl", action="store_true",
                    help="以 JSONL 事件方式输出生成过程（需配合 --output）")
```

含约束（§3.2 / §3.4）。开启时构造 `emit` 回调，把 `event_sink` 传入
`process_script`，事件经 `print(json.dumps(ev, ensure_ascii=False)); sys.stdout.flush()`
写入 stdout；`--dry-run` 常规模态仍返回原脚本。

## 7. 验收清单（Level 1 单测覆盖点）

- 默认 CLI 行为完全不变：不传 `--emit-jsonl` 时 stdout 仍输出生成代码。
- `--emit-jsonl` 未带 `--output` → `parser.error`（退出码非 0）。
- `--emit-jsonl` + `--show-prompt` → `parser.error`。
- `--emit-jsonl --output x.py`：stdout 只含可 `json.loads` 的单行 JSON，无
  空行/分隔线/中文日志/traceback。
- `process_script(event_sink=events.append)`：事件序列含 `agent_started`
  → 各 marker（`marker_started`→[`marker_attempt`*]→`marker_finished` 或
  `marker_skipped`）→ `agent_finished`。
- 确定性 marker 无 `marker_attempt`；无 skill marker 发 `marker_skipped`。
- LLM 失败重试耗尽 → 事件流含 `agent_failed` 且不含该 marker 的
  `marker_finished`；`agent_failed` 不含完整模型响应/prompt。
- `--emit-jsonl --dry-run`：只 `agent_started` + `agent_finished(status="dry_run")`。
- 现有 `test_agent_marker_boundaries.py` 等不传 `event_sink`，回归通过。
