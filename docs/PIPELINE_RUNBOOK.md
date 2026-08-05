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

点「⚙️ 生成 Adapter + 离线复刻」，选择登录方式后点击。GUI 以子进程启动
`pipeline_orchestrator.py`，事件流（`auth_required` / `auth_completed` /
各阶段事件 / `completed`）实时回显到面板。

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

`--operation`：`full | adapter-only | replica-build | offline-validation`。
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
