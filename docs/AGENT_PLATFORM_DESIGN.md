# Agent 平台设计方案

> 目标：将现有"录制脚本 + marker + skill + LLM 补全"流程升级为**可自动验证、可迭代、可回归**的 Agent 平台。

---

## 1. 现状诊断

### 1.1 当前架构

```
GUI 录制 → processed_script.py（含 marker）
    ↓
agent.py（LLM 补全）  或  auto_gen.py（规则补全）
    ↓
completed.py / auto_capture.py
```

### 1.2 核心问题

| # | 问题 | 根因 | 影响 |
|---|------|------|------|
| 1 | MARKER_MAP 映射断裂 | agent.py 中映射到不存在的 skill 目录（marker-window-level、marker-layout-switch、marker-canvas-interact） | LLM 补全跳过这些 marker |
| 2 | 验证只有 ast.parse | 无运行时校验、无产物校验 | 生成的代码能过语法但跑不通 |
| 3 | LLM 生成不稳定 | prompt 过长、few-shot 不足、无结构化约束 | 每次生成结果差异大，需人工审查 |
| 4 | 两条生成链路割裂 | agent.py（LLM）与 auto_gen.py（规则）各自独立，无统一调度 | 维护成本翻倍 |
| 5 | 无回归机制 | 改了 skill 不知道是否影响其他医院 | 修一个坏另一个 |
| 6 | 反馈不闭环 | 失败需人工定位、手改 completed.py | 知识不沉淀 |

### 1.3 现有可复用资产

| 资产 | 位置 | 状态 |
|------|------|------|
| 确定性画布截图 | `skills/_shared/canvas_capture.py` | ✅ 可直接用 |
| 确定性 Meta 提取 | `skills/_shared/meta_extract.py` | ✅ 可直接用 |
| Meta 校验 | `skills/_shared/meta_validate.py` | ✅ 可直接用 |
| 规则补全引擎 | `auto_gen.py` | ✅ 覆盖 80% 场景 |
| LLM 补全引擎 | `agent.py` | ⚠️ 需重构 |
| Skill 知识库 | `skills/marker-*/` | ✅ 内容可复用 |
| Viewer 配置 | `skills/_shared/viewers.yaml` | ✅ 可直接用 |

---

## 2. 目标架构

```
┌─────────────────────────────────────────────────────────────────┐
│                        Agent Platform                            │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌───────────┐    ┌───────────┐    ┌───────────┐    ┌────────┐│
│  │  Planner  │───▶│ Executor  │───▶│ Validator │───▶│Learner ││
│  └───────────┘    └───────────┘    └───────────┘    └────────┘│
│       │                │                │                │     │
│       ▼                ▼                ▼                ▼     │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │              Skill Registry (统一注册表)                  │   │
│  └─────────────────────────────────────────────────────────┘   │
│       │                │                │                │     │
│       ▼                ▼                ▼                ▼     │
│  ┌─────────┐    ┌──────────┐    ┌──────────┐    ┌─────────┐  │
│  │viewers  │    │ _shared/ │    │ LLM API  │    │ cases/  │  │
│  │.yaml    │    │ modules  │    │ provider │    │ 回归集  │  │
│  └─────────┘    └──────────┘    └──────────┘    └─────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

### 2.1 四层职责

| 层 | 职责 | 输入 | 输出 |
|----|------|------|------|
| **Planner** | 解析 marker、选择执行策略（规则/LLM/混合） | processed_script.py | 执行计划（Plan） |
| **Executor** | 按计划生成代码片段 | Plan + skill bundle | 代码 patch 列表 |
| **Validator** | 四级验证（静态/单元/回放/产物） | completed script + 期望产物 | 验证报告 |
| **Learner** | 失败归因、case 归档、skill 自动建议 | 验证报告 + 运行日志 | 更新建议 |

---

## 3. 模块详细设计

### 3.1 Skill Registry（技能注册表）

**替代现有 `agent.py` 中的硬编码 `MARKER_MAP`。**

```
platform/
├── registry.py          # 注册表加载器
└── skill_manifest.json  # 全局清单
```

#### skill_manifest.json 格式

```json
{
  "skills": [
    {
      "id": "report-screenshot",
      "marker_patterns": ["报告截图"],
      "skill_dir": "skills/marker-report-screenshot",
      "executor": "deterministic",
      "module": null,
      "function": null,
      "template": "report_screenshot.py.jinja",
      "keep_original": false,
      "prerequisites": [],
      "validator": "file_exists",
      "expected_outputs": ["report.jpeg"],
      "timeout_ms": 15000,
      "retry": 2
    },
    {
      "id": "sequence-select",
      "marker_patterns": ["序列选择"],
      "skill_dir": "skills/marker-sequence-select",
      "executor": "deterministic+llm_fallback",
      "module": "skills._shared.sequence_select",
      "function": "select_series",
      "template": null,
      "keep_original": false,
      "prerequisites": [],
      "validator": "series_selected",
      "expected_outputs": [],
      "timeout_ms": 30000,
      "retry": 3
    },
    {
      "id": "canvas-capture",
      "marker_patterns": ["影像画布交互"],
      "skill_dir": "skills/marker-canvas-capture",
      "executor": "deterministic",
      "module": "skills._shared.canvas_capture",
      "function": "capture_canvas_interaction",
      "template": null,
      "keep_original": false,
      "prerequisites": ["sequence-select"],
      "validator": "frames_captured",
      "expected_outputs": ["canvas_frames/"],
      "timeout_ms": 600000,
      "retry": 1
    },
    {
      "id": "meta-extract",
      "marker_patterns": ["Meta 信息工具"],
      "skill_dir": "skills/marker-meta-extract",
      "executor": "deterministic",
      "module": "skills._shared.meta_extract",
      "function": "extract_meta_from_frame",
      "template": null,
      "keep_original": false,
      "prerequisites": [],
      "validator": "json_schema",
      "expected_outputs": ["dicom_meta.json"],
      "timeout_ms": 30000,
      "retry": 2
    },
    {
      "id": "layout-switch",
      "marker_patterns": ["序列布局切换"],
      "skill_dir": null,
      "executor": "keep_original",
      "module": null,
      "function": null,
      "template": null,
      "keep_original": true,
      "prerequisites": [],
      "validator": "noop",
      "expected_outputs": [],
      "timeout_ms": 5000,
      "retry": 0
    },
    {
      "id": "window-level",
      "marker_patterns": ["窗宽窗位 WL/WW"],
      "skill_dir": null,
      "executor": "keep_original",
      "module": null,
      "function": null,
      "template": null,
      "keep_original": true,
      "prerequisites": [],
      "validator": "noop",
      "expected_outputs": [],
      "timeout_ms": 5000,
      "retry": 0
    }
  ]
}
```

#### registry.py 接口

```python
class SkillRegistry:
    def __init__(self, manifest_path: Path): ...
    def match(self, marker_name: str) -> SkillEntry | None: ...
    def resolve_prerequisites(self, entries: list[SkillEntry]) -> list[SkillEntry]: ...
    def get_executor(self, entry: SkillEntry) -> BaseExecutor: ...
    def get_validator(self, entry: SkillEntry) -> BaseValidator: ...
```

---

### 3.2 Planner（规划器）

**职责**：解析 processed_script → 识别所有 marker → 查注册表 → 排列执行顺序 → 生成 Plan。

```python
@dataclass
class PlanStep:
    marker: MarkerInfo          # 原始 marker 信息（行号、上下文等）
    skill: SkillEntry           # 注册表匹配结果
    strategy: Literal["deterministic", "llm", "keep_original"]
    params: dict                # 从录制脚本自动提取的参数

@dataclass
class Plan:
    steps: list[PlanStep]
    script_context: ScriptContext  # URL、page变量、iframe路径等全局上下文
```

**自动提取参数**（复用 auto_gen.py 逻辑）：

```python
@dataclass
class ScriptContext:
    url: str
    iframe_selectors: list[str]
    page_var: str               # "page" | "page1"
    viewport: tuple[int, int]
    protocol_name: str
    frame_count: int | None
    canvas_coords: tuple[int, int]
    dicom_button: str
```

---

### 3.3 Executor（执行器）

三种执行器，按 skill 注册表配置选择：

#### 3.3.1 DeterministicExecutor（规则执行器）

直接调用 `skills/_shared/` 中的函数，用 Jinja2 模板生成代码片段。

```python
class DeterministicExecutor(BaseExecutor):
    def execute(self, step: PlanStep, context: ScriptContext) -> CodePatch:
        """生成结构化 patch，而非自由文本。"""
        if step.skill.template:
            code = render_template(step.skill.template, context, step.params)
        elif step.skill.module and step.skill.function:
            code = generate_call_code(step.skill.module, step.skill.function, step.params)
        return CodePatch(
            start_line=step.marker.line_start,
            end_line=step.marker.line_end,
            replacement=code,
        )
```

#### 3.3.2 LLMExecutor（LLM 执行器）

仅在规则执行器失败或未覆盖时触发。

改进点（相比现有 agent.py）：
- **结构化输出**：要求 LLM 返回 JSON `{"code": "...", "imports": [...], "explanation": "..."}`
- **约束 prompt**：只给 marker 前后 10 行上下文 + skill 核心指令，不倾倒整个 bundle
- **Few-shot 精选**：每个 skill 保留 2-3 个黄金样本（input → output 对）
- **温度 0**：确定性优先

```python
class LLMExecutor(BaseExecutor):
    def execute(self, step: PlanStep, context: ScriptContext) -> CodePatch:
        prompt = self._build_constrained_prompt(step, context)
        response = call_llm(prompt, response_format="json")
        parsed = json.loads(response)
        return CodePatch(
            start_line=step.marker.line_start,
            end_line=step.marker.line_end,
            replacement=parsed["code"],
            imports=parsed.get("imports", []),
        )
```

#### 3.3.3 KeepOriginalExecutor（保留执行器）

只添加 marker 注释标签，不替换代码。

```python
class KeepOriginalExecutor(BaseExecutor):
    def execute(self, step: PlanStep, context: ScriptContext) -> CodePatch:
        return CodePatch(
            start_line=step.marker.line_start,
            end_line=step.marker.line_end,
            replacement=step.marker.raw,  # 原样保留
        )
```

---

### 3.4 Validator（验证器）

四级验证，逐级递进：

```
Level 1: Static     — 语法、导入、路径、命名
Level 2: Unit       — skills/_shared 函数级测试
Level 3: Replay     — Playwright dry-run（headless 回放前 N 步）
Level 4: Artifact   — 产物存在性、schema、质量阈值
```

#### 验证器接口

```python
class BaseValidator(ABC):
    @abstractmethod
    def validate(self, completed_script: str, output_dir: Path,
                 expected: list[str]) -> ValidationResult: ...

@dataclass
class ValidationResult:
    level: int                  # 1-4
    passed: bool
    errors: list[str]
    warnings: list[str]
    artifacts_found: list[str]
    duration_ms: int
```

#### 内置验证器

| 验证器 ID | Level | 检查内容 |
|-----------|-------|---------|
| `syntax` | 1 | ast.parse + import 可达性 |
| `path_check` | 1 | SCRIPT_DIR 路径引用正确性 |
| `file_exists` | 4 | 指定文件存在且 > 1KB |
| `json_schema` | 4 | JSON 产物符合 schema（tag/desc/value 结构） |
| `frames_captured` | 4 | canvas_frames/ 下 ≥ 10 张 JPEG，文件大小不全相同 |
| `series_selected` | 3 | 回放到序列选择步骤，验证 dblclick 被调用 |
| `noop` | 1 | 永远通过（用于 keep_original） |

#### 回放验证（Level 3）

```python
class ReplayValidator(BaseValidator):
    """Playwright headless 模式运行 completed script 的前 N 步。

    不访问真实 URL，用 route() mock 网络请求，
    只验证脚本结构不报错、关键 API 调用顺序正确。
    """
    def validate(self, completed_script: str, output_dir: Path, expected: list[str]):
        # 1. 插入 mock 层（route 拦截所有请求返回空页面）
        # 2. 运行脚本（subprocess，timeout 30s）
        # 3. 检查 exit code + stderr
        # 4. 检查关键日志输出（[截图] [序列选择] [画布] [Meta] 等）
        ...
```

---

### 3.5 Learner（学习器）

失败时自动归档 case，产出可操作建议。

```
cases/
├── 2026-07-02_uicloud_sequence-select_fail/
│   ├── context.json        # 失败上下文（URL、marker、上下文代码）
│   ├── generated_code.py   # 生成的代码
│   ├── validation.json     # 验证报告
│   ├── screenshot.jpeg     # 页面快照（如有）
│   └── fix_applied.py      # 人工修复后的代码（用于后续 few-shot）
```

#### 自动建议

```python
class Learner:
    def analyze_failure(self, case: FailureCase) -> list[Suggestion]:
        """分析失败模式，生成建议。"""
        suggestions = []

        # 模式1：选择器不存在 → 建议更新 viewers.yaml
        if "locator" in case.error and "timeout" in case.error:
            suggestions.append(Suggestion(
                type="update_viewer_config",
                target="skills/_shared/viewers.yaml",
                detail="选择器超时，可能需要适配新 viewer DOM 结构",
            ))

        # 模式2：帧数为 0 → 建议检查翻页策略
        if "0 帧" in case.log or "stall" in case.log:
            suggestions.append(Suggestion(
                type="update_skill",
                target="skills/marker-canvas-capture/references/navigation_debug.md",
                detail="翻页未生效，检查 canvas 聚焦和翻页策略优先级",
            ))

        # 模式3：LLM 生成代码语法错误 → 建议加 few-shot
        if case.validation.level == 1 and "SyntaxError" in case.validation.errors[0]:
            suggestions.append(Suggestion(
                type="add_few_shot",
                target=f"skills/{case.skill_id}/test_data/",
                detail="LLM 生成语法错误，需要增加正确样本",
            ))

        return suggestions
```

---

## 4. 回归系统

### 4.1 回归数据集

```
regression/
├── dataset.json            # 回归清单
├── uicloud/
│   ├── input.py            # processed_script
│   ├── expected_outputs/   # 期望产物指纹
│   │   ├── report.jpeg.sha256
│   │   ├── dicom_meta.json.schema
│   │   └── canvas_frames.count  # ">=50"
│   └── golden_completed.py # 参考正确输出（可选）
├── cxhospital/
│   └── ...
└── zscloud/
    └── ...
```

#### dataset.json

```json
{
  "cases": [
    {
      "id": "uicloud-standard",
      "hospital": "uicloud",
      "input": "regression/uicloud/input.py",
      "expected_markers": ["报告截图", "序列选择", "序列布局切换", "窗宽窗位 WL/WW", "影像画布交互", "Meta 信息工具"],
      "validators": ["syntax", "path_check", "json_schema", "frames_captured"],
      "tags": ["popup", "single-iframe", "standard"]
    },
    {
      "id": "cxhospital-nested",
      "hospital": "cxhospital",
      "input": "regression/cxhospital/input.py",
      "expected_markers": ["报告截图", "序列选择", "影像画布交互", "Meta 信息工具"],
      "validators": ["syntax", "path_check", "json_schema"],
      "tags": ["nested-iframe", "no-frame-count", "cornerstone"]
    }
  ]
}
```

### 4.2 回归命令

```bash
# 跑全部回归
python -m platform.regression run --all

# 跑单个医院
python -m platform.regression run --case uicloud-standard

# 查看报告
python -m platform.regression report --format table
```

### 4.3 回归报告

```
╔══════════════════════════════════════════════════════════════╗
║                    回归报告 2026-07-02                        ║
╠══════════════════╦═══════╦═══════╦═══════╦══════════════════╣
║ Case             ║ L1    ║ L2    ║ L3    ║ L4               ║
╠══════════════════╬═══════╬═══════╬═══════╬══════════════════╣
║ uicloud-standard ║ ✅    ║ ✅    ║ ✅    ║ ✅ (4/4 产物)    ║
║ cxhospital       ║ ✅    ║ ✅    ║ ⚠️    ║ ❌ (0/3 产物)    ║
╚══════════════════╩═══════╩═══════╩═══════╩══════════════════╝

失败详情:
  cxhospital L3: 序列选择 dblclick 未执行（DOM 未渲染）
  cxhospital L4: canvas_frames/ 目录为空
```

---

## 5. 文件结构

新增/改动文件一览：

```
22_dicom_model_service/
├── platform/                       # 新增：平台核心
│   ├── __init__.py
│   ├── registry.py                 # Skill 注册表
│   ├── planner.py                  # 规划器
│   ├── executor/
│   │   ├── __init__.py
│   │   ├── base.py                 # BaseExecutor ABC
│   │   ├── deterministic.py        # 规则执行器
│   │   ├── llm.py                  # LLM 执行器
│   │   └── keep_original.py        # 保留执行器
│   ├── validator/
│   │   ├── __init__.py
│   │   ├── base.py                 # BaseValidator ABC
│   │   ├── static.py              # L1 静态检查
│   │   ├── unit.py                # L2 单元测试
│   │   ├── replay.py             # L3 回放验证
│   │   └── artifact.py           # L4 产物验证
│   ├── learner.py                  # 学习器
│   ├── regression.py               # 回归跑批入口
│   ├── models.py                   # 数据模型（Plan, PlanStep, CodePatch 等）
│   └── cli.py                      # 统一 CLI 入口
├── platform_config/
│   ├── skill_manifest.json         # 技能注册清单
│   └── templates/                  # Jinja2 代码模板
│       ├── report_screenshot.py.jinja
│       ├── canvas_capture.py.jinja
│       └── meta_extract.py.jinja
├── regression/                     # 回归数据集
│   ├── dataset.json
│   ├── uicloud/
│   └── cxhospital/
├── cases/                          # 失败 case 归档（git ignore）
├── docs/
│   └── AGENT_PLATFORM_DESIGN.md   # 本文档
├── agent.py                        # 保留，但内部改为调用 platform/
└── auto_gen.py                     # 保留，但内部改为调用 platform/
```

---

## 6. 迁移计划

### Phase 1：基础设施（第 1 周）

| 任务 | 产出 | 验收标准 |
|------|------|---------|
| 创建 `platform/` 包骨架 | 目录 + `__init__.py` | import 不报错 |
| 实现 `registry.py` + `skill_manifest.json` | 注册表 | `registry.match("报告截图")` 返回正确 entry |
| 实现 `planner.py` | Plan 生成 | 给 processed_script → 输出正确 Plan |
| 迁移 `auto_gen.py` 的提取逻辑到 `planner.py` | ScriptContext | 所有现有 extract_* 函数测试通过 |
| 修复 agent.py 的 MARKER_MAP | 映射正确 | 所有 marker 能匹配到 skill 或 keep_original |

### Phase 2：执行与验证（第 2 周）

| 任务 | 产出 | 验收标准 |
|------|------|---------|
| 实现 DeterministicExecutor | 规则生成 | uicloud case 能生成可执行脚本 |
| 实现 LLMExecutor（结构化输出） | LLM fallback | 失败时自动降级到 LLM |
| 实现 L1 StaticValidator | 语法+路径 | 捕获当前已知的路径错误 |
| 实现 L4 ArtifactValidator | 产物检查 | 检测 report.jpeg / dicom_meta.json 存在 |
| 创建 regression/ 数据集 | 2 个 case | `python -m platform.regression run --all` 可执行 |
| 统一 CLI | `python -m platform generate` | 替代直接调 agent.py / auto_gen.py |

### Phase 3：闭环与学习（第 3 周）

| 任务 | 产出 | 验收标准 |
|------|------|---------|
| 实现 L3 ReplayValidator | mock 回放 | headless 运行不报错 |
| 实现 Learner | 失败归档 + 建议 | 失败自动写入 cases/ |
| Jinja2 模板化 | 3 个模板 | 规则生成不再拼字符串 |
| 接入 CI（可选） | GitHub Actions / 本地 bat | push 自动跑回归 |
| 文档完善 | README 更新 | 新人 10 分钟能跑通 |

---

## 7. 统一 CLI 设计

```bash
# 从 processed_script 生成 auto_capture（取代 auto_gen.py）
python -m platform generate \
    --input out/uicloud/processed_script.py \
    --output out/uicloud/auto_capture_uicloud.py

# 从 processed_script 生成 completed（取代 agent.py）
python -m platform complete \
    --input out/uicloud/processed_script.py \
    --output out/uicloud/completed.py \
    --strategy deterministic  # 或 llm, hybrid

# 验证已生成的脚本
python -m platform validate \
    --script out/uicloud/auto_capture_uicloud.py \
    --level 4 \
    --output-dir out/uicloud/

# 回归跑批
python -m platform regression run --all --report table

# 查看某个 case 的失败详情
python -m platform regression detail --case cxhospital-nested

# 分析失败并生成修复建议
python -m platform learn --case-dir cases/2026-07-02_cxhospital_*/
```

---

## 8. 关键设计决策

### 8.1 规则优先，LLM 兜底

```
Marker 到达
  → 查注册表 → executor = ?
      ├─ "keep_original" → 直接保留，不生成
      ├─ "deterministic" → 调共享模块/模板
      │     ├─ 成功 → 验证 → 完成
      │     └─ 失败 → 降级到 LLM
      └─ "llm" → 直接 LLM 生成
            ├─ 成功 → 验证 → 完成
            └─ 失败 → 记录 case → 人工介入
```

**原因**：
- 确定性执行 100% 可复现，不受 API 波动影响
- LLM 适合处理"未见过的 viewer 布局"等长尾场景
- 随着 case 积累，越来越多场景可从 LLM 毕业为规则

### 8.2 结构化 Patch 而非自由替换

```python
@dataclass
class CodePatch:
    start_line: int         # 替换起始行（1-indexed）
    end_line: int           # 替换结束行（inclusive）
    replacement: str        # 替换代码
    imports: list[str]      # 需要添加的 import（自动去重插入文件头）
    post_wait_ms: int = 0   # 替换后追加的 wait_for_timeout
```

**原因**：
- 精确控制替换范围，不会误改上下文
- import 自动管理，避免重复或遗漏
- post_wait 统一管理，不散落在各处

### 8.3 验证驱动开发

每次生成后必须跑验证。验证不通过 → 不输出文件 → 记录 case。

```python
def generate_and_validate(input_path, output_path, level=4):
    plan = planner.plan(input_path)
    patches = executor.execute_all(plan)
    completed = assembler.apply_patches(plan.script, patches)

    result = validator.validate(completed, level=level)
    if result.passed:
        Path(output_path).write_text(completed)
        print(f"✅ 生成成功: {output_path}")
    else:
        case = learner.archive(plan, patches, result)
        print(f"❌ 验证失败: {result.errors}")
        print(f"   Case 已归档: {case.path}")
        suggestions = learner.analyze_failure(case)
        for s in suggestions:
            print(f"   建议: {s.detail}")
```

### 8.4 Viewer 适配通过配置而非代码

新医院接入流程：
1. 录制 processed_script
2. 运行 `python -m platform generate` → 自动提取 viewer 特征
3. 如果失败 → 在 `viewers.yaml` 中添加配置项
4. 重新生成 → 验证通过 → 加入回归数据集

**不需要写新代码**，只需要配置。

---

## 9. 与现有代码的兼容性

| 现有入口 | 迁移方式 | 过渡期行为 |
|---------|---------|-----------|
| `agent.py` | 内部改为调用 `platform.cli.complete()` | 保留 CLI 接口不变 |
| `auto_gen.py` | 内部改为调用 `platform.cli.generate()` | 保留 CLI 接口不变 |
| `main_gui.py` | 不变 | GUI 录制流程完全不受影响 |
| `skills/` | 不变 | 注册表引用现有目录 |
| `skills/_shared/` | 不变 | 执行器直接 import |
| `out/` | 不变 | 产出物目录结构不变 |

---

## 10. 成功指标

| 指标 | 当前 | Phase 1 后 | Phase 3 后 |
|------|------|-----------|-----------|
| 生成成功率（语法通过） | ~70% | 95% | 99% |
| 生成成功率（可运行） | ~30% | 70% | 90% |
| 新医院接入时间 | 2-4h 手工 | 30min 配置 | 10min 配置 |
| 回归覆盖 | 0 | 2 case | 5+ case |
| 人工介入频率 | 每次 | 失败时 | 罕见 |

---

## 11. 与 F:\19_playWrightReader Agent 平台集成

### 11.1 两个项目的定位差异

| 维度 | 22_dicom_model_service（本项目） | 19_playWrightReader |
|------|--------------------------------|---------------------|
| 核心能力 | Codegen 录制 → marker → 确定性补全 | LangGraph agent → MCP Playwright → 在线探索 |
| 强项 | 精确的 DOM 选择器、帧级画布截图、DICOM meta 提取 | 动态决策、未知页面探索、VL 视觉确认 |
| 弱项 | 遇到未知 viewer 结构需手工适配 | 探索慢（200 轮迭代）、重复操作浪费 token |
| 产物 | `auto_capture.py`（确定性回放脚本） | `skills/{domain}.md` + `replays/replay_{domain}.py` |
| 适用阶段 | 已知 viewer → 批量病人回放 | 首次接触 viewer → 学会操作 |

### 11.2 核心洞察：你的想法是对的

把本项目的 **AGENTS.md + 录制脚本** 作为 19 平台的输入文档，可以：

1. **跳过探索阶段**：录制脚本已经包含了完整的操作路径（点哪个按钮、iframe 嵌套结构、选择器），agent 不需要从零探索
2. **消除 DOM 猜测**：录制脚本里的 `locator("[id=\"2d-iframe\"]").content_frame.get_by_role(...)` 就是精确答案，agent 不用反复 snapshot + evaluate 试错
3. **参数化**：录制脚本是一个具体病人的操作，但 DOM 结构对同医院所有病人通用，只有 URL 和密码变化

### 11.3 集成方案：录制脚本 → 指引文档 → Agent 回放

```
┌───────────────────────────────────────────────────────────────┐
│                    集成工作流                                    │
├───────────────────────────────────────────────────────────────┤
│                                                               │
│  ① 人工录制（本项目 GUI）                                      │
│     → processed_script.py + AGENTS.md                         │
│                                                               │
│  ② 自动转换（新增 script_to_guide.py）                         │
│     → input/{hospital}.md（19 平台格式的指引文档）              │
│     → 包含精确选择器、iframe 路径、参数化占位                    │
│                                                               │
│  ③ Agent 执行（19 平台）                                       │
│     python main.py explore --url {{新病人URL}} \               │
│         --doc input/{hospital}.md                              │
│     → 有精确指引，探索轮次从 ~200 降到 ~20                      │
│                                                               │
│  ④ 验证 + 产物                                                 │
│     → skills/{hospital}.md（操作规程，含 dynamic 分类）          │
│     → replays/replay_{hospital}.py（确定性回放脚本）            │
│                                                               │
│  ⑤ 批量回放（无需 agent）                                      │
│     python replays/replay_{hospital}.py --url {{病人N}}         │
│                                                               │
└───────────────────────────────────────────────────────────────┘
```

### 11.4 关键模块：script_to_guide.py

把录制的 `processed_script.py` 转换成 19 平台能消费的指引文档格式：

```python
"""script_to_guide.py — 录制脚本 → 19 平台指引文档

将 processed_script.py 中的操作步骤和 marker 转换为
F:\19_playWrightReader\input\{hospital}.md 格式的指引文档。

核心转换规则：
- page.goto(url)         → ### 1. 打开链接
- expect_popup           → ### 2. 进入影像查看（记录 popup 处理方式）
- [MARKER: 序列选择]    → ### 3. 选择序列（动态判断）+ 精确选择器提示
- [MARKER: 序列布局切换] → ### 4. 调整布局 + 精确按钮名称
- [MARKER: 窗宽窗位]    → ### 5. 调整窗宽窗位 + 精确控件 ID
- [MARKER: 影像画布交互] → ### 6. 翻页截图 + canvas 选择器 + 帧数
- [MARKER: Meta 信息]   → ### 7. 提取 DICOM 信息 + 按钮名称
"""

# 输出格式示例（给 19 平台的 doc_parser.py 消费）：

TEMPLATE = '''# {hospital} 云胶片操作指引

## 参数
- {{{{url}}}} — 病人分享链接
- {{{{password}}}} — 密码（可选）
- {{{{output_dir}}}} — 截图保存目录

## 已知 DOM 结构
> 以下信息来自录制脚本，agent 可直接使用，无需探索。

- iframe 路径: {iframe_path}
- 页面变量: {page_var}（{page_var_explain}）
- canvas 选择器: {canvas_selector}
- 序列文本样例: "{protocol_sample}"

## 步骤

### 1. 打开链接
- 访问 {{{{url}}}}
- 等待页面加载完成
{password_section}

### 2. 进入影像查看
{enter_viewer_section}

### 3. 选择序列（动态判断）
- **精确方法**: 在 `{iframe_path}` 内查找序列列表
- 选择规则:
  - 优先选帧数最多的序列（关键词: AIIR/Lung/MPR/薄层）
  - 双击选中（不是单击）
- **录制参考**: `{sequence_selector}`
- 如果找不到匹配序列，选列表中帧数最多的

### 4. 调整布局
- 点击「{layout_button}」按钮
- 选择 1×1 布局（「{layout_option}」）

### 5. 调整窗宽窗位
- canvas 位置: `{canvas_selector}`
- WL 输入框: `{wl_selector}` → 填入目标值
- WW 输入框: `{ww_selector}` → 填入目标值
- 每次填入后按 Enter 确认

### 6. 翻页截图
- canvas: `{canvas_selector}`
- 已知总帧数: {total_frames}（来自序列名解析）
- 翻页方式: 键盘 ArrowDown（canvas 需先聚焦）
- 每帧保存到 {{{{output_dir}}}}/frame_XXXX.jpeg

### 7. 提取 DICOM 信息
- 打开方式: 点击「{dicom_button}」
- 面板格式: table 行（tag | desc | value）
- 提取后关闭面板

## 异常处理
- 序列加载超时 → 等待 15 秒后重试
- canvas 翻页无响应 → 先点击 canvas 聚焦，再翻页
- DICOM 面板为空 → 截图保存当前状态
'''
```

### 11.5 为什么这能解决"探索缓慢"

19 平台当前的问题根源：

| 问题 | 原因 | 录制脚本如何解决 |
|------|------|----------------|
| 找不到按钮 → snapshot → evaluate → 重试循环 | 不知道 DOM 结构 | 录制脚本直接给出 `get_by_role("button", name="序列布局")` |
| iframe 内操作失败 | 不知道嵌套层级 | 录制脚本给出完整路径 `[id="2d-iframe"].content_frame` |
| 序列选错 | 不知道哪个是薄层 | AGENTS.md 里有评分规则 + 帧数解析逻辑 |
| 翻页不生效 | 不知道 canvas 需要聚焦 | AGENTS.md 里有翻页策略优先级 |
| 200+ 轮迭代 | 每步都要试错 | 指引文档把试错结果固化，agent 照做即可 |

**预期效果**：
- 探索轮次：200+ → **15-30 轮**（只处理动态判断步骤）
- Token 消耗：降低 80%+
- 首次成功率：从 ~40% 提升到 ~85%

### 11.6 "一次录制，全院通用"的实现路径

```
                    一次录制
                       │
                       ▼
         processed_script_uicloud.py
         (病人 A 的具体操作)
                       │
          script_to_guide.py 转换
                       │
                       ▼
         input/uicloud.md
         (参数化指引文档，URL/密码 为占位符)
                       │
         ┌─────────────┼─────────────┐
         │             │             │
         ▼             ▼             ▼
    病人 B URL     病人 C URL     病人 D URL
    (同医院)       (同医院)       (同医院)
         │             │             │
         └─────────────┼─────────────┘
                       │
              19 平台 replay 模式
              (确定性回放，无需 agent)
```

**关键假设**（已验证）：
- 同一医院的所有病人共享相同的 viewer 结构（iframe 路径、按钮名称、canvas 选择器）
- 变化的只有：URL、密码、序列名称（动态选择）、帧数
- 录制一个病人 → 提取出通用 DOM 结构 → 适用于该医院所有病人

### 11.7 两种运行模式

#### 模式 A：确定性回放（推荐，90% 场景）

直接用本项目的 `auto_gen.py` 生成回放脚本，不经过 19 平台：

```bash
# 生成
python auto_gen.py --input out/uicloud/processed_script.py \
                   --output out/uicloud/auto_capture_uicloud.py

# 换病人运行（只改 URL）
python out/uicloud/auto_capture_uicloud.py  # 改脚本里的 URL
```

适用于：同医院、同 viewer 版本、操作路径完全相同的病人。

#### 模式 B：Agent 辅助回放（10% 场景）

当确定性回放失败时（viewer 更新了 DOM、新增了弹窗等），用 19 平台 + 精确指引：

```bash
# 把录制脚本转为指引文档
python script_to_guide.py \
    --script out/uicloud/processed_script.py \
    --agents-md AGENTS.md \
    --output F:/19_playWrightReader/input/uicloud.md

# 用 19 平台执行（有精确指引，不用盲目探索）
cd F:/19_playWrightReader
python main.py explore --url "https://uicloud.com/film/#/新病人ID" \
    --doc input/uicloud.md
```

适用于：viewer 改版、新增步骤、确定性脚本报错的恢复场景。

### 11.8 实施优先级

| 优先级 | 任务 | 工作量 | 收益 |
|--------|------|--------|------|
| P0 | 写 `script_to_guide.py`（录制脚本 → 指引文档） | 1 天 | 打通两个项目的桥梁 |
| P1 | 在 19 平台的 `_GENERAL_RULES` 中支持"精确选择器提示" | 半天 | agent 看到选择器直接用，不探索 |
| P1 | 在 `auto_gen.py` 中支持 URL 参数化（`{{url}}` 占位） | 半天 | 确定性回放支持换病人 |
| P2 | 失败自动降级：`auto_capture` 失败 → 触发 19 平台 agent | 1 天 | 全自动容错 |
| P3 | 19 平台产出的 `skills/{domain}.md` 反哺本项目的 skill | 2 天 | 知识闭环 |

### 11.9 对比：纯 Agent 探索 vs 录制+Agent 混合

| 指标 | 纯 19 平台探索 | 录制 + 指引文档 + Agent |
|------|---------------|------------------------|
| 首次接入新医院 | 200+ 轮，~30min，$2-5 token | 录制 5min + Agent 15-30 轮，~5min，$0.3 |
| 同医院新病人 | 15-30 轮（replay 模式） | 0 轮（确定性 `auto_capture.py`） |
| Viewer 改版适应 | 重新探索 200+ 轮 | 重新录制 5min 或 Agent 30 轮修复 |
| 知识沉淀 | `skills/{domain}.md`（操作步骤级） | AGENTS.md + skill（DOM 结构 + 算法级） |
| 可靠性 | ~60%（动态决策有不确定性） | ~95%（确定性优先） |

---

## 附录 A：Jinja2 模板示例

### report_screenshot.py.jinja

```python
# [MARKER: 报告截图 @ {{ ts }}]
try:
    {{ page_var }}.wait_for_load_state("networkidle", timeout=10000)
except Exception:
    print("[截图] networkidle 超时，降级继续")
{{ page_var }}.wait_for_timeout(2000)
{{ page_var }}.screenshot(path=str(SCRIPT_DIR / "report.jpeg"), type="jpeg", quality=95, full_page=True)
print("[截图] 报告已保存: report.jpeg")
```

### canvas_capture.py.jinja

```python
# [MARKER: 影像画布交互]
print("[画布] 开始全量帧翻页截图...")
frame_paths = capture_canvas_interaction(
    {{ page_var }},
    click_x={{ click_x }}, click_y={{ click_y }},
    iframe_selectors={{ iframe_selectors | tojson }},
    total_frames={{ total_frames or 'None' }},
    output_dir=str(SCRIPT_DIR / "canvas_frames"),
)
print(f"[画布] 共截取 {len(frame_paths)} 帧")
```

### meta_extract.py.jinja

```python
# [MARKER: Meta 信息工具]
{{ page_var }}.wait_for_timeout(1500)
print("[Meta] 开始提取 DICOM 信息...")
rows = extract_meta_from_frame(
    {{ page_var }},
    iframe_selectors={{ iframe_selectors | tojson }},
)
print(f"[Meta] 提取了 {len(rows)} 个 tag")
validate_and_save(rows, output_dir=SCRIPT_DIR, project_root=_PROJECT)
```

---

## 附录 B：快速启动（实施第一步）

```bash
# 1. 创建目录结构
mkdir platform platform\executor platform\validator platform_config platform_config\templates regression regression\uicloud

# 2. 创建 skill_manifest.json（复制上面的内容）

# 3. 实现 registry.py（约 80 行）

# 4. 测试注册表
python -c "from platform.registry import SkillRegistry; r = SkillRegistry('platform_config/skill_manifest.json'); print(r.match('报告截图'))"

# 5. 修复 agent.py 的 MARKER_MAP（或直接替换为 registry 调用）
```
