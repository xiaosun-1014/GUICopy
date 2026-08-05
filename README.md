# Playwright Codegen 智能标记工具

> 基于 PyQt6 的桌面工具，包装 `playwright codegen` 子进程，在录制浏览器操作的同时，向生成的 Python 脚本中插入预设的「标记」注释，用于标注后续需要处理的动作（截图、PDF 解析、序列布局切换、窗宽窗位调整等）。

---

## 功能概览

| 功能 | 说明 |
|------|------|
| **🎥 录制操作** | 启动 `playwright codegen` 子进程，自动弹出浏览器，实时捕获操作生成 Python 脚本 |
| **📋 同步脚本** | 录制生成的代码自动同步到 GUI 面板，无需手动复制 |
| **🏷️ 插入标记** | 右键面板任意位置 → 「插入标记」→ 选择预设标记类型，标记注释精准插入当前行之后 |
| **🗑️ 删除行** | 右键面板 → 「删除当前行」，同步从数据模型和界面中移除 |
| **💾 保存脚本** | 停止录制后可直接保存带标记的处理后脚本 |
| **🔀 录制容错** | 录制过程中即使脚本被覆盖，已插入的标记不会丢失 |

### 支持的标记类型

| 标记 | 用途 |
|------|------|
| 📸 报告截图 | 截取报告页面截图，固定产物 `report.jpeg` |
| 🔲 序列布局 | 切换 1×1 / 2×2 / MPR 等布局 |
| 🖼️ 影像画布 | 调用 VL 模型对当前帧做判定 / 切帧 |
| 🎚️ 窗宽窗位 | 遍历预设窗（肺窗 / 骨窗 / 软组织窗） |
| 🔲 序列选择 | 对当前序列帧做判定 / 切帧 |
| 📋 Meta 信息工具 | 提取当前检查的 Meta 信息（Patient / Study / Series） |

---

## 技术栈

| 组件 | 版本要求 |
|------|---------|
| Python | **3.11**（3.7/3.8 不支持 PyQt6 wheel） |
| PyQt6 | ≥ 6.5 |
| playwright | ≥ 1.40 |
| watchdog | ≥ 3.0 |

完整依赖见 [`requirements_codegen_marker.txt`](requirements_codegen_marker.txt)。

---

## 环境准备

### 1. 使用 Conda 创建独立虚拟环境

> ⚠️ Windows 默认的 system Python（通常是 3.7）**不支持** PyQt6 / 新版 playwright 的 wheel，必须使用 conda 虚拟环境。

```bash
D:/Anaconda/Scripts/conda.exe create -n codegen-marker python=3.11 -y
```

> 若首次 `conda create` 报 exit code 137（OOM），直接重试即可，第二次通常成功。

### 2. 安装依赖

```bash
D:/Anaconda/envs/codegen-marker/Scripts/pip.exe install -r requirements_codegen_marker.txt
```

### 3. 安装 Playwright 浏览器

```bash
D:/Anaconda/envs/codegen-marker/Scripts/playwright.exe install chromium
```

### 已测试的环境版本

- Python 3.11.15
- PyQt6 6.11.0
- playwright 1.60.0
- watchdog 6.0.0

### 环境路径

| 工具 | 绝对路径 |
|------|---------|
| 解释器 | `D:/Anaconda/envs/codegen-marker/python.exe` |
| pip | `D:/Anaconda/envs/codegen-marker/Scripts/pip.exe` |
| playwright | `D:/Anaconda/envs/codegen-marker/Scripts/playwright.exe` |

> 后续命令若不显式 `conda activate`，**必须**用绝对路径调用，否则会落到 system Python 3.7 上导致报错。

---

## 快速开始

```bash
D:/Anaconda/envs/codegen-marker/python.exe main_gui.py
```

1. **输入 URL** — 填写目标 DICOM Web 应用地址（如 `https://uicloud.com/film/...`）
2. **点击「启动录制」** — Playwright 浏览器窗口自动弹出
3. **在浏览器中操作** — 所有操作实时同步到 GUI 面板
4. **右键插入标记** — 在面板中右键 → 「插入标记」→ 选择标记类型，标记精确插入当前行之后
5. **停止录制** — 点击「停止录制」，面板进入自由编辑模式
6. **保存脚本** — 点击「保存处理后代码」将脚本写入 `out/{医院名}/processed_script_{医院名}.py`

### 后续：补全脚本生成

录制完成后，使用 `agent.py` 将 processed 脚本中的 marker 填充为可执行代码：

```bash
D:/Anaconda/envs/codegen-marker/python.exe agent.py out/cxhospital/processed_script_cxhospital.py -o out/cxhospital/completed_cxhospital.py
```

`agent.py` 读取脚本中的 `# [MARKER: xxx]` 标记 → 匹配 `skills/{标记名}/` 目录 → 加载 skill bundle（SKILL.md / references / test_data）→ 调用 LLM 生成补全代码 → 语法校验 → 输出 completed 脚本。

> **⚠️ 调试原则**：如果发现生成的 complete.py 代码不合理（选择器不对、等待策略不当等），**必须先更新完善对应的 skill**（SKILL.md / references），**再重新运行 agent.py 生成**。不要直接手改 complete.py，否则其他医院遇到相同问题还会出错。

---

## 产品管道：生成 Adapter + 离线复刻

录制完成后，可通过 GUI 的「⚙️ 生成 Adapter + 离线复刻」按钮一键执行端到端产品管道：把一份带 marker 的录制脚本转成可在**本机离线复现**的 Adapter + 静态 Replica 站点 + 确定性验证报告。完整操作手册见 [`docs/PIPELINE_RUNBOOK.md`](docs/PIPELINE_RUNBOOK.md)，真实站点冒烟清单见 [`docs/REAL_SITE_SMOKE_TEST.md`](docs/REAL_SITE_SMOKE_TEST.md)。

### 一键路径（GUI）

1. **录制 + 插入 marker**：录制浏览器操作，在报告截图、影像画布、Meta 信息工具等位置插入 marker（见上文「快速开始」）。
2. **保存**：先点击「保存处理后代码」把带 marker 的脚本写入 `out/{医院}/processed_script_{医院}.py`（按钮在脚本已保存后方可用）。
3. **一键运行**：点击「⚙️ 生成 Adapter + 离线复刻」，选择登录方式后由 GUI 以子进程启动 `pipeline_orchestrator.py`，事件流实时回显。
    - **脚本登录（scripted）**：录制脚本内含登录动作，管道自动回放。
    - **手动登录（interactive）**：管道在浏览器弹窗让操作者手工登录，完成点「登录完成，继续」。
    - **storage-state**：传入已保存的浏览器状态文件（供非 GUI 场景调用），跳过登录。
4. **查看结果**：管道结束后从 GUI 状态栏或产物目录打开报告与副本（见下）。

### 产物目录结构（run tree）

每次运行生成独立运行目录，稳定产物名可供断点重跑：

```
out/{医院}/runs/{run_id}/
├── source/            # 录制 source 脚本 + replica_annotations.json（含 marker_id 映射）
├── adapter/           # completed_{医院}.py（Adapter）→ completed_{医院}_offline.py（离线副本）
├── capture/           # capture_to_manifest 生成的 capture/manifest.json + 逐帧证据
├── replica/           # 离线静态 Replica：index.html + assets + serve_replica.py
├── validation/        # 离线验证产物：dicom_meta.json + patient_info.json + 校验报告
├── logs/              # 管道日志
├── pipeline_state.json      # 运行状态（断点续跑依据）
├── pipeline_events.jsonl    # 逐事件 JSONL（含脱敏）
├── pipeline_report.json     # 确定性报告（事实来源）
└── pipeline_report.html     # 报告的可读 HTML 渲染
```

固定产物名：报告截图 `report.jpeg`、DICOM 元数据 `dicom_meta.json`——**不带时间戳**，保证产物名稳定、可被后续验证步骤定位。

### 状态语义

- **success**：所有阶段全部通过（Adapter 生成、现场采集、Replica 构建、Replica 校验、离线 Adapter 校验）。
- **partial**：管道跑通但存在非阻断项（如某验证指标降级，仅部分 marker 通过）。
- **failed**：任一关键阶段失败（类型见下），管道停止并生成报告。
- **cancelled**：由操作者主动取消。
- 失败按 `error_category` 归类（如 `authentication`、`adapter_generation`、`replica_build`、`privacy_violation`），并写入报告。

### 打开 Replica 与报告

```bash
# 在 run 目录下启动本地静态副本服务并打开 replica/index.html
D:/Anaconda/envs/codegen-marker/python.exe out/{医院}/runs/{run_id}/replica/serve_replica.py

# 打开确定性报告（JSON 事实来源 + HTML 渲染）
# out/{医院}/runs/{run_id}/pipeline_report.html
```

报告不会内联病人数据或图片，只渲染已脱敏的 JSON 字段与本地相对产物链接。

### 断点重跑（rerun operations）

从稳定产物可单独重跑任一环节，而无需重新录制或重新调用 LLM：

```bash
# 仅重新生成 Adapter（需 source/）
D:/Anaconda/envs/codegen-marker/python.exe pipeline_orchestrator.py \
    --script out/{医院}/runs/{run_id}/source/processed_script_{医院}.py \
    --annotations out/{医院}/runs/{run_id}/source/replica_annotations.json \
    --hospital {医院} --output-root out --run-id {run_id} --operation adapter-only

# 仅重新构建 Replica（需已有 capture/manifest.json）
... --operation replica-build

# 仅重跑离线验证（需 adapter/completed_{医院}_offline.py + capture + replica）
... --operation offline-validation
```

`--operation` 可选 `full | adapter-only | replica-build | offline-validation`。`--auth-mode` 可选 `scripted | interactive | storage-state`。

### 隐私注意

- **截图**：报告截图 `report.jpeg`、画布帧、`dicom_panel_fallback.jpeg` 均可能含患者影像/姓名，仅用于本地验证与归档，**不得**外发。
- **Metadata**：`dicom_meta.json` / `patient_info.json` 含 Patient / Study / Series 标签，管道会对其做脱敏处理后再写入报告事件流；任何产物都不要上传到公共仓库。
- 管道日志与事件 `pipeline_events.jsonl` 在写入前会脱敏（URL 重写、凭据字段以 `REDACTED` 替代）。

### `.env` 配置

一键管道复用 `agent.py` 的 LLM 配置（Adapter 生成阶段需要 LLM），复制 `.env.example` 为 `.env` 填入真实值。键位如下（**不带真实值**，仅文档说明）：

| 键 | 用途 |
|----|------|
| `LLM_API_KEY` | LLM 服务 API Key |
| `LLM_BASE_URL` | LLM 服务 Base URL |
| `LLM_MODEL` | 默认补全模型名 |
| `LLM_MAX_TOKENS` | 单次补全最大 token 数（可选） |
| `VL_API_KEY` / `VL_BASE_URL` / `VL_MODEL` | 视觉模型专用配置（可选，缺省回退到 `LLM_*`） |

---

## 模块结构

```
├── main_gui.py               # QMainWindow 主窗口，UI 布局、信号桥接、标记插入
├── codegen_manager.py        # 后台 playwright codegen 子进程 + 文件监听
├── markers.py                # 标记模板注册表（Marker 数据类 + DEFAULT_MARKERS）
├── agent.py                  # LLM 补全引擎：marker → skill → LLM → completed 脚本
├── recorded_script.py        # 录制输出文件（被 codegen 子进程持续覆盖，不要手编）
├── requirements_codegen_marker.txt  # Python 依赖清单
├── out/                      # 按医院组织的产出物目录（processed/completed 脚本、截图、JSON）
├── test/
│   ├── test_markers.py       # 标记模板注册表不变量验证
│   ├── test_codegen_manager.py # 文件读写兜底与内容去重
│   ├── test_workflow.py      # 端到端工作流（Mock 替换 subprocess）
│   ├── test_marker_apply.py  # 纯文本函数增量同步 + marker 插入逻辑
│   └── test_qt_workflow.py   # Qt 级别集成测试（数据模型 + 面板同步）
└── CLAUDE.md                 # 项目记忆（agent 开发参考）
```

---

## 核心设计

### 数据模型（v2）

GUI 面板由 **`_display_items`**（有序行列表）驱动，每行包含类型和文本：

```
_display_items = [
    {"type": "codegen", "text": "page.get_by_role(...).click()"},
    {"type": "marker",  "text": "# [MARKER: 报告截图 @ 20250101_120000]"},
    {"type": "codegen", "text": "page.get_by_text(...).click()"},
]
```

- **codegen 推送**：新代码行追加到最后一个 codegen 条目之后（不会跨 marker 跑到面板末尾）
- **marker 插入**：右键菜单在点击位置插入 → 写入 `_display_items` → `QTextCursor` 精确插入
- **录制停止**：断开推送通道，用户可自由编辑；最终脚本由 `_display_items` 合成

### 线程模型

```
┌─────────────────────────────────────────────────────┐
│                  主线程（Qt 事件循环）                  │
│  MainWindow — 所有控件操作、右键菜单、面板渲染           │
│  ┌──────────────────────────────────────────────┐   │
│  │ CodeUpdateEmitter (pyqtSignal)              │   │
│  │   code_ready   → _on_code_ready (主线程)      │   │
│  │   status_ready → _show_status (主线程)        │   │
│  └──────────────────────────────────────────────┘   │
└──────────────┬──────────────────────────────────────┘
               │ 信号桥接
┌──────────────▼──────────────────────────────────────┐
│                子线程                                 │
│  CodegenManager — watchdog Observer + 轮询线程        │
│  ▶ 只做原文透传，不操作 Qt 控件                        │
└─────────────────────────────────────────────────────┘
```

### 标记插入流程

```
右键点击面板
  → 获取 cursor.blockNumber() 得到锚点行号
  → detect_indent() 取锚点行缩进
  → render(marker) 替换 {ts} 占位符
  → 插入 _display_items（锚点行后）
  → 保存锚点信息（用于 codegen 推送后重定位 marker）
  → QTextCursor.insertText() 精确插入面板文本
```

### 文件监听的去重

`CodegenManager` 使用 watchdog 事件 + 低频轮询（1s）双路径兜底，**按内容比对去重**（不依赖 mtime），相同内容不重复触发回调，确保 Windows 网络盘 / 防病毒场景的兼容性。

---

## 运行测试

```bash
D:/Anaconda/envs/codegen-marker/python.exe -m unittest discover -s test -v
```

测试覆盖：
- `test_markers.py` — 标记模板不变量、`{ts}` 占位符替换
- `test_codegen_manager.py` — 文件读写兜底、内容去重
- `test_workflow.py` — 端到端 Mock 测试（不依赖真实浏览器）
- `test_marker_apply.py` — 纯函数增量同步、marker 插入逻辑
- `test_qt_workflow.py` — Qt 集成测试（完整工作流：推送 → 插入 → 删除）

---

## 常见问题

**Q: 为什么不用 `python main_gui.py`？**
A: 系统 Python 通常是 3.7，缺少 PyQt6 的预编译 wheel。必须用 conda 虚拟环境的 Python。

**Q: `recorded_script.py` 有什么用？**
A: 它是录制进程持续改写的输出文件，不要手编或提交代码审查。GUI 面板展示的内容才是工作副本。

**Q: 录制过程中能编辑面板吗？**
A: 可以。录制期间 `_rebuild_display()` 会在 codegen 推送时刷新面板覆盖手动编辑，但已插入的 marker 会通过锚点重定位机制保留。停止录制后不再覆盖，可自由编辑。

**Q: `out/` 文件夹下的文件是怎么组织的？**
A: `out/{医院名}/` 下存放该医院的所有录制与产物：`processed_script_{医院名}.py`（GUI 面板保存的原始脚本，含 marker）、`completed_{医院名}.py`（agent.py 生成的补全脚本）、`canvas_frames/`（影像截图）、`dicom_meta.json`（元数据，固定文件名）、`report.jpeg`（报告截图，固定文件名）等。一键产物管道另有 `runs/{run_id}/` 目录结构，见下方「产品管道」章节。

**Q: completed.py 和 processed_script.py 有什么区别？**
A: `processed_script.py` 是 GUI 直接保存的，里面 marker 还是占位注释（`# [MARKER: xxx]`）；`completed.py` 是 agent.py 读取 processed 脚本后，通过 LLM + skill 把每个 marker 替换为可执行代码的最终产物。不要直接手改 completed.py，如果有问题应该更新对应的 skill 后重新生成。

**Q: 如何在录制结束后继续编辑？**
A: 点击「停止录制」后，推送通道断开，面板变为纯文本编辑器，可任意增删改代码。

---

## 许可

内部开发工具。
