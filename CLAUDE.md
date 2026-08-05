# 22_dicom_model_service — Playwright Codegen 智能标记工具
## 输出为中文，思考过程也需要变成中文

## 项目记忆位置

所有项目记忆（持久化约定、工作流、调试原则）存储在项目目录下：

- **`memory/`** 目录 — 结构化记忆文件（Markdown），每个文件一个主题
  - `three-stage-workflow.md` — 三阶段工作流与调试原则
  - `loop-stop-hook-json-validation.md` — `/loop` Stop hook 报 "JSON validation failed" 的根因与规避
  - `sdd-closeout-experience.md` — SDD 计划收尾执行经验（re-review → 关 task → final review triage → push）
- **本文件（CLAUDE.md）** — 项目核心上下文，整合所有记忆的要点

不再使用系统级（AppData）记忆存储。如需添加或更新记忆，直接编辑 `memory/` 下的对应文件，并同步更新 CLAUDE.md 中的相关章节。

## 项目定位
PyQt6 桌面工具，包装 `playwright codegen` 子进程：用户在浏览器里录制操作，工具实时把生成的 Python 脚本同步到 GUI 面板，并允许用户在脚本特定位置插入预设的「标记」注释（用于标注后续要做的事情：截图、PDF 解析、序列布局切换、窗宽窗位等）。

录制目标主要是 DICOM 影像 Web 应用（uicloud.com/film 等），生成的脚本用于后续自动化回归。

## 技术栈
- Python 3.11（**必须 3.11**，3.7/3.8 没有 PyQt6 与新版 playwright 的预编译 wheel）
- PyQt6 ≥ 6.5（GUI 主线程）
- playwright ≥ 1.40（录制子进程，同步 API）
- watchdog ≥ 3.0（文件监听）+ 自带低频轮询兜底（Windows 兼容性）

依赖见 `requirements_codegen_marker.txt`。

## 环境与虚拟环境

**重要**：Windows 默认的 system Python（通常是 3.7）**不支持** PyQt6 / 新版 playwright 的 wheel，**不要**直接用 `python` / `pip`。必须用 conda 创建独立虚拟环境。

### 一次性创建环境
```bash
D:/Anaconda/Scripts/conda.exe create -n codegen-marker python=3.11 -y
D:/Anaconda/Scripts/conda.exe activate codegen-marker
pip install -r requirements_codegen_marker.txt
playwright install chromium
```

> 如果 `conda create` 首次报 exit code 137（OOM），直接重试即可，第二次通常成功。

### 已安装版本（实测）
- Python 3.11.15
- PyQt6 6.11.0
- playwright 1.60.0
- watchdog 6.0.0

### 环境路径
- 解释器：`D:/Anaconda/envs/codegen-marker/python.exe`
- pip：`D:/Anaconda/envs/codegen-marker/Scripts/pip.exe`
- playwright：`D:/Anaconda/envs/codegen-marker/Scripts/playwright.exe`

后续命令若不显式 `activate`，**必须**用绝对路径调用 `D:/Anaconda/envs/codegen-marker/python.exe`，否则会落到 system Python 3.7 上直接报错（缺 PyQt6 wheel / qmake）。

## 模块结构

| 文件 | 角色 |
|---|---|
| [main_gui.py](main_gui.py) | QMainWindow，承载 UI、信号桥接、标记插入与 append-only 增量同步；另含一组模块级纯文本函数（`detect_indent` / `compute_codegen_appendix` / `insert_marker_after_line`），不依赖 Qt，方便单元测试 |
| [codegen_manager.py](codegen_manager.py) | 后台 `playwright codegen` 子进程 + watchdog Observer + 轮询线程，仅做原文透传，不引入任何规则引擎 |
| [markers.py](markers.py) | 标记模板注册表（`DEFAULT_MARKERS`），新增标记只需追加 `Marker` 实例 |
| [recorded_script.py](recorded_script.py) | 录制输出文件，被 codegen 子进程持续覆盖（**会被自动改写，不要手编**） |
| [agent.py](agent.py) | LLM 驱动的补全引擎，读取 processed 脚本中的 marker，加载 skill → 调用 LLM → 生成 completed 脚本 |
| [test/](test/) | unittest 套件（`test_markers` / `test_codegen_manager` / `test_workflow` / `test_marker_apply`） |
| [out/](out/) | 按医院组织的产出物目录（processed 脚本、completed 脚本、截图、JSON 等） |
| [memory/](memory/) | 项目记忆文件（工作流约定、调试原则等持久化知识） |

### GUI 与文件的关系

`recorded_script.py`（磁盘文件）和 GUI 面板是**两套独立的内容**：

- **磁盘文件**：`playwright codegen` 子进程持续覆盖，里面只有录制产生的原始动作代码。
- **GUI 面板 = 工作副本**：由 `_display_items` 数据模型驱动（有序行列表，每行有 type: codegen/marker）。
  - 首次 codegen 推送 → 用全部 codegen 行初始化 `_display_items` → `_rebuild_display()` 渲染面板。
  - 后续推送 → `compute_codegen_appendix(last_count, code)` 算 delta → 新 codegen 行插入到 `_display_items` 中**最后一个 codegen 条目之后**（而非面板末尾），保证 codegen 内容始终连续，不会被用户插入的 marker 隔断。
  - marker 插入 → 右键面板 → 「插入标记」→ 写入 `_display_items` + QTextCursor 精确插入文本。
  - 删除行 → 右键面板 → 「删除当前行」→ 从 `_display_items` 移除 + QTextCursor 删除。
  - 录制停止（`self._manager = None`）→ 推送通道断开，GUI 完全独立，可放心编辑。
  - `_rebuild_display()` 在 codegen 推送时从 `_display_items` 重建面板（`setPlainText`），用户在此期间的手动编辑会被覆盖；录制停止后不再重建。

## 产出物组织

GUI 工具录制+编辑后的产出物按医院整理在 `out/` 文件夹下：

```
out/
├── cxhospital/                     # 医院名（从 URL 或人工命名）
│   ├── processed_script_cxhospital.py  ← GUI「保存处理后代码」的输出
│   ├── completed_cxhospital.py         ← agent.py 生成的补全脚本
│   ├── canvas_frames/                  ← 影像画布逐帧截图
│   ├── dicom_meta_*.json               ← Meta 信息提取结果
│   ├── report_*.png                    ← 报告页面截图
│   └── series_select_*.png             ← 序列选择 VL 回退截图
│
└── uicloud/                        # 下一个医院
    └── ...
```

### 命名规范

| 产物 | 命名格式 | 说明 |
|------|---------|------|
| 处理后脚本 | `processed_script_{hospital}.py` | 从 GUI 面板保存，含 marker 注释的原始录制脚本 |
| 补全脚本 | `completed_{hospital}.py` | `agent.py` 读取 processed 脚本，调用 skill 填充所有 marker 后的可执行脚本 |
| 自动捕获脚本 | `auto_capture_{hospital}.py` | `auto_gen.py` 从 processed 脚本 regex 抽配置 + 模板拼接生成的可执行脚本（不走 LLM） |
| 画布截图 | `canvas_frame_{序号}_{时间戳}.jpeg` | 自动命名到 `canvas_frames/` 下（补全脚本产物）或 `capture_frames/` 下（自动捕获脚本产物） |
| Meta JSON | `dicom_meta_{时间戳}.json` | DICOM tag 提取结果 |

## 补全脚本生成（Complete.py）

`agent.py` 是 LLM 驱动的补全引擎，将 `processed_script_{hospital}.py` 中的 marker 占位符替换为可执行代码：

```bash
D:/Anaconda/envs/codegen-marker/python.exe agent.py out/cxhospital/processed_script_cxhospital.py -o out/cxhospital/completed_cxhospital.py
```

### 工作原理

1. 解析 processed 脚本中的 `# [MARKER: xxx]` 标记
2. 匹配 `skills/{marker-name}/` 目录
3. 加载 skill bundle（SKILL.md + references + test_data）
4. 拼成 prompt → 调用 LLM 生成补全代码
5. 语法检查 + 结构校验
6. 失败则把错误信息追加到 prompt，重新调用 LLM 修正
7. 输出 completed 脚本

每个医院可能有不同的 viewer 结构和交互方式，因此 **complete.py 是医院级别的**，需要根据医院实际情况调整生成的代码。

## 固化捕获脚本生成（auto_capture）

> **与路径 A 互补的产物**：当 viewer 交互方式已经摸清后，可以跳过 LLM，用 `auto_gen.py` 配合 `skills/_shared/` 下的固化工具，直接生成 `auto_capture_{hospital}.py`。

### 何时用哪条管道

| 场景 | 管道 | 产物 |
|---|---|---|
| viewer 结构不稳定 / 探索期 / 需要灵活适配 | 路径 A（agent.py + LLM） | `completed_{hospital}.py` |
| viewer 交互方式已知 / 稳定运行 / 需要可重复离线执行 | 路径 B（auto_gen.py） | `auto_capture_{hospital}.py` |

两条管道**共用同一个输入**（`processed_script_{hospital}.py`），产物可并行存在。

### 使用方法

```bash
D:/Anaconda/envs/codegen-marker/python.exe auto_gen.py \
    --input out/cxhospital/processed_script_cxhospital.py \
    --output out/cxhospital/auto_capture_cxhospital.py
```

可选参数：
- `--total-frames N`：协议名解析不到帧数时手动指定。

### 工作原理（[auto_gen.py:230-396](auto_gen.py#L230) `generate()`）

1. **regex 反向抽取 6 类配置**（[auto_gen.py:42-132](auto_gen.py#L42)）：
   - `URL` ← `page.goto("...")`
   - `iframe_selectors` ← `.locator(X).content_frame` 链
   - `protocol_name` ← `get_by_text(...)`（marker 附近 8 行内）
   - `total_frames` ← 协议名数字解析（`x 1.0 AIIR_LungMPR205362幅` → 362）
   - `canvas_coords` ← `position={"x":X,"y":Y}`
   - `viewer_page_var` ← 有 `page1` → `"page1"`，否则 `"page"`
2. **生成 87 行编排脚本**：header（import + run 框架） + 录制动作行（保留原始选择器） + marker 替换块 + footer
3. **每个 marker 按 3 种模式处理**（[auto_gen.py:335-393](auto_gen.py#L335)）：
   - **硬编码**：报告截图 → `page.screenshot(...)`
   - **保留录制**（`KEEP_ORIGINAL`）：序列布局切换、窗宽窗位 → 不展开
   - **替换为 skill 调用**：影像画布交互 → `capture_canvas_interaction(...)`；Meta 信息工具 → `extract_meta_from_frame(...)` + `validate_and_save(...)`

### 路径 B 与路径 A 的关键差异

| 维度 | 路径 A（completed） | 路径 B（auto_capture） |
|---|---|---|
| **生成入口** | `agent.py` | `auto_gen.py` |
| **核心机制** | 拼 prompt → LLM 写代码 | regex 抽配置 → 模板拼字符串 |
| **marker 实现位置** | inline 在产物里（800+ 行） | 在 `skills/_shared/` 共享模块里 |
| **viewer 适配** | LLM 现场生成（DOM 遍历 / 三层降级） | 抽配置（iframe/帧数/坐标）+ 通用工具固定策略 |
| **可重入性** | 每次 LLM 结果可能略不同 | 完全可重复 |
| **执行依赖** | 每次都要 LLM 配额 | 完全离线 |
| **产物路径** | `canvas_frames/` + `dicom_meta_*.json` | `capture_frames/` + `dicom_meta_*.json` + `meta_validation/` |

### 共享工具模块（`skills/_shared/`）

`auto_capture_{hospital}.py` 通过 `sys.path.insert` 引入项目根，然后 import：

| 模块 | 关键函数 | 行为 |
|---|---|---|
| [skills/_shared/canvas_capture.py](skills/_shared/canvas_capture.py) | `capture_canvas_interaction` | 翻页 + 全量帧截图（4 策略降级 + drawImage hash 等待） |
| [skills/_shared/meta_extract.py](skills/_shared/meta_extract.py) | `extract_meta_from_frame` | 从 DICOM 面板 body_text 提取 tag 行 |
| [skills/_shared/meta_validate.py](skills/_shared/meta_validate.py) | `validate_and_save` | 校验 + 落盘 dicom_meta_*.json + meta_validation/ |

> **设计意图**：路径 A 探索出来的成功经验（`completed.py` 里 600 行画布管线的精简版）下沉到 `_shared/` 共享模块，路径 B 直接复用。

### 沉淀路径（skill 进化的两个方向）

```
viewer 不稳定期（探索）                          viewer 稳定期（生产）
─────────────────────                          ──────────────────
录制 → agent.py → completed.py                 录制 → auto_gen.py → auto_capture_*.py
       │ ↓ 成功经验沉淀                                  │
       ▼                                               │
  skills/marker-*/SKILL.md ◄────── 共享 ──────►  skills/_shared/*.py
       │ (策略规则给 LLM 看)                            │ (工具函数直接 import)
       │                                               │
       │                                               ▼
       └────────────── 两条管道可并行存在 ──────────────┘
```

### 调试对应位置

| 出问题 | 改这里 | 重新生成 |
|---|---|---|
| auto_capture_*.py 翻页失败 / 截图质量差 | `skills/_shared/canvas_capture.py` | 重跑 `auto_gen.py` |
| auto_capture_*.py Meta 提取不到 | `skills/_shared/meta_extract.py` | 重跑 `auto_gen.py` |
| auto_capture_*.py Meta 校验不通过 | `skills/marker-meta-info/scripts/validators.py` | 重跑 `auto_gen.py` |
| auto_gen.py 抽错配置（URL/iframe/帧数） | `auto_gen.py` 的 `extract_*` 函数 | 改完重跑 |
| auto_capture_*.py 输出格式不对 | `auto_gen.py` 的 `generate()` 模板 | 改完重跑 |
| completed.py 行为不合理 | `skills/marker-*/SKILL.md` + `references/` | 重跑 `agent.py` |

## 重要调试原则

生成 complete.py 的调试过程中，如果发现生成的代码不合理（选择器不对、等待方式不当、交互流程错误等）：

> **必须先更新完善对应的 skill，再去更新生成的 complete 代码。**

原因：
- Skill 是所有医院的共享知识库。如果直接在 complete.py 里手改，下次其他医院遇到相同问题还是会错。
- 完善 skill 后，重新运行 `agent.py` 即可为所有受影响医院重新生成正确的代码。
- Skill 的更新包括：SKILL.md（逻辑/规则调整）、references/（技术方案补充）、test_data/（测试用例完善）。

### 调试流程

```
发现 complete.py 代码不合理
  → 定位对应的 skill（如 marker-sequence-select）
  → 分析问题根因（选择器差异 / 等待策略 / 布局结构）
  → 更新 skill 内容（SKILL.md / references）
  → 重新运行 agent.py 生成 complete.py
  → 验证修复效果
  → 重复直到合理
```
- **主线程**：Qt 事件循环，所有控件操作都在这里。
- **子线程**：watchdog Observer 线程 + `CodegenManager` 的轮询线程。
- **桥接**：[main_gui.py:96](main_gui.py#L96) `CodeUpdateEmitter.code_ready` 把子线程回调里的原文投递到主线程。
- 子线程回调中**禁止**直接操作 Qt 控件，必须通过 `pyqtSignal` 转发。

## 关键约定

### 标记插入（v3 — codegen 偏移 + 指纹锚定）

- **数据模型**：`_display_items: List[Dict]`，每项 `{"type": "codegen"|"marker", "text": str}`。这是面板的单一真相源。
- `_on_code_ready`（codegen 推送）：
  - 首次 → 用全部 codegen 行填充 `_display_items` → `_rebuild_display()`。
  - 后续 → 全量用新 codegen 行重建 codegen 条目，再调用 `relocate_markers(new_codegen_lines, self._marker_anchors)` 把 marker 按锚点重定位后插回 → `_rebuild_display()`。
  - **为什么全量替换**：playwright codegen 新动作插在收尾段（`# ----` / `context.close` / `with sync_playwright`）**之前**而非文件末尾，取末尾 delta 会丢动作 + 重复收尾段（见 `test_middle_insert_bug.py`）。
- **marker 锚定机制**（`_marker_anchors`，每项 `{codegen_idx, fingerprint, items}`）：
  - `codegen_idx` = 锚定的 codegen 行在【纯 codegen 序列】中的 0-based 偏移，跨推送稳定。
  - `fingerprint` = 该 codegen 行 `rstrip()` 后的文本，容错 trailing 空格 / 细微改写。
  - `relocate_markers` 重定位：先按偏移定位 + 指纹校验；失配则用指纹全局查找最接近原偏移的匹配行（修复重复行命中第一个的 Bug）；仍找不到 → **丢弃**（不再甩到末尾，修复锚点改写后飞末尾的 Bug）。
  - 多个 marker 锚定同一 codegen 行时按 anchor 列表顺序成组插入，保持用户插入顺序。
- `_insert_marker(marker)`（右键菜单触发）：
  - 取 `cursor.blockNumber()` 得面板行号 → `_codegen_idx_for_panel_line` 映射到纯 codegen 序列偏移（marker 行则锚定前一个 codegen 项）→ `detect_indent` 取缩进。
  - 构造 marker 条目（type="marker"）→ 插入 `_display_items`（锚点行后）+ 记录 anchor（偏移+指纹）。
  - 同时用 `QTextCursor.insertText("\n" + marker 文本)` 精确插入，**不调用 `setPlainText`**，避免清空 undo 栈和光标跳动。
- `_delete_current_line`（右键菜单触发）：
  - 从 `_display_items` 删除对应条目 → `QTextCursor` 删除面板对应行。
  - 若删的是 marker 行，**同步按 items 身份匹配从 `_marker_anchors` 移除**，避免下次推送时被删 marker 复活（修复删后复活的 Bug）。
- 右键菜单（`_on_context_menu`）：
  - `self.code_view.setContextMenuPolicy(CustomContextMenu)` → `customContextMenuRequested` 信号。
  - 菜单项：「➕ 插入标记」子菜单（遍历 DEFAULT_MARKERS）、分隔线、「🗑 删除当前行」。
- `_rebuild_display()`：把所有 `_display_items[*]["text"]` 用 `\n` 拼接 → `setPlainText(text)`。
  - **注意**：录制期间 `_rebuild_display()` 会覆盖用户在面板中的自由文本编辑；停止录制后不再重建，用户可自由编辑。
- 文本变化通过 `textChanged` 信号同步到 `_latest_code`，「保存处理后代码」直接拿面板当前内容。
- `_panel_initialized` 在「启动录制」与「清空展示」时重置为 False。

### CodegenManager 的去重 ([codegen_manager.py:164](codegen_manager.py#L164) `_handle_change`)
- 不依赖 mtime 单调，**按内容比对**去重：相同内容不重复触发回调。
- watchdog 事件 + 兜底轮询两条路径汇入同一函数，互不打架。
- 任何来源的 `on_update` 回调异常都被吞掉，绝不能让监听线程崩。

### Windows 进程终止 ([codegen_manager.py:85](codegen_manager.py#L85))
- 子进程必须用 `CREATE_NEW_PROCESS_GROUP` 启动，`stop()` 时用 `CTRL_BREAK_EVENT` 才能干净退出，否则可能留下僵尸浏览器进程。

## 如何修改问题
现在在用户发现bug了之后，首先需要进行分析，输出分析方案，并且提供分析报告给用户。如果不确定原因需要尝试复现。

在 complete.py 生成流程中，如果发现生成的代码不合理，**先更新完善 skill，再重新生成 complete 代码**——不要直接手改 complete.py。详见本章「重要调试原则」。

## /loop Stop hook 报 "JSON validation failed"（详见 memory/loop-stop-hook-json-validation.md）

长任务用 `/loop 用子代理把所有计划完成…` 收尾时，每轮结束可能出现 `Stop hook error: JSON validation failed`：

- **根因**：built-in `/loop` 注册的 Stop 钩子要求模型 stdout 恰好是**裸 JSON** 判定 `{"ok": true}` 或 `{"ok": false, "impossible": bool, "reason": "…"}`。本机 proxy 模型（DeepSeek-V4-Flash，`ANTHROPIC_BASE_URL: http://127.0.0.1:15721`）会把判定包进 markdown 围栏/散文，harness 对整段 stdout 做 `JSON.parse` 失败。
- **非阻塞**：`preventedContinuation:false`——循环不崩，只是无法干净判定，反复自检反复报错。判定逻辑往往本就对（`ok:false` 因任务确实没做完），错只在格式。
- **查不到配置**：该钩子是 built-in `/loop` 运行时自建的内存钩子，`~/.claude/settings.json` / `~/.claude.json` / 项目 `.claude` / superpowers 插件 hooks.json 都无 Stop hook 可编辑。
- **规避**：长任务收尾**直接手动在当前会话推进**（review → 关 task → final review → push），让条件真正满足再靠 `/loop` 判定（此时正确输出 `{"ok": true}`）；或把条件措辞写成「只输出裸 JSON、不要代码围栏」。

## SDD 计划收尾（详见 memory/sdd-closeout-experience.md）

用 superpowers SDD 跑多任务实现计划时，收尾路径：

1. **逐 task 收尾**：scoped review 有 Critical/Important → implementer 出 fix round → 提交 → **派发 scoped re-review** 独立验证 fix 真关闭 finding（只审 fix 改动）。
2. **关 task**：re-review approved 后在 `.superpowers/sdd/{plan}/progress.md`（ledger）记录「fix round + re-review ✅ + complete」。
3. **final whole-branch review**：全部 task 关完才做，聚焦跨 task 集成 + plan 全局约束合规，对 ledger 里 deferred minor / parked 项做 **triage 定案**（修复 / 规格对齐 / minor 不阻断 / 留待后续，逐一给 reason）。
4. **push**：只 push `origin/main..HEAD` 未推送提交（`git log --oneline origin/main..HEAD` 看范围）。

**注意事项**
- 测试/子进程一律用 `D:/Anaconda/envs/codegen-marker/python.exe`，**禁止静默回退 `sys.executable`**（系统 Python 3.7 缺 PyQt6/playwright wheel）。
- `test_batch_capture_replicate` 在 headless 下 import 时挂起（导入 Playwright/浏览器），非代码引入。单测用：
  `PYTHONIOENCODING=utf-8 D:/Anaconda/envs/codegen-marker/python.exe -m unittest test.test_orchestrator_events test.test_agent_marker_boundaries test.test_replica_gui -v`
  （`unittest discover` 会含浏览器集成测试导致挂起。）
- **triage 倾向**：实现跨事件一致、spec 单点不一致时，倾向**改 spec 文本对齐稳定代码**而非改代码（例：agent_failed 字段实现统一用 `status`、spec §4.7 误写 `reason`，最终改 spec）。

## 如何运行
```bash
D:/Anaconda/envs/codegen-marker/python.exe main_gui.py
```

启动后填 URL → 点「启动录制」→ 在弹出的浏览器里操作 → 回到 GUI 面板，**右键**要插入标记的位置 → 「插入标记」→ 选标记类型 → 标记直接插入到该行后面。停止录制后可手动编辑或点「保存处理后代码」落盘到 `out/{hospital}/processed_script_{hospital}.py`。

### 后续：补全脚本生成

```bash
D:/Anaconda/envs/codegen-marker/python.exe agent.py out/cxhospital/processed_script_cxhospital.py -o out/cxhospital/completed_cxhospital.py
```

`agent.py` 读取 processed 脚本中的 marker → 加载 `skills/` 下对应的 skill → 调用 LLM 填充可执行代码 → 输出 completed 脚本。

> **调试原则**：如果生成的 complete.py 代码不合理，先更新完善对应的 skill（SKILL.md / references），再重新运行 agent.py 生成。不要直接手改 complete.py。

> **不要**写 `python main_gui.py` —— 系统 Python 是 3.7，会报 `ModuleNotFoundError: No module named 'PyQt6'` 或 pip 编译错。

## 如何测试
```bash
D:/Anaconda/envs/codegen-marker/python.exe -m unittest discover -s test -v
```
- `test_markers.py` 验证 marker 模板注册表不变量 + `{ts}` 占位符替换。
- `test_codegen_manager.py` 覆盖文件读写兜底与内容去重。
- `test_workflow.py` 端到端用 `MagicMock` 替换 `subprocess.Popen`，不依赖真实浏览器。
- `test_marker_apply.py` 直接调用 `compute_codegen_appendix` / `insert_marker_after_line` / `detect_indent` 等模块级纯函数，不依赖 Qt，覆盖 append-only 增量同步逻辑（首次推送 / 增量 / 无增量 / 行数回退 / 用户手编共存）以及 marker 插入逻辑（缩进沿用 / 锚点行号越界 / 末尾空行丢弃）。
- `test_qt_workflow.py` Qt 级别集成测试，验证完整工作流（推送 → 插入 marker → 推送 → 删除行），确认数据模型与面板同步正确。
- `test_marker_position_bug.py` 复现并锁定的三个 marker 位置 Bug：锚点行被改写后飞末尾 / 删除 marker 后推送复活 / 重复行命中第一个匹配。修复后应持续转绿，防止锚定机制回退。
- `test_middle_insert_bug.py` 锁定 codegen 中段插入导致收尾段重复的 Bug，验证全量替换方案。

## 常见坑
- **`recorded_script.py` 会被录制进程持续改写**，不要在这里写有意义的代码或提交它做 review。
- `test_playwright.py` 是早期的人工录制样本（URL 已脱敏），用于参考录制输出格式，不是测试入口。
- marker 模板末尾留一个空字符串 `""` 是为了 `render().split("\n")` 后留出换行；`insert_marker_after_line` 在拼装前会 `pop()` 末尾空串，不要去掉。
- **GUI 面板 = 工作副本**。codegen 推送时新 codegen 行插入到最后一个 codegen 条目之后（而非面板末尾），保证 codegen 内容始终连续。marker 通过右键菜单插入。录制期间 `_rebuild_display()` 会覆盖用户在面板中的手动编辑，停止录制后不再覆盖。
- **不要**直接用 `pip install -r requirements_codegen_marker.txt`，必须先 `conda activate codegen-marker` 或用绝对路径 pip，否则会装到 system Python 3.7 上。
- **不要**用 `python main_gui.py` / `python -m unittest ...`，系统 Python 3.7 缺 PyQt6 wheel；统一用 `D:/Anaconda/envs/codegen-marker/python.exe`。
- **截图/图片用 `.jpeg` 不要用 `.png`**：本机对 `.png` 结尾的文件会自动加密（文件头被改写，Read 工具读取报 `unrecognized bytes`）。需要生成或保存图片（如 GUI 截图验证、report 截图、canvas 帧截图）时，一律用 `.jpeg` 扩展名 + `pix.save(path, 'JPEG')`，避免加密导致文件无法读取。`out/` 下既有的 `report.jpeg` / `canvas_frame_*.jpeg` 已遵循此约定。
- **`agent.py` 的 `MARKER_MAP` 只注册走 LLM 路径的 marker**（[agent.py:35-44](agent.py#L35)）。当前 4 个：`报告截图` / `序列选择` / `Meta 信息工具` / `影像画布交互`，全部对应 `skills/` 下真实存在的 skill 目录。**「窗宽窗位 WL/WW」和「序列布局切换」是固定操作**（录制时手编的按钮点击 / 输入填充），**故意不**注册到 `MARKER_MAP`：agent.py 见到它们会因 skill 目录缺失而**静默跳过**（[agent.py:238-241](agent.py#L238)），marker 块（注释 + 录制操作）原样保留在 completed.py 里。这是预期行为，**不需要**补对应 skill 目录。GUI 菜单里这两个 marker 仍在 `markers.py:DEFAULT_MARKERS` 中可见可插入。Marker 的「手编操作 vs LLM 补全」分类见 [AGENTS.md:65-74](AGENTS.md#L65)。
