# 多医院 Web 复刻操作手册（Multi-Hospital Replica Runbook）

> 本手册是把一个 DICOM 影像 Web 应用录制成「可离线复现的静态 Replica 站点」的**完整、可逐步照做**流程，
> 面向需要在一家以上医院（如 ft / 中山 / 联影）反复复刻的场景。
> 单项命令与产物语义参见 [`PIPELINE_RUNBOOK.md`](PIPELINE_RUNBOOK.md)。

---

## 0. 核心概念（先读，避免误解）

**Replica（离线静态 web）和 Adapter（completed 脚本）是两个独立的产物，互不依赖：**

| | Replica（你点开看的 web） | Adapter（completed_offline.py） |
|---|---|---|
| 是什么 | 浏览器打开的静态 HTML + 截图 + 交互 overlay | 一个可执行的 Python 脚本，自动驱动采集/校验 |
| 依赖 | 只需 **capture（浏览器采集）→ build（构建）** | LLM 生成 |
| 是否烧 API | **否** | **是**（每次生成都要调 LLM） |

> 结论：**跑「复刻 web」不需要 adapter，不烧 LLM API。** adapter 只在你要"用脚本自动驱动复刻/采集"时才需要，应单独按需生成。
> 这对应管道里两种操作：
> - **只复刻（跳过 adapter）** = `capture` + `build` + replica 校验（快、无 API）
> - **完整（adapter + 复刻）** = 上面再加 adapter 生成与校验

---

## 1. 三家医院速览（你要复刻的目标）

| 医院 | URL | 认证方式 | 复刻注意点 |
|---|---|---|---|
| **ft** | `https://yyx.ftimage.cn/dimage/index.html?stm=...` | URL 带 `stm=` 会话 token | 直接进 viewer，序列/Meta 都在主文档 |
| **中山** | `https://zscloud.zs-hospital.sh.cn/film/#/shared?code=<分享码>` | URL 带 `code=` 分享码 | 会重定向到**分享视图**；**点「查看影像」转新窗口**；要做**布局切换** |
| **联影** | `https://uicloud.com/film/#/<检查标识>` | 裸 URL（免登录） | 先到**检查列表**；**点「查看影像」转新窗口**；要做**布局切换** |

> ⚠️ 三家 URL 中的 `stm=` / `code=` 是**临时会话凭证，会过期**。过期后复刻 capture 会报 `authentication` 失败，
> 需重新拿有效凭证（新录制或在 URL 里换新 token）。

---

## 2. 录制（GUI，每家各自一次）

### 2.1 启动 GUI

```bash
D:/Anaconda/envs/codegen-marker/python.exe main_gui.py
```

### 2.2 每家填的两个框

| 医院 | URL 框 | 「输出文件」框 |
|---|---|---|
| **ft** | `https://yyx.ftimage.cn/dimage/index.html?stm=<有效token>` | `D:\00-Project\04-codegencopy\out\ftimage\processed_script_ftimage.py` |
| **中山** | `https://zscloud.zs-hospital.sh.cn/film/#/shared?code=<分享码>` | `D:\00-Project\04-codegencopy\out\zscloud\processed_script_zscloud.py` |
| **联影** | `https://uicloud.com/film/#/<检查标识>` | `D:\00-Project\04-codegencopy\out\uicloud\processed_script_uicloud.py` |

> 输出目录无需手动建，保存时自动创建。
> GUI 从「输出文件」的父目录名识别医院（`out/{医院}/...`），复刻时路由到对应产物。

### 2.3 录制动作与标记（每家要点）

**通用动作**：录完一个动作后，回到 GUI 面板，把光标放在该动作行，**右键 → 插入标记** → 选类型。

| 标记类型 | 用途 | 对应动作 |
|---|---|---|
| 报告截图 | 报告页截图 | 打开报告 |
| 序列选择 | 序列列表 | 点序列项 |
| Meta 信息工具 | DICOM 信息面板 | 打开 Tags/DICOM 信息 |
| 影像画布交互 | 画布翻页/缩放 | 在画布操作 |
| 序列布局切换 | 布局（1x1/1x2/多宫格） | 点布局按钮 |
| 窗宽窗位 WL/WW | 窗宽窗位 | 输入窗宽窗位 |

**🥇 最重要的一条（popup）**：
> 中山/联影**点「查看影像」会转跳到新窗口（弹窗）**。**点完后就在新弹出的窗口里继续操作**（开序列、开 Meta、布局切换），**不要切回旧窗口** —— 录制器会自动把它记成 `expect_popup` 结构，复刻管道原生支持 popup 转跳。

**各家录制清单：**

- **ft**：进页面 → 开序列（插「序列选择」）→ 开 Meta 面板（插「Meta 信息工具」）→ 布局切换（插「序列布局切换」）→ 画布翻页（插「影像画布交互」）→ 停止
- **中山**：进分享视图 → **点「查看影像」→ 在新窗口**做布局切换（插「序列布局切换」）、开 Meta（插「Meta 信息工具」）、选序列/翻页（插「序列选择」/「影像画布交互」）→ 停止
  - ⚠️ 若分享视图点不动「查看影像」/开不了面板：这是**分享视图受限**，需换登录态进入完整 viewer 的方式，单独处理。
- **联影**：进检查列表 → **点某检查「查看影像」→ 在新窗口**做布局切换、开 Meta、选序列/翻页 → 停止

### 2.4 保存

点「💾 保存处理后代码」→ 自动落盘：
- `out/{医院}/processed_script_{医院}.py`
- `out/{医院}/replica_annotations.json`（marker 稳定映射，与脚本通过 `source_script_sha256` 强绑定）

> ⚠️ **改过脚本必须重新点保存**，否则 `replica_annotations.json` 的 sha 会和脚本对不上，管道拒绝运行。

---

## 3. 复刻（跑管道）

### 3.1 方式 A：只复刻，跳过 Adapter（不烧 API）

> 已落地为 `--operation capture-build`（新 run 操作，不接受 `--run-id`）。用法：
>
> ```bash
> D:/Anaconda/envs/codegen-marker/python.exe pipeline_orchestrator.py `
>   --hospital {医院} `
>   --script "D:\00-Project\04-codegencopy\out\{医院}\processed_script_{医院}.py" `
>   --annotations "D:\00-Project\04-codegencopy\out\{医院}\replica_annotations.json" `
>   --output-root out --auth-mode scripted --operation capture-build
> ```
>
> 只跑 `preflight → 现场采集 → 复刻构建 → 复刻校验`，**跳过 adapter**，产出 `replica/index.html`。
> **该 run 不会生成 completed adapter**。**同一 run 如需 adapter 驱动的离线校验**，先 `adapter-only`
> （生成 `completed_{医院}.py`），再 `offline-validation`（配合 gate 修复后同 run 闭环可行；
> `offline-validation` 的 resume gate 前置是 `adapter/completed_{医院}.py`，非固定名
> `completed_offline.py`）；`full` 是**另开新 run** 的完整流程，不是给当前 run 补产物。
> 若不需要 adapter，则 capture-build 已足够。

### 3.2 方式 B：完整（adapter + 复刻）

```bash
D:/Anaconda/envs/codegen-marker/python.exe pipeline_orchestrator.py `
  --hospital {医院} `
  --script "D:\00-Project\04-codegencopy\out\{医院}\processed_script_{医院}.py" `
  --annotations "D:\00-Project\04-codegencopy\out\{医院}\replica_annotations.json" `
  --output-root out --auth-mode scripted --operation full
```

> `--auth-mode`：`scripted`（脚本登录）/ `interactive`（手动）/ `storage-state`（浏览器状态文件，最稳，推荐对带登录态的医院维护持久登录态）。
> `--operation`：`full | capture-build | adapter-only | replica-build | offline-validation`。

**判断成功**：最后输出 `{"event":"completed","entrypoint":"...replica\index.html"}`。失败会打印 `error_category`
（`authentication` / `adapter_generation` / `replica_build` / `privacy_violation` 等），据此定位。

---

## 4. 打开 / 验证 Replica

```bash
D:/Anaconda/envs/codegen-marker/python.exe "out\{医院}\runs\{run_id}\replica\serve_replica.py"
```
浏览器打开打印的 URL，实际点 Meta / 序列 / 布局 / 画布验证还原。

报告：`out/{医院}/runs/{run_id}/pipeline_report.json`（JSON 事实来源）同目录 `.html`（可读渲染）。

---

## 5. 重跑（rerun，不用重录、不烧 API）

用 run 目录内的稳定产物单独重跑某环节：

```bash
# 仅重建 Replica（已有 capture）
... --script out/{医院}/runs/{run_id}/source/processed_script_{医院}.py \
    --annotations out/{医院}/runs/{run_id}/source/replica_annotations.json \
    --hospital {医院} --output-root out --run-id {run_id} --operation replica-build

# 仅重生成 Adapter（要调 LLM）
... --run-id {run_id} --operation adapter-only

# 仅重跑校验（需 completed adapter + capture + replica）
... --run-id {run_id} --operation offline-validation
```

---

## 6. 本次已修复的问题（为什么现在能跑通）

| 提交 | 修复 | 影响 |
|---|---|---|
| `5c2e2d4` | codex 实现 metadata 面板渲染/嵌套 frame/稳定等待/close 放置 | 复刻能捕获并渲染完整 Meta 面板 |
| `ad6b27a` | Meta 面板不稳定时**不再丢弃整个 action** | 面板捕获不到时复刻不整体失败 |
| `c302c64` | 复刻校验改用 `data-replica-action`，**不再依赖会被安全清洗删掉的语义 locator**；修跨 state carry-forward 误判 | **任何医院、任何选择器都能过校验** |
| `40e8082` | 子进程强制 UTF-8 stdio（修 `gbk codec can't encode` 崩溃） | GUI 触发 adapter 生成不再崩 |

> 复刻捕捉到的目标元素，其 `href/role/title` 等交互属性会被安全清洗删掉（防注入）。
> 校验层已改为按 build 注入的 `data-replica-action` overlay 校验，因此不依赖这些属性 —— 这是"对不同 viewer 鲁棒"的关键。

---

## 7. 各医院 viewer 选择器差异（为什么有时要补适配）

三家 hits 面/序列/画布的选择器差异很大：

| | Meta 面板 | 画布 |
|---|---|---|
| ft | `#tagsBox` | canvas |
| 联影 | `#popTagText_WL`、`DICOM信息 F2` | `#overlaycanvas-0_0` |
| 中山 | 待录制确认 | 待录制确认 |

复刻后端靠一份**全局候选选择器**（`capture_snapshot.py` 的 `_MARKER_REGION_CANDIDATES`）去匹配面板。
**若某医院的某块覆盖不到（面板捕获不全），才需要针对该医院补选择器适配**（计划中：把候选做成按 viewer 可配置，如复用 `skills/_shared/viewers.yaml`）。
绝大多数情况下靠通用兜底 + 录制时 marker 位置即可跑出可用复刻 —— **先录了跑，卡住再补**。

---

## 8. 建议的执行顺序

1. **ft**（最熟、链路已验证）先录+跑通，确认整套流程 OK（若只需看复刻，用"只复刻"方式，不烧 API）。
2. **联影**（裸 URL 最简单，多一步点查看影像 + 布局切换）。
3. **中山**（分享视图，最不确定，放最后；确认分享视图能否完整操作 viewer）。

> 每家录完先跑**只复刻**验证面板/序列/布局捕获取没取全，取全再决定是否需要为那家补选择器适配或生成 adapter。
