# Pipeline 运行手册（Runbook）

本手册说明如何把一份带 marker 的录制脚本，通过「生成 Adapter + 离线复刻」一键管道，
生成可在本机离线复现的 Adapter + 静态 Replica 站点 + 确定性验证报告。

## 1. 前置条件

- Conda 虚拟环境 `codegen-marker`（Python 3.11 + PyQt6 + playwright），解释器路径
  `D:/Anaconda/envs/codegen-marker/python.exe`。
- 浏览器已安装：`D:/Anaconda/envs/codegen-marker/Scripts/playwright.exe install chromium`。
- `.env` 已从 `.env.example` 复制并填入 LLM 配置（Adapter 生成阶段需要 LLM）。

## 2. 录制与标记

1. 运行 `D:/Anaconda/envs/codegen-marker/python.exe main_gui.py`。
2. 填写目标 DICOM Web 应用 URL（如 `https://uicloud.com/film/...`），点「启动录制」。
3. 在浏览器中录制登录与访问操作；在需要后续处理的位置（报告截图、影像画布、Meta 信息工具等）
   于 GUI 面板**右键 → 插入标记**。
4. 点「停止录制」，面板进入自由编辑模式。

## 3. 保存要求

一键运行前**必须先保存**处理后脚本：点「保存处理后代码」写入
`out/{医院}/processed_script_{医院}.py`。GUI 的「生成 Adapter + 离线复刻」按钮
只有在脚本已保存、且含至少一个 marker、且已停止录制时才可用。
该脚本与 `replica_annotations.json`（由 GUI 生成、含 marker → 源码行的稳定映射）
共同作为管道输入。

## 4. 一键操作（GUI）

点「⚙️ 生成 Adapter + 离线复刻」，选择登录方式与运行方式（`完整（Adapter + 复刻）` /
`只复刻（跳过 Adapter）`）后点击。GUI 以子进程启动 `pipeline_orchestrator.py`，事件流
（`auth_required` / `auth_completed` / 各阶段事件 / `completed`）实时回显到面板。

> **运行方式**：选「只复刻」→ `--operation capture-build`，仅现场采集 + 构建复刻，跳过 adapter
> （不烧 LLM API，也不生成 completed adapter）；GUI 状态文案动态显示「正在生成离线复刻…」。

### 登录方式

| 模式 | 说明 |
|------|------|
| `scripted`（脚本登录） | 录制脚本内含登录动作，管道自动回放；认证超时（`--auth-timeout`，默认 300s）则失败。 |
| `interactive`（手动登录） | 管道弹出浏览器让操作者手工登录；完成后点「登录完成，继续」向子进程发 `continue_after_auth`。 |
| `storage-state` | 直接传入已保存的浏览器状态文件 `--storage-state <path>`，跳过登录（供非 GUI / 命令行场景）。 |

## 5. 运行目录结构

每次运行生成独立 run 目录，稳定产物名可被断点重跑：

```
out/{医院}/runs/{run_id}/
├── source/            # source 录制脚本副本 + replica_annotations.json
├── adapter/           # completed_{医院}.py → completed_{医院}_offline.py
├── capture/           # capture/manifest.json + 逐帧截图证据
├── replica/           # index.html + assets + serve_replica.py
├── validation/        # dicom_meta.json + patient_info.json + 校验产物
├── logs/              # 管道日志
├── pipeline_state.json      # 运行状态（断点续跑依据）
├── pipeline_events.jsonl    # 逐事件 JSONL（脱敏）
├── pipeline_report.json     # 确定性报告（事实来源）
└── pipeline_report.html     # 报告的可读 HTML 渲染
```

固定产物名：报告截图 `report.jpeg`、DICOM 元数据 `dicom_meta.json`，不带时间戳。

## 5b. 多序列探索（可点击 Replica 全序列采集）

当 `expand_all_series=true` 时，管道在 live capture 阶段（同一已登录浏览器会话内）自动
**发现并按序串行采集**其余可发现序列，使离线 Replica 中每个成功序列都可点击。

### 配置项与预算默认值

| 配置项 | 默认 | 说明 |
|---|---|---|
| `expand_all_series` | `false` | 是否开启全序列探索；默认关闭，旧录制行为逐字节不变 |
| `max_series` | `40` | 最多采集的序列条数（硬上限，超出的不尝试） |
| `per_series_timeout_s` | `20` | 单序列切换就绪 / Metadata 稳定超时（秒） |
| `total_series_timeout_s` | `900`（10 分钟） | 整次探索的总时间预算（秒） |
| `viewer_capture_mode` | `first_stable_frame` | MVP 唯一实现/唯一可用值 |

命令行示例（orchestrator / `batch_capture_replicate.py --mode capture-only`）：

```bash
... --expand-all-series --max-series 40 --per-series-timeout 20 --total-series-timeout 900
```

### 预算与速率（执行侧强制）

- **串行**：同一 Page 上按 ordinal 顺序逐条采集，**绝不并行点击多个序列**；每次切换后
  用「组合证据」（selected 态 / 名称匹配 / canvas hash 变化 / DOM 稳定 / 截图非空）等待稳定，
  不以固定 sleep 作为唯一就绪条件。
- **单序列超时**：`per_series_timeout_s` 作用在该条序列的切换就绪与 Metadata 稳定上；
  超时只降级该条（Metadata 没稳定 → `partial`），不影响其它序列。
- **总预算**：达到 `total_series_timeout_s` 后停止启动新 branch，剩余 descriptor 标
  `skipped_budget` / partial，不静默从 discovered 列表删除。
- **单序列一个稳定 Viewer 截图**：每条成功序列只保存一张稳定 Viewer 截图；重复视觉资产
  在构建阶段按 SHA-256 内容哈希去重（`assets/by-hash/{sha256}`，同内容只存一份）。

### 产物目录（敏感医疗数据，绝不提交）

探索证据写在本 run 的 capture 树内（已被 `out/` 与 `.gitignore` 的 `out/**/series_branches/`
覆盖）：

```
out/{医院}/runs/{run_id}/capture/
└── series_branches/
    ├── series_capture_manifest.json   # 计数守恒 + 每 branch 终态 + 预算/恢复警告
    └── {safe_series_key}/            # 内部 hash slug，不含原始 UID/患者名/检查号
        ├── descriptor.json
        ├── viewer/  metadata/  metadata_rows.json  status.json
```

`safe_series_key` 只嵌入 ordinal + 稳定身份的 SHA-256 前缀；**原始 SeriesInstanceUID、患者姓名、
检查号、token 绝不进入文件名、日志、事件或公开报告**。事件流与公开产物只落 `<hash 前缀>` 与
解析行（`series_key_sha256`），不落 Metadata 原文到事件流；完整（已剔可执行/credential/remote
属性）的 Metadata 面板 DOM 只作为本地受限敏感产物写入 `metadata/metadata_rows.json` 并复刻到
served Metadata 面板——见下节「患者数据保护注意」的豁免边界。

### complete / partial / failed 语义（整次探索）

- **complete**：`reached_end=true` 且无 partial/failed/skipped_budget。
- **partial**：列表未枚举到底、某 branch 为 partial/failed、预算耗尽 `skipped_budget`、或
  受控恢复失败——任何一条都使整次探索诚实降级为 partial。
- **failed**：没有任何可用 Viewer branch，或探索基础设施失败。

单条 branch 终态：`captured`（有可用 Viewer + 按要求 Metadata）/ `partial`（Viewer 成功但
Metadata 未变化等）/ `failed`（无可用 Viewer）/ `skipped_budget`（预算耗尽未尝试）/
`skipped_duplicate`（身份重复跳过）。守恒律 `captured+partial+failed+skipped == discovered`
由 `series_capture_manifest.json` 的 `count_conserved` 强制并校验。

### 断点 / 失败恢复边界

- MVP **只支持整个 exploration 重跑**，不做 branch 级 resume；不跨患者 / 检查复用 snapshot。
- 探索期因序列列表 hub 连续无法恢复时，允许**一次**受控 reload/bootstrap 恢复；第二次仍失败
  即停止并标记 overall partial/failed。恢复发生会在 `series_capture_manifest.json` 记录
  `reloaded: true` 与 `series_reload_recovered_once` 审计 warning。

### 患者数据保护注意

- 日志、`pipeline_events.jsonl`、`pipeline_report` 与 `pipeline_report.html` **不含**患者姓名、
  检查号、原始 SeriesInstanceUID 或 Metadata 原文；URL / token 属性沿用 `sanitize_html()` 与
  URL redaction（`replay_helpers.redact_url` / `scan_text_for_secrets` 守卫）。
- **served Replica 的 Metadata 面板是本地受限敏感产物**：按产品目标展示完整的（已剔除可执行 /
  credential / remote 属性）Metadata DOM，其文本可能含患者 / 序列身份值。因此 route / event / log /
  公开报告与其余 served 面一律只用 `series_key_slug()` / SHA-256 哈希，绝不含原始 UID / 患者派生 key；
  Metadata 面板文本是该边界的明确豁免项，只作本地受限产物保留完整。
  `pipeline_validation.validate_series_privacy` 会扫描 route/event/log 与 served（非 Metadata）面，
  并核对 Metadata 面板仍完整可读。
- capture 原始产物、截图与 metadata 仅限本地；`out/`、`capture/`、`spy/`、`snapshots/`、
  `replicas/`、`annotations/`、`out/**/series_branches/` 均在 `.gitignore` 覆盖内，不进入版本控制。

## 6. 状态语义

| 状态 | 含义 |
|------|------|
| `success` | 所有阶段（Adapter 生成、现场采集、Replica 构建、Replica 校验、离线 Adapter 校验）全部通过。 |
| `partial` | 管道跑通但存在非阻断项（部分指标降级 / 部分 marker 通过）。 |
| `failed` | 任一关键阶段失败，按 `error_category` 归类（`authentication`、`adapter_generation`、`replica_build`、`privacy_violation` 等）。 |
| `cancelled` | 操作者主动取消（GUI「取消」或 stdin `{"command":"cancel"}`）。 |

## 7. 打开 Replica

在 run 目录下启动内置静态副本服务并打开 `replica/index.html`：

```bash
D:/Anaconda/envs/codegen-marker/python.exe out/{医院}/runs/{run_id}/replica/serve_replica.py
```

Replica 是纯静态站点（HTML + 截图 + 内联状态），可在无网络、无浏览器目标站的环境下
离线检查页面结构与序列布局。

## 8. 打开报告

```bash
# 事实来源（JSON）
# out/{医院}/runs/{run_id}/pipeline_report.json
# 可读渲染（HTML）
# out/{医院}/runs/{run_id}/pipeline_report.html
```

HTML 报告只渲染已脱敏 JSON 字段 + 本地相对产物链接，不内联病人数据或图片。

## 9. 重跑操作（rerun）

从稳定产物可单独重跑任一环节，无需重新录制或重新调用 LLM：

```bash
# 仅重新生成 Adapter
D:/Anaconda/envs/codegen-marker/python.exe pipeline_orchestrator.py \
    --script out/{医院}/runs/{run_id}/source/processed_script_{医院}.py \
    --annotations out/{医院}/runs/{run_id}/source/replica_annotations.json \
    --hospital {医院} --output-root out --run-id {run_id} --operation adapter-only

# 仅重新构建 Replica（需已有 capture）
... --operation replica-build

# 仅重跑离线验证（需 completed adapter + capture + replica）
... --operation offline-validation
```

`--operation`：`full | capture-build | adapter-only | replica-build | offline-validation`。

- **`full` / `capture-build` 是「新 run」操作**，不接受 `--run-id`（在 §4 / 或首次跑复刻的 CLI 用）。
  `capture-build` = `preflight → 现场采集 → 复刻构建 → 复刻校验`，**跳过 adapter**（不烧 LLM API），
  也**不生成** `completed_{医院}.py`。
- **`adapter-only` / `replica-build` / `offline-validation` 是「重跑」操作**，必须带 `--run-id` 续跑既有 run。
- **`offline-validation` 的 resume gate** 前置文件是 `adapter/completed_{医院}.py`（医院真名，非固定
  `completed_offline.py`；离线 runner 由该 stage 现场生成）。同一 run 想闭环 adapter 驱动的离线校验，
  先 `adapter-only` 生成 `completed_{医院}.py`，再 `offline-validation`。

`--auth-mode`：`scripted | interactive | storage-state`。
命令行可选 `--model`（LLM 模型）、`--retry`（Adapter 重试次数，默认 3）、
`--capture-timeout`（现场采集超时，默认 900s）、`--auth-timeout`（默认 300s）。

## 10. 隐私注意

- **截图**：`report.jpeg`、画布帧、`dicom_panel_fallback.jpeg` 可能含患者影像/姓名，
  仅限本地验证与归档，不得外发或上传公共仓库。
- **Metadata**：`dicom_meta.json` / `patient_info.json` 含 Patient / Study / Series 标签；
  写入报告事件流前会被脱敏。
- 管道日志与 `pipeline_events.jsonl` 在写入前脱敏（URL 重写、凭据字段以 `REDACTED` 替代）。
- 不要在本仓库任何文档中复制真实 token 或病人数据。

## 11. `.env` 键位

复制 `.env.example` 为 `.env` 并填入真实值（以下仅为键位文档，**不含真实值**）：

| 键 | 用途 |
|----|------|
| `LLM_API_KEY` | LLM 服务 API Key（必填，Adapter 生成阶段需要） |
| `LLM_BASE_URL` | LLM 服务 Base URL（缺省 `https://api.openai.com/v1`） |
| `LLM_MODEL` | 默认补全模型名（缺省 `gpt-4o`） |
| `LLM_MAX_TOKENS` | 单次补全最大 token 数（可选，缺省 `12000`） |
| `VL_API_KEY` / `VL_BASE_URL` / `VL_MODEL` | 视觉模型专用配置（可选，缺省回退到 `LLM_*`） |
