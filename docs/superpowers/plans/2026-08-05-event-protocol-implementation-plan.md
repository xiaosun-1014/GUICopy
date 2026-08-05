# Implementation Plan: GUI↔Orchestrator Event Protocol + Agent JSONL

Status: 已批准（D1–D4 已确认）；本文件是把已批准设计落为可执行代码的分步实现计划。
上游设计：
- `docs/superpowers/specs/2026-08-04-one-recording-adapter-replica-pipeline-design.md`
- `docs/superpowers/specs/2026-08-05-gui-orchestrator-event-protocol.md`
- `docs/superpowers/specs/2026-08-05-gui-orchestrator-agent-protocol.md`

## 全局约束（Global Constraints）

这些约束绑定每个任务，逐条原样引用到 review：

- **解释器硬性**：任何子进程 / 测试必须用 `D:/Anaconda/envs/codegen-marker/python.exe`
  （系统 Python 是 3.7，缺 PyQt6/playwright wheel；禁止静默回退 `sys.executable`）。
- **agent 子协议**（`2026-08-05-gui-orchestrator-agent-protocol.md`）：
  - 事件用 `line`（源码 1 基行号）+ `label` 标识 marker，**事件里不带 GUID UUID**
    （agent 无法产生 UUID；UUID 由 orchestrator 按 §6.3 行号+label 回填）。
  - 事件 `agent_started / marker_started / marker_attempt / marker_skipped /
    marker_finished / agent_finished / agent_failed`，单行 JSON、`\n` 结尾、`flush()`。
  - 失败事件不内嵌完整模型响应或 prompt；`reason` 用短稳定枚举
    （`llm_call_failed / generated_code_syntax_invalid / exceeded_retries /
     deterministic_syntax_error`）。
- **CLI 兼容**：默认 `agent.py input.py`（无 `--emit-jsonl`）行为**必须完全不变**；
  `--emit-jsonl` 必须同时带 `--output`，否则 `parser.error`；`--emit-jsonl` 与
  `--show-prompt` 互斥；`--emit-jsonl --dry-run` 只发 `agent_started` +
  `agent_finished(status="dry_run")`。
- **确定性 marker 不发 `marker_attempt`**；无 skill marker 发 `marker_skipped`；
  序列选择以「包装 + 二次验证」通过后才发 `marker_finished(success)`。
- 测试用 `unittest`，放在 `test/`，跑法 `D:/Anaconda/envs/codegen-marker/python.exe -m unittest discover -s test -v`。

---

## Task 1: agent.py 增加 event_sink + --emit-jsonl

**文件**：`agent.py`（改）、`test/test_agent_marker_boundaries.py`（改/增）。

**需求（来自 agent 子协议 §3–§7，已批准）**：

1. `process_script(script, dry_run=False, max_retries=3, model=DEFAULT_MODEL,
   event_sink: Callable[[dict[str, object]], None] | None = None) -> str`
   - 默认 `notify = event_sink or (lambda event: None)`，现有调用/测试零改动。
   - marker 标识用 `line`（`marker["line_start"]`）+ `label`（`marker["name"]`）。
2. 事件埋点（对照 `agent.py` 现有分支，行号为当前文件）：
   - `agent_started`：`process_script` 开头（含 `input_sha256`、`model`、`marker_count`）。
   - 每个 marker（含将跳过的）在进入 `reversed(markers)` 循环前发 `marker_started`
     `{label, line, generator}`；`generator`∈`llm|deterministic|skipped`。
   - 无 skill（agent.py:445-446 `not skill_dir.exists(): continue`）→ `marker_skipped`
     `{label, line, reason:"no_skill"}`。
   - 确定性分支（agent.py:448-468）成功 → `marker_finished {label, line, status:"success",
     generator:"deterministic", output_line_count}`；语法错 → `agent_failed{...deterministic_syntax_error}` 后 raise。
   - LLM 分支（agent.py:470-527）：每次尝试前 `marker_attempt {label, line, attempt,
     max_attempts, prompt_sha256}`；`call_llm` 抛错 → `agent_failed{llm_call_failed}`；语法
     通过 break → 序列选择先 `_wrap_sequence_state_waits`+二次验证，成功才
     `marker_finished{status:"success", generator:"llm", attempts, output_line_count, prompt_sha256}`；
     重试耗尽 → `agent_failed{exceeded_retries}`。
   - 全部完成 → `agent_finished {status:"success", output_sha256}`；`dry_run` 时
     （agent.py:439-440 return 前）只发 `agent_started` + `agent_finished{status:"dry_run"}`。
3. `main()` 增 `--emit-jsonl`（`action="store_true"`）：
   - `if args.emit_jsonl and not args.output: parser.error("--emit-jsonl requires --output")`
   - `if args.emit_jsonl and args.show_prompt: parser.error("--emit-jsonl conflicts with --show-prompt")`
   - 开启时构造 emit 回调：`print(json.dumps(ev, ensure_ascii=False)); sys.stdout.flush()`；
     stdout 只输出事件行；生成代码写入 `--output` 文件。
4. 测试（`test/test_agent_marker_boundaries.py` 新增用例，`event_sink` 传 `events.append`）：
   - 默认调用 `process_script(...)` 不传 sink，行为不变、无副作用。
   - `--emit-jsonl` 不带 `--output` → `parser.error`（`SystemExit` 码 2）。
   - `--emit-jsonl` + `--show-prompt` → `parser.error`。
   - 事件序列含 `agent_started → [marker_started → …] → agent_finished`；
     确定性 marker 无 `marker_attempt`；无 skill marker 发 `marker_skipped`。
   - LLM 重试耗尽（mock call_llm 抛错/返回无效）→ 事件流含 `agent_failed` 且该 marker
     无 `marker_finished`；`agent_failed` 不含完整响应/prompt。
   - `--dry-run`（传 `dry_run=True` + sink）→ 只 `agent_started` +
     `agent_finished{status:"dry_run"}`。
   - 回归：现有 agent 测试全部通过（默认无 `--emit-jsonl` 时 CLI 输出生成代码到 stdout）。

**验收**：现有 10 个 agent 测试 + 新增用例全绿；CLI 默认行为不变；stdout 事件单行 JSON。

---

## Task 2: 事件协议纯逻辑模块 + Level-1 单测

**文件**：`orchestrator_events.py`（新建）、`test/test_orchestrator_events.py`（新建）。

**需求（`2026-08-05-gui-orchestrator-event-protocol.md`）**：仓库当前没有
orchestrator 模块。本任务建一个**纯逻辑、可测试、无 Qt/浏览器/子进程**的事件
协议模块，把 ORchestrator 侧生成/规范化和 GUI 侧消费的核心规则固化，供后续
orchestrator 与实际 GUI 复用。设计决策（依据已批准的事件协议规格 §2–§8）：

**模块 `orchestrator_events.py` 提供（全部纯函数/纯类，无 I/O）**：
- `parse_envelope(line: str) -> dict | None`：单行 JSON 解析；非法 JSON / 非 dict 返回 None；未知 `event` 原样透传（前向兼容）。
- `normalize_child_event(child: dict, stage: str, run_id: str) -> dict`：payload 规范化转发——顶层 `event` 复制 child 名、`version/ts/stage/source/payload` 由 orchestrator 填；**保留终态名守卫**：child 的 `completed`/`fatal` 不得出现在顶层 `event`（须改名，如 `capture_completed`/`capture_failed`），否则返回 None（丢弃）或改名——模块内选用其一并说明。
- `ready_event(run_id: str) -> dict`：返回 `{"event":"ready","version":1,"run_id":...}`。
- `class MarkerTracker`：`upsert(marker_result: dict)`（按 `marker_id` 覆盖）、`counts() -> dict`（从最新 upsert 重算 success/partial/failed/skipped）、`overwrite(summary: dict)`（用 `summary` 计数覆盖，供权威覆盖）。
- `class TerminalGuard`：实施 D4 终态唯一——记录已发 `fatal`（最多一次）、`completed`（恰好一次）；`note(event_kind)` 校验规则：最多一条 `fatal`；恰好一条 `completed` 且必须为最末业务终态；`fatal` 之后只允 `summary`/`completed`；不允许先 `completed` 再 `fatal`；不允许两个 `completed`。违规时抛 `ValueError`。
- `redact(text: str, registry: Sequence[str]) -> str`：把 text 中命中 registry 的脱敏值替换（如 `[REDACTED]`）。

**验收（`test/test_orchestrator_events.py`，`unittest`，跑 `D:/Anaconda/envs/codegen-marker/python.exe -m unittest test.test_orchestrator_events -v`）**：
- ready 事件可被 `parse_envelope` 解析、字段正确；未知 `event` 种类透传不崩；非法行返回 None。
- `normalize_child_event`：child `capture_*`/`auth_*` 进 payload、顶层复制 event 名；child `completed`/`failed` 被改名/剥离，不出现在顶层 `event`。
- `MarkerTracker.upsert` + `counts`：同一 marker 从 success→partial 时，计数由 `success=1,partial=0` 正确更新为 `success=0,partial=1`；`overwrite(summary)` 覆盖计数。
- `TerminalGuard`：fatal 后必须且仅一次 completed（小于一次 → 违规、多于一次 → 违规）；两 completed 违规；completed 后再 fatal 违规；合法序列（`fatal→summary→completed`、`completed(success/partial)`、`completed(cancelled)`）通过。
- `redact`：text 含 registry 中的患者名/URL query/token 时被替换；registry 外的文本不被替换。
- 全部测试无 Qt/浏览器/网络依赖，运行毫秒级。

## Task 4: main_gui.py 复制子进程解释器硬校验

**文件**：`main_gui.py`（改）。

**需求（主规格 §6.1 + 全局约束）**：子进程（replica 导出等）必须用
`D:/Anaconda/envs/codegen-marker/python.exe`，**禁止静默回退**到 `sys.executable`
（系统 Python 3.7 缺 PyQt6/playwright wheel）。

现状：`main_gui.py:65-67` `replica_python_executable()`：
```python
def replica_python_executable() -> str:
    """Use the documented replica environment when available, with a local fallback."""
    return str(CODEGEN_MARKER_PYTHON if CODEGEN_MARKER_PYTHON.is_file() else Path(sys.executable))
```
当 `CODEGEN_MARKER_PYTHON`（`D:/Anaconda/envs/codegen-marker/python.exe`，第 57 行）不存在时会**静默回退** `sys.executable`——这正是 preflight 禁止的行为（不准用 system Python）。

改法：`replica_python_executable()` 不再静默回退；解释器文件不存在时以明确失败替代（如抛 `RuntimeError` 或返回错误状态供调用方 `_on_export_replica` 在启动子进程前中断并提示）。选择其一并在报告中说明；不要保留回退路径。

**验收**（须含单元测试，`unittest`，跑 `test/test_replica_gui.py`）：
- 解释器缺失时不再回退 `sys.executable`，而是触发明确失败/提示。
- 正常路径（解释器存在）行为不变。
- `test/test_replica_gui.py` 全部通过、无破坏。

## Task 5: batch_capture annotations UUID writeback

**文件**：`batch_capture_replicate.py`（改）、`test/test_batch_capture_replicate.py`（改）。

**需求（主规格 §6.3 + agent/annotations schema）**：Live capture 读取 GUI 的
`replica_annotations.json` 后，按「真实源代码行号 + 规范化 label」把 annotations
里的 **GUI UUID** 一一匹配并**写回** `ActionTarget.marker_id` / group /
snapshot 路径 / manifest / report（报告由 orchestrator 阶段读 manifest，故本任务
只需把它写进 manifest 的 ActionTarget 及 group 级）。缺失、重复、label 不一致
属于 **preflight 失败**，禁止只校验 source hash 后忽略 annotations 内容。

现状（已核对）：
- `replica_annotations.json`：`markers: [{marker_id: <UUID>, line: int, label: str}]`；
  `line` 是 GUI 里 marker 注释行的 1 基行号（与源码 marker 注释行对齐）。
- `validate_annotations(script_path, annotations_path)`（batch_capture 504-511）
  目前**只**校验 `schema_version == 1` + `source_script_sha256`，返回 payload，
  UUID 从未被消费。
- `parse_action_plan(source)`（rewrite_script）产生 `MarkerGroup(marker_id,
  marker_label, source_line)` 与 `ActionTarget(..., marker_id, ...)`，其中
  `marker_id` 是重新生成的 `m_{index:03d}`。
- `build_flow_from_snapshots`（437-476）用 `marker_labels = {group.marker_id:
  group.marker_label}`，从不使用 annotations 的 GUI UUID。

**实现要点**：
- 在 `validate_annotations` 后新增一个把 annotations UUID 合并进 marker group /
  ActionTarget 的步骤：构造 `{"line": "规范化label": uuid}` 索引，按
  `MarkerGroup.source_line` + 规范化 `marker_label` 匹配（规范化为去除空白/
  统一大小写——采用与主规格 §6.3「规范化 label」一致的规则，报告中说明该规则）。
  缺失（annotations 有 UUID 但脚本无对应 group）、重复（同 line+label 多 UUID）、
  label 不一致（line 对齐但 label 文本不同）→ 抛 `ValueError`（preflight 失败）。
- 把匹配到的 GUI UUID 写回 ActionTarget 的 `marker_id`（及 group 级标记），使其
  进入 manifest 的 ActionTarget，供报告消费。snapshot 路径（`snapshots/<action_id>`
  目录名）**不强制改名**，但报告须能从 manifest UUID 关联到 action。
- `capture_and_build`（495-501）或 `build_flow_from_snapshots` 需接入该合并步骤
  （annotations payload 从上游传入）。`main()` 的 live 分支（585-586）已在调用
  `validate_annotations`，让其在校验后把合并结果传给构建链路。

**验收**（`unittest`，跑 `test/test_batch_capture_replicate.py`，必须全绿）：
- `validate_annotations` 保留现有 hash/schema 校验；新增：能返回/产出「annotations
  UUID 与 marker group 的映射」。
- 匹配正确：line + 规范化 label 命中时，对应 ActionTarget.marker_id = 该 GUI UUID。
- 缺失 / 重复 / label 不一致 → 抛错（preflight 失败）。
- 不传 annotations 或只有 hash 校验时，行为退回现状（不破坏现有路径）。
- 现有 17 个 `test_batch_capture_replicate` 全部通过、无破坏。
