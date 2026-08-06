# 真实站点冒烟测试清单（Real-Site Smoke Checklist）— 可执行核对手册

在真实 DICOM Web viewer 上按「生成 Adapter + 离线复刻」一键管道跑全流程的**发布门槛验收**。
三类 viewer **分开记录**：弹出窗（popup）类、嵌套 iframe 类、FTImage 类。三类各至少通过一次才满足
设计规格 §16 发布门槛（FTImage 以收缩后 critical 集通过即记为发布门槛通过，其 `run.status` 仍按 §10.0 为 `partial`）。

> **保密约定**：本文件任何一行都**不得**包含病人数据或真实 token。
> 站点标识使用脱敏后的代号（如 `uicloud-demo`、`cxhospital-sandbox`）。
> 测试完成后如含敏感观察，只登记类别与代号，不复制实际影像/姓名/凭据。
> 执行前请先读 `docs/PIPELINE_RUNBOOK.md`；术语与产物路径以它为准。

---

## 0. 每个站点开始前的通用准备（每次都要做）

- [ ] **环境就绪**：确认 `D:/Anaconda/envs/codegen-marker/python.exe` 存在，
      `playwright install chromium` 已装。
- [ ] **凭据在手**：所需的账号 / 验证码处理方式已确定；选择 `auth mode`：
  - `scripted`：录制脚本内含登录动作，管道自动回放（认证超时默认 300s）。
  - `interactive`：录制含登录前动作，现场采集时由操作者在弹出的浏览器手工登录，
        完成后在 GUI 点「登录完成，继续」（子进程 `continue_after_auth`）。
  - `storage-state`：命令行传入已保存的浏览器状态文件 `--storage-state <path>`（跳过登录）。
- [ ] **机器人数据**：已从 `.env.example` 复制 `.env` 并填入 LLM 配置（Adapter 生成需要 LLM）。
- [ ] **已授权**：本次对站点的访问与录制已获授权，用于验证与回归。
- [ ] **博客/笔记**：准备「site 脱敏代号」+「run ID」记录位（见各表列 3 / 列 5）。

### 执行路径（两条等价，任选其一）

**方式 1 — GUI 一键（推荐，主路径）：**

1. `D:/Anaconda/envs/codegen-marker/python.exe main_gui.py`
2. 填站点 URL →「启动录制」→ 在浏览器里录制登录 + 关键操作。
3. 需要后续处理的点（报告截图 / 序列选择 / Meta 信息 / 影像画布等）→ GUI 面板右键「插入标记」。
4. 「停止录制」→「保存处理后代码」（写 `out/{医院}/processed_script_{医院}.py`）。
5. 点「⚙️ 生成 Adapter + 离线复刻」→ 选登录方式 → 运行。
6. 观察阶段事件流；`interactive` 时在弹窗完成登录后点「登录完成，继续」。

**方式 2 — 命令行（可脚本化 / 复现）：**

```bash
D:/Anaconda/envs/codegen-marker/python.exe pipeline_orchestrator.py \
    --script out/{医院}/processed_script_{医院}.py \
    --annotations out/{医院}/replica_annotations.json \
    --hospital {医院} --output-root out \
    --auth-mode scripted|interactive|storage-state \
    [--storage-state <path>] [--model <llm>] [--retry 3] \
    --capture-timeout 900 --auth-timeout 300
```

> 注解文件由 GUI 在保存录制后写到 `out/{医院}/replica_annotations.json`（与 processed 脚本同目录、固定名）。
> 若手工复现，可用 `--run-id <已有run_id> --operation adapter-only|replica-build|offline-validation` 只重跑某环节。

### 逐阶段核对清单（每个站点逐项打勾）

- [ ] **① Adapter 生成**：`adapter/completed_{医院}.py` 生成且 `py_compile` 通过；
      报告 `drivers.adapter_generation` 非空；生成阶段无外网/模型失败。
- [ ] **② Live capture**：`capture/manifest.json` 生成；snapshot 含真实状态；
      popup / iframe / 页面拓扑与预期一致。
- [ ] **③ Replica build**：`replica/index.html` + `assets/` + `serve_replica.py` 存在；
      `build_from_manifest` 的 source-hash 门禁通过。
- [ ] **④ Replica 校验（manifest-replay 驱动）**：`replica/replay_replica.py` 执行退出 0；
      critical locator `count()==1` 且可见。
- [ ] **⑤ Offline adapter 校验（completed- adapter 驱动）**：`adapter/completed_{医院}_offline.py` 生成并执行；
      marker 顺序执行，前序 marker 建立的 `seq_frames`/`seq_name` 供后序读取。
- [ ] **⑥ 离线隔离**：`validation/external_requests.json` 为 `[]`（浏览器 route **和** Python 进程级 egress 两道均无外发）。
- [ ] **⑦ 产物校验**：`report.jpeg` 非空；`dicom_meta.json`/`patient_info.json` 可解析非空；
      画布帧（若 `supported`）≥1；缺失时按能力收缩记 `partial` 而非误报失败。
- [ ] **⑧ 隐私校验**：报告/事件流不含凭据、token、cookie、storage-state；`validate_privacy` 无 `privacy_violation`。
- [ ] **⑨ 进程清理**：浏览器 / 本地 server 无残留（GUI 关闭或管道结束后无孤儿进程）。
- [ ] **⑩ 终态**：GUI 显示 `completed.status`；报告 `pipeline_report.json.status` 与其一致。

---

## 通用记录列（每行一个 run）

以下 15 列用于逐项记录一次完整冒烟：

| # | 列 | 说明 |
|---|----|------|
| 1 | date | 测试日期（YYYY-MM-DD） |
| 2 | tester | 执行人代号 |
| 3 | sanitized site identifier | 脱敏后的站点代号（不带 token / 病人数据） |
| 4 | auth mode | `scripted` / `interactive` / `storage-state` |
| 5 | run ID | `runs/` 下的运行 ID |
| 6 | adapter generation | ✅ / ⚠️ / ❌（+ error_category） |
| 7 | live capture | 现场采集是否成功 + 帧数 |
| 8 | popup/frame topology | 弹出窗 vs iframe 及其选择器是否为预期拓扑 |
| 9 | replica build | 静态 Replica 是否成功构建 |
| 10 | offline adapter | 离线 Adapter（`completed_*_offline.py`）是否生成并执行 |
| 11 | external request count | 离线重放过程中的外部请求数（应趋于 0） |
| 12 | artifact validation | `report.jpeg` / `dicom_meta.json` / 画布帧等产物校验 |
| 13 | privacy validation | 脱敏 / 无凭据泄漏检查 |
| 14 | final status | `success` / `partial` / `failed` / `cancelled` |
| 15 | blocker category | 若失败：`authentication` / `adapter_generation` / `replica_build` / `privacy_violation` / `other` |

---

## A. Popup viewer（uicloud 类目标）

**特点**：目标站把 viewer 以 `expect_popup()` 弹出独立窗口；live capture 必须捕获到该 popup 页面。

**A 专属核对（在通用清单之外）：**
- [ ] 录制时确实用 `with page.expect_popup()` 打开 viewer，而不是同页导航。
- [ ] `capture/manifest.json` 中 popup 页面（`page`/`page1`）齐全，`popup/frame topology` 记录为「独立弹窗」。
- [ ] Replica 中 popup 状态可访问（`states/*/pages/p_popup/*` 存在）。
- [ ] 离线 Adapter 能恢复 `page` / `page1` 绑定，marker 操作命中。

**冒烟记录：**

| date | tester | site（脱敏） | auth mode | run ID | adapter generation | live capture | popup/frame topology | replica build | offline adapter | external request count | artifact validation | privacy validation | final status | blocker category |
|------|--------|--------------|-----------|--------|--------------------|--------------|----------------------|---------------|-----------------|------------------------|---------------------|--------------------|--------------|-----------------|
|      |        |              |           |        |                    |              |                      |               |                 |                        |                     |                    |              |                 |

---

## B. Nested iframe viewer（cxhospital 类目标）

**特点**：目标站把 viewer 嵌在嵌套 `<iframe>` 中，需要用 `.locator(...).content_frame` 链定位；
live capture 必须保留 iframe 子文档拓扑。

**B 专属核对（在通用清单之外）：**
- [ ] 录制时通过 Playwright `Frame`（`.content_frame`）访问 iframe 内部 DOM，而非 `contentDocument`。
- [ ] `capture/manifest.json` 中 iframe 父子文档（`documents/d_*_f_*`）齐全；顶层父文档存在。
- [ ] Replica 中 iframe 子文档可访问（`documents/` 下按 frame 组织），不塌缩成 div。
- [ ] 离线 Adapter 的 Meta / 序列 marker 在 iframe 内定位失败时，按能力收缩记 `partial` 而非误报 `failed`。

**冒烟记录：**

| date | tester | site（脱敏） | auth mode | run ID | adapter generation | live capture | popup/frame topology | replica build | offline adapter | external request count | artifact validation | privacy validation | final status | blocker category |
|------|--------|--------------|-----------|--------|--------------------|--------------|----------------------|---------------|-----------------|------------------------|---------------------|--------------------|--------------|-----------------|
|      |        |              |           |        |                    |              |                      |               |                 |                        |                     |                    |              |                 |

---

## C. FTImage viewer

**特点**：动态 marker 多（序列选择 / Meta 信息 / 影像画布）。**canvas 动态帧为 `unsupported` 能力**，
按设计 §6.6.1 收缩后 critical 集**不含动态帧**；因此只要收缩后的 critical 集全部通过即记为**发布门槛通过**，
但该 run 的 `pipeline_report.json.status` 仍按 §10.0 为 `partial`（与「发布门槛通过」不冲突）。

**C 专属核对（在通用清单之外）：**
- [ ] 序列选择 marker 生成的 `seq_frames` / `seq_name` 建立成功，供后序 Meta / 画布 marker 读取。
- [ ] Meta 信息 marker 产出的 `dicom_meta.json` / `patient_info.json` 可解析非空。
- [ ] 影像画布 marker 在离线只验证定位/聚焦/点击（`canvas_locate_focus_click`=supported），
      动态帧像素变化（`canvas_dynamic_pixels`=unsupported）按能力收缩**不**计入 success 判定。
- [ ] 预期终态为 `partial`（收缩后 critical 通过 + 动态帧 unsupported），发布门槛仍记通过。
- [ ] 记录每个 marker 的 `success/partial/failed/skipped` 明细（取自报告 / summary）。

**冒烟记录：**

| date | tester | site（脱敏） | auth mode | run ID | adapter generation | live capture | popup/frame topology | replica build | offline adapter | external request count | artifact validation | privacy validation | final status | blocker category |
|------|--------|--------------|-----------|--------|--------------------|--------------|----------------------|---------------|-----------------|------------------------|---------------------|--------------------|--------------|-----------------|
|      |        |              |           |        |                    |              |                      |               |                 |                        |                     |                    |              |                 |

---

## 发布门槛判定（三个站点**各至少通过一次**）

- [ ] **A. Popup viewer**：见上表 A，`final status` ≥ `partial` 且收缩后 critical 通过；`privacy ✅`。
- [ ] **B. Nested iframe viewer**：见上表 B，同上。
- [ ] **C. FTImage**：见上表 C，收缩后 critical 集通过即记发布门槛通过（`run.status` 允许为 `partial`）。

## 验收判定（每个 run）

- **privacy validation** 必须为 ✅ 才视为可交付：报告/事件流脱敏、无真实 token / 病人数据进入本仓库文档。
- **external request count** 在离线 Adapter 重放阶段应稳定为 0（或仅本地 `serve_replica.py` 的本地请求）——
  **两道都要为 0**：浏览器 `context.route` 与 Python 进程级 egress（`socket`/`urllib`/`requests`）。
  任一 `offline_external_request` → 记 `failed` 并阻断。
- 任一 run 出现 `privacy_violation` → 立即阻断，先修管道再继续冒烟。
- **blocker category** 仅登记稳定错误分类（`authentication` / `adapter_generation` / `replica_build` /
  `privacy_violation` / `network` / `selector_failure` / `offline_external_request` / `other`），
  并把「未通过项明确记录为发布 blocker」回填到本页相应表行。

### 回填本文件的方式

冒烟完成后，把每次 run 的结果**只登记脱敏指标**（上表 15 列）回填到对应的站点表格行
（可新增多行表示同一站点的多次 run）。**绝不**填入下面任何一项：真实病人姓名、检查号、accession、真实 token、cookie、
storage-state 路径、带 query 值的真实 URL、截图/影像本件。
