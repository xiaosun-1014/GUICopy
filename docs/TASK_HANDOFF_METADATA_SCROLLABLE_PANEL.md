# 【任务交接】复刻站「全量 Meta 面板 + 关闭返回」修复

> 用途：交接给下一位模型（**多模态版**，能看图/看截图）。本文档汇总当前状态、已改动、剩余工作与验证方式。
> 项目根：`D:/00-Project/04-codegencopy`（Playwright Codegen 复刻工具，PyQt6）。
> 关键环境：`D:/Anaconda/envs/codegen-marker/python.exe`（不要用裸 `python`/`pip`）。

---

## 1. 背景与目标

用户录制了 ftimage 医院（免登录，URL 带 `stm` token），跑复刻管道生成静态复刻站。目标是：**复刻站里点开「Tags / Meta 信息面板」后，能看到完整 DICOM 元数据（Patient/Study/Series/Instance/Image/Equipment/UIDS 全部分组、约 40+ 行），并能上下滚动查看全部、且有「关闭 ×」回到报告页的交互。**

实测复刻站地址（后台 http.server 已起过，端口视启动时的日志）：
- 入口：`out/ftimage/runs/20260807T012243Z-d43b70/replica_v2/index.html`
- Tags 面板状态页：`.../replica_v2/states/s_003/index.html`

---

## 2. 已确认的事实（不要再猜测）

1. **Meta/Tags 面板是「全屏浮层」**，不是小弹窗。capture 到的 rect = `0,0,1696,880`（铺满整个报告画面）。
2. **状态图是线性单链**，且**没有「关闭 Tags → 回到 s_002」的现成跳转**：
   ```
   s_000(a_001_001)→s_001(a_001_002)→s_002(a_002_001,点Tags)→s_003(打开Tags)
   →s_004(a_002_002 空链接)→s_005(窗宽窗位)→…→s_011
   ```
   录制里没有录「关闭 Tags」这一步（`a_002_002` 是个空文本链接 `.filter(has_text=re.compile(r"^$"))`）。
3. **当前模型的非多模态问题**：无法看图（`Read` 图片返回 `[Unsupported Image]`）。你（多模态）可以直接读取 `*.jpeg` 截图看画面。
4. **s_003 的整页截图**已经包含 Tags 浮层的完整画面（原始 `d_p_000_root.png` 158KB，在 `capture_v2/snapshots/a_002_001/after/assets/`）。所以「展示全量 Meta」最简单的路径 = 让 s_003 用真实截图当背景，不要用任何 DOM 覆盖面板去遮住它。
5. 失败根因（已定位）：之前实现的 metadata「白底全覆盖 `replica-panel`」把 s_003 的整页截图**盖住了**，导致「整个界面全是数据、没有 x、无法回到报告页」。

---

## 3. 已完成的代码改动（工作区未提交）

涉及 3 个文件 + 2 个测试（都已进入工作区但**尚未 git commit**）：

| 文件 | 改动 | 状态 |
|---|---|---|
| `replica_models.py` | `InteractionRegion` 加字段 `full_html: str \| None = None` | 已改 |
| `capture_snapshot.py` | 新增 `capture_marker_panel_region()`；扩展 Meta 候选选择器（`[id*='tags']`/`[class*='tags']`/`[id*='info']`/`[class*='info']` 等）；`capture_marker_interaction_region` 对 metadata region 走通用面板捕获 | 已改 |
| `build_replica.py` | ① **移除**了白底全覆盖 metadata panel 渲染（原 107-128 行已删）；② `_render_document` 新增 `back_target` 参数，非默认时在页面右上角注入 `× 关闭` 按钮（`data-replica-back="<URL>"`）；③ 主循环为每个非入口 state 计算 `back_abs`（前一 ordinal state 的入口页绝对路径），主文档渲染时传入 back_target | 已改 |
| `test/test_capture_snapshot.py` | 新增 `test_metadata_panel_captures_full_container_from_candidates` | 已改 |
| `test/test_build_replica.py` | 新增 `test_metadata_region_renders_scrollable_panel_with_full_html`（**注意：这个测试可能因「移除白底 panel」而需要更新/重写**，见 §4） | 已改 |

另外有**一笔必要修复已单独 commit**（`11e47b6`）：`pipeline_orchestrator.py` 的 capture manifest 路径 bug（相对 entrypoint 拼接错误导致 capture 误判网络失败）。功能和测试的未提交改动是另两个 commit 的候选。

> 当前 `git log` 顶部是：
> ```
> 9bbb2c3 feat: capture and render scrollable full-HTML metadata panels in replica   ← 已提交(旧方案:白底面板)
> 11e47b6 fix: anchor capture manifest path to capture_dir...
> ```
> `9bbb2c3` 是**旧白底方案**的提交；§3 的 build_replica 改动是在它基础上**移除白底 + 加返回按钮**的增量，**尚未提交**。

---

## 4. 剩余要做的（按顺序）

### 4.1 更新/重写 build_replica 的 metadata 渲染测试
- 因为已移除「白底 metadata panel 渲染」，`test_build_replica.py::test_metadata_region_renders_scrollable_panel_with_full_html` 现在预期会失败（它断言 `data-replica-panel` 和 `overflow-y:auto`，而那块渲染已删）。
- 应改写为新行为测试：构造一个带 `back_target` 的非入口状态，断言页面 HTML 里出现 `<button data-replica-back="...">`，且**不再出现** `data-replica-panel`。
- 参考 `build_replica._render_document` / `build_replica` 主循环的传参方式。

### 4.2 让 `replica_runtime.js` 处理 `[data-replica-back]` 点击
- 当前 RUNTIME（`build_replica.py` 顶部的 `RUNTIME` 常量）只处理 `[data-replica-action]`。点击 `× 关闭` 按钮需要跳转。
- 在 RUNTIME 的 `click` 监听里加分支：命中 `[data-replica-back]` → `event.preventDefault(); window.top.location.assign(element.dataset.replicaBack)`。
- 之后重新运行 `build_from_manifest`（见 §5）验证 s_003 页面点「关闭」能回到 s_002。

### 4.3 （可选但推荐）核对 s_003 截图的正确性（多模态优势）
- 用 `Read` 看 `out/ftimage/runs/20260807T012243Z-d43b70/replica_v2/states/s_003/index.html` 渲染时引用的 `.replica-bg` 截图（构建后是 `assets/by-hash/*.jpeg`），确认它是「Tags 浮层 + 报告」的完整画面，且能看清 40+ 行 tag。
- 若截图不够清晰/想增强可读性，可考虑：保留截图背景 + 叠加一个**半透明、右上角滚动条**的面板容器（不遮住截图），或提供缩放。但**优先保证「不遮整页、能回退」**。
- 判断叠加物是否会挡字/挡点，可直接看图确认。

### 4.4 重新构建复刻站并检查
- 用 `capture_v2`（新代码 capture，含完整 `#tagsBox` full_html）重建一个 `replica_v2b`（不要覆盖已有文件，先建新目录），核对 s_003 页面结构。

### 4.5 提交
- 分两个逻辑提交（项目习惯：单逻辑一个提交，精确文件）：
  1. **bug 修复**（若还有）——如 RUNTIME back 处理。
  2. **功能**：`replica_models.py` + `capture_snapshot.py` + `build_replica.py`（不含白底 panel）+ 两个测试，message 如 `feat: show full metadata on Tags panel with a close-back button in replica`。
- 注意：不要 `git add .`；精确文件。未提交的 `docs/superpowers/specs/2026-08-06-...design.md` 是用户手改稿，**不要动/不要提交**。

---

## 5. 如何重新构建复刻站（验证用，一条命令）

```powershell
cd D:/00-Project/04-codegencopy
$env:PYTHONIOENCODING='utf-8'
D:/Anaconda/envs/codegen-marker/python.exe -c "import sys; sys.path.insert(0,'.'); from batch_capture_replicate import build_from_manifest; from pathlib import Path; RUN=Path('out/ftimage/runs/20260807T012243Z-d43b70'); out=RUN/'replica_v2b'; build_from_manifest(RUN/'capture_v2'/'manifest.json', RUN/'capture_v2', out, source_path=RUN/'source'/'processed_script_ftimage.py'); print('built ->', out)"
```

然后用一个不依赖 stdin 的静态服务（`serve_replica.py` 在后台会因 input() EOF 退出，所以用 python http.server 后台常驻）：

```powershell
cd out/ftimage/runs/20260807T012243Z-d43b70/replica_v2b
D:/Anaconda/envs/codegen-marker/python.exe -c "from http.server import SimpleHTTPRequestHandler,ThreadingHTTPServer; import functools; ThreadingHTTPServer(('127.0.0.1',8099), functools.partial(SimpleHTTPRequestHandler,directory='.')).serve_forever()"
```

浏览器打开 `http://127.0.0.1:8099/index.html`（报告页入口），再进 `states/s_003/index.html`（Tags 面板状态）验收：应能看全量 tag、能上下滚动、右上角有「× 关闭」、点击能回 s_002。

---

## 6. 复刻相关概念速览（避免下家重新摸索）

- `replica_models.py`：`ReplicaFlow`/`ReplicaState`/`ReplicaDocument`/`InteractionRegion`（含新增 `full_html`）/`ActionTarget`。
- `replica_annotation_panel.py`：GUI 右侧「复刻标注面板」（stop 录制后可视，可改 locator）。本任务与它无关，勿误伤。
- `build_replica.py`：`build_replica(flow, source_root, output_root)` 主入口；`_render_document` 生成单个 state 页面；`_state_root` 决定 URL（entry→`index.html`，其它→`states/{id}/index.html`）；`RUNTIME` 是 `replica_runtime.js` 内容。
- `capture_v2/` = 用新代码（含通用 Meta 面板捕获）重新 capture 的成果，其中 **s_003 已捕获完整 `#tagsBox`（full_html 4549 字符，含全部 tag）**。这是「数据里已有全量 meta」的关键证据——之前旧 manifest 没有。
- 背景：旧 `replica_v2/` 是用旧 capture + 旧（白底）build 生成的；`replica_v2b/` 应作为新验证产物，避免覆盖旧产物混淆。

---

## 7. 已知约束/坑

- 不要用裸 `python`（系统是 3.7，缺 PyQt6/playwright wheel）。一律 `D:/Anaconda/envs/codegen-marker/python.exe`。
- 截图存 `.jpeg`（本机对 `.png` 结尾会自动加密，Read 读不了；但 `capture_v2/snapshots/**/*.png` 是录制中间产物，别动）。
- 复刻是**纯静态**，可断网打开；画布动态帧等能力 `unsupported`（自然 partial，非 bug）。
- 不要在后台 shell 里跑 `serve_replica.py`（`input()` 在无 stdin 时立即退出）；用 §5 的 http.server 方式。
- 项目 `CLAUDE.md` 有标记/Marker 体系、管道等约定，如需要可参考；本任务范围只涉及复刻渲染与捕获的 Meta 面板部分。

---

## 8. 交接后建议的第一动作

1. 用多模态 **`Read` 看 s_003 相关 jpeg 截图**确认画面与关闭按钮位置。
2. 按 §4.1 更新 build_replica 测试（改断言新行为），然后运行：
   ```powershell
   D:/Anaconda/envs/codegen-marker/python.exe -m unittest test.test_build_replica test.test_capture_snapshot -v
   ```
3. 按 §4.2 给 RUNTIME 加 `data-replica-back` 处理。
4. §5 重建 `replica_v2b` 并在浏览器验收（看全量 Meta、滚动、关闭回 s_002）。
5. §4.5 提交。
