
你是这个 DICOM 影像 Web 复刻工具链的开发者。项目用 PyQt6 + Playwright 录制操作，录制脚本中带 `# [MARKER: xxx]` 标注，下游有两条独立管线：

1. **复刻管线**：live 回放录制脚本并**自动探索全部序列**（不只录制的那一个）→ 落中间快照 → `build_replica` 构建**离线静态 replica**（截图帧 + 热区 overlay）→ 校验。
2. **Adapter 管线**：`agent.py` 读 processed 脚本 + marker → 加载 `skills/` 对应 skill → LLM / 确定性补全 → 生成 completed 脚本（可执行）。

已录制两家医院（**均已录完，勿重录**）：`ftimage`（8 序列，run `20260816T050045Z-f44c89` 全 8/8 captured）、`zscloud`（4 序列，run `20260817T214500Z-layout-fullfix`：3 captured + 1 partial）。

## 本次任务

在**两条管线**上：先**诊断**，再**动手改共享代码**，最后**回归验收**。

第一要务是回答用户的核心疑问：**复刻过程中反复出现的问题，到底是「代码流程缺陷」还是「viewer 适配问题」还是「复刻机制本身的约束」？** 每类问题都必须给出**证据链**（文件:行号 + 运行日志/复现步骤），不能凭印象下结论。诊断清楚后再改代码。

### 已知问题线索（只作路标，必须亲自验证，不得当作定论）

**复刻侧**
- 中山布局⇄序列耦合（`docs/ZSCLOUD_LAYOUT_SERIES_REPLICA_ISSUE_2026-08-17.md` R1–R4 有完整证据）：
  - R1 布局被烘焙进「选序列后的每一帧」，点序列 = 整页跳 1×1 分支帧（布局与序列未解耦）
  - R2 `capture_locator_snapshot` 多元素 locator strict-mode 抛错被 `except: pass` 静默吞 → `target.json` 缺失 → 序列选择转场死胡同
  - R3 布局浮层只有录制过的选项可点，其余纯装饰
  - R4 入口布局按钮与序列热区重叠 → 误触
- ft 多序列身份与离线点击（`docs/FIX_FT_MULTI_SERIES_CLICK_AND_DISCOVERY_2026-08-16.md`，**已修，勿回退**）：动态下载进度被当稳定身份（9 分 8）、series route 缺几何、z-index 竞争。
- 分支/入口 series region 归属（`_promote_series_regions_to_earliest_documents` / `_reroute_branch_series_regions_to_viewer_documents`，**已加，勿回退**）。
- 中山 Dapeng viewer：WL/WW 固定操作录制选择器（`#popTagText_*`）在该 viewer 不存在 → 空等超时拖垮整 run；分享页 SPA 渲染竞态 → popup 偶发超时。
- Meta 面板为可滚动面板（历史已修，回归确认）。

**Adapter 侧（`agent.py` / `pipeline_adapter.py` / `skills/`）**
- LLM 每次生成结果可能略不同（可重入性）；语法检查通过但运行时选择器/等待策略失败；skill 更新后未重新生成导致产物与 skill 不一致。
- 部分 marker 走确定性生成（Meta / 影像画布交互），部分走 LLM（序列选择），「窗宽窗位」「序列布局切换」是手编固定操作不替换——分类必须与 `AGENTS.md`+`markers.py` 一致。

## 必读资料（进入任务先读，快速过一遍）

- `docs/MULTI_HOSPITAL_REPLICA_RUNBOOK.md` — 整体流程与三家医院差异
- `docs/PIPELINE_RUNBOOK.md` — capture / build / adapter / validation 各操作命令语义
- `docs/ZSCLOUD_LAYOUT_SERIES_REPLICA_ISSUE_2026-08-17.md` — 中山布局耦合根因与证据链
- `docs/PLAN_FIX_ZSCLOUD_LAYOUT_SERIES_COUPLING_2026-08-17.md` — 中山修复执行计划；**先 `git log --oneline` 核对哪些步骤已落地**，别重复做 / 别推翻已提交修复
- `docs/FIX_FT_MULTI_SERIES_CLICK_AND_DISCOVERY_2026-08-16.md` — ft 缺陷与修复验收标准
- `memory/zscloud-dapeng-replica-adaptation.md` — 中山实战经验（Dapeng viewer、popup 竞态、region 归属）
- `CLAUDE.md` — 项目约定（**必须遵守**）
- `AGENTS.md` — marker 分类与 skill 补全策略

核心代码：`batch_capture_replicate.py` / `capture_snapshot.py` / `build_replica.py` / `pipeline_orchestrator.py` / `replica_models.py` / `pipeline_validation.py` / `agent.py` / `pipeline_adapter.py` / `rewrite_script.py` / `skills/`（`marker-*`、`_shared/`、`viewers.yaml`）/ `test/`

## 工作硬约束（违反即失败）

1. **解释器一律** `D:/Anaconda/envs/codegen-marker/python.exe`。禁止裸 `python` / `pip`（系统 Python 3.7 无 PyQt6/playwright wheel）。
2. **改共享代码 / skill / 配置**，禁止手改产物（`completed_*.py` / `auto_capture_*.py` / manifest / 截图）。需改产物时改源码后重新生成。
3. **先修 skill / 共享逻辑 → 再重新生成代码**（`agent.py` 产 completed，`auto_gen.py` 产 auto_capture），不直接手改 completed。
4. **测试**：单测用
   `PYTHONIOENCODING=utf-8 D:/Anaconda/envs/codegen-marker/python.exe -m unittest test.test_orchestrator_events test.test_agent_marker_boundaries test.test_replica_gui -v`
   （**不要** `unittest discover` 全量，会含浏览器集成测试导致挂起）。涉及 UI/浏览器的一律 `$env:QT_QPA_PLATFORM='offscreen'` 跑。
5. **截图/图片一律 `.jpeg`**（`.png` 会被本机加密、Read 报 unrecognized bytes）。
6. **环境能跑浏览器就跑浏览器套件**（离线 DOM 断言 / runtime 测试），不要只看单测。
7. **选择器同源原则**：发现 / 激活 / 定位 / 层级必须同源；**多元素 locator 的单元素操作必须 `.first`**；`locator.evaluate` 别整体吞成 None，多匹配才归一。
8. 每处修改**先跑相关既有测试确认基线**，再动；改完补回归测试锁住。

## 验收标准（定义 done）

1. **诊断报告**：输出每类问题的根因分层（代码流程缺陷 / viewer 适配 / 机制约束），附文件:行号 + 证据；**先交诊断，再交改动**。
2. **复刻侧**：中山 headless 全链路「布局独立于序列、序列可切、分支可再切、入口布局不误触」；ft 保持 `discovered=8 / 8 捕获 / count_conserved` 不回归。老 run 离线 rebuild 兼容不破坏。
3. **Adapter 侧**：从 ft / zscloud 两家 `processed_script_*.py` 各重新生成 completed，语法检查通过；skill 更新落库；确定性 marker 与 LLM marker 分类与文档一致。
4. **测试全绿**：上面第 4 条的单测命令全绿；新增回归测试。

## 交付物（会话结束时给出）

- 诊断报告（根因分层 + 证据）
- 改动清单（文件 → 改了什么 → 为什么）
- 测试结果（跑过的命令与输出摘要）
- 遗留问题与建议（尤其：需要重录才能验证的项、机制约束类问题的长期路线）

> 环境路径/命令速查：
> - Python：`D:/Anaconda/envs/codegen-marker/python.exe`
> - 复刻运行：`D:/Anaconda/envs/codegen-marker/python.exe pipeline_orchestrator.py --hospital {医院} --script "out\{医院}\processed_script_{医院}.py" --annotations "out\{医院}\replica_annotations.json" --output-root out --auth-mode scripted --operation capture-build --expand-all-series`
> - Adapter 生成：`D:/Anaconda/envs/codegen-marker/python.exe agent.py out\{医院}\processed_script_{医院}.py -o out\{医院}\completed_{医院}.py`
> - run 目录：`out/{医院}/runs/{run_id}/`（pipeline_report.json 是事实来源）
