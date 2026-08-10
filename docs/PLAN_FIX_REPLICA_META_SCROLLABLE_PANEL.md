# 【执行计划】复刻站「全量 Meta 面板 + 关闭返回」修复

> **用途**：可直接执行的分步计划。交接给下一位（多模态）模型或按序人工执行。
> **项目根**：`D:/00-Project/04-codegencopy`（Playwright Codegen 复刻工具）
> **解释器**：一律 `D:/Anaconda/envs/codegen-marker/python.exe`
> **GUI 测试前缀**：`$env:QT_QPA_PLATFORM='offscreen'`
> **当前 git**：`main`，已有 `11e47b6`(orchestrator 修复) + `9bbb2c3`(旧白底面板方案)；本计划是在 `9bbb2c3` 基础上的**增量修复**。

---

## 0. 目标（最终验收标准）

复刻站里打开「Tags / Meta 信息面板」的状态页（`states/s_003/index.html`）：
- ① 能看到**完整 DICOM 元数据**（Patient/Study/Series/Instance/Image/Equipment/UIDS 全分组，约 40+ 行）。
- ② 内容超出可视区时**可上下滚动**查看全部。
- ③ 页面右上角有 **「× 关闭」**按钮，点击**回到 s_002**（Tags 打开前的报告画面），不遮住报告、不影响其它状态页。

---

## 1. 背景事实（已确认，勿重查）

1. Meta/Tags 面板是**全屏浮层**（capture rect = 0,0,1696,880）。
2. 状态图是**线性单链**，**没有**「s_003 → s_002」现成跳转：
   ```
   s_000 →s_001 →s_002(点Tags按钮 a_002_001) →s_003(打开Tags)
   →s_004(空链接 a_002_002) →s_005(窗宽窗位) →…→s_011
   ```
3. 录制里**没有**「关闭 Tags」动作，需手动补一条返回导航。
4. **数据已就绪**：`capture_v2`（新代码 capture）里 **s_003 已捕获完整 `#tagsBox`**（`full_html` 4549 字符，含全部 tag）。这是关键前置证据。
5. 之前失败根因：旧 `9bbb2c3` 方案在 s_003 渲染了「白底全覆盖 replica-panel」盖住整页截图 → 全屏数据、无 x、无法回退。
6. 当前模型的非多模态问题：不能看图。**下家（多模态）可用 `Read` 读 `*.jpeg` 看画面**。

---

## 2. 已完成的代码改动（工作区，未提交）

| 文件 | 改动 | 说明 |
|---|---|---|
| `replica_models.py` | `InteractionRegion` 加 `full_html: str\|None=None` | 完成 |
| `capture_snapshot.py` | 新增 `capture_marker_panel_region()`；扩展 Meta 候选；metadata 走通用面板捕获 | 完成 |
| `build_replica.py` | ① 移除白底 coverage panel 渲染；② `_render_document` 加 `back_target`（右上角 `× 关闭`，`data-replica-back`）；③ 主循环为非入口 state 算 `back_abs` 并传参 | 完成（**尚未提交**）|
| `test/test_capture_snapshot.py` | 新增 `test_metadata_panel_captures_full_container_from_candidates` | 完成 |
| `test/test_build_replica.py` | 新增 `test_metadata_region_renders_scrollable_panel_with_full_html` | **需重写**（断言白底 panel，已删）|

---

# —— 执行步骤 ——

## 步骤 1：重写 build_replica 渲染测试（断言新行为）

**文件**：`test/test_build_replica.py`

**现状**：`test_metadata_region_renders_scrollable_panel_with_full_html` 断言 `data-replica-panel`/`overflow-y:auto`（白底 panel 已删 → 必然失败）。

**做法**：把该测试改为断言新行为——构造一个**非入口状态**（带 `back_abs` 前置状态），断言其页面：
- 包含 `<button data-replica-back=...>× 关闭</button>`（含 `data-replica-back` 与文字「关闭」）
- **不再**包含 `data-replica-panel`
- 背景 `replica-bg` 仍存在

> 关键点：`back_abs` 由主循环按「前一 ordinal state 的入口页绝对路径」计算。测试里要构造**两个 state**（s_000 入口 + s_001 非入口），且 s_001 的 `back_abs` 应解析到 s_000 页面 URL（`index.html`）。可参考现有 `test_same_document_id_uses_state_specific_screenshot_assets` 如何构造多 state flow。

**自检**：
```powershell
cd D:/00-Project/04-codegencopy
D:/Anaconda/envs/codegen-marker/python.exe -m unittest test.test_build_replica.CaptureSnapshotTests 2>nul
D:/Anaconda/envs/codegen-marker/python.exe -m unittest test.test_build_replica -v
```

**验收**：`test.test_build_replica` 全绿；新测试确认 s_001 页面带 `data-replica-back` 且无 `data-replica-panel`。

---

## 步骤 2：给 `replica_runtime.js` 加「× 关闭」跳转处理

**文件**：`build_replica.py` 顶部的 `RUNTIME` 常量

**现状**：RUNTIME 的 click 监听只处理 `[data-replica-action]`，不处理 `[data-replica-back]`。

**做法**：在 `document.addEventListener('click', ...)` 处理器里，`const element = event.target.closest('[data-replica-action]')` **之前/之后**加分支：
```javascript
const backEl = event.target.closest('[data-replica-back]');
if (backEl) {
  event.preventDefault();
  const target = backEl.getAttribute('data-replica-back');
  if (target) window.top.location.assign(target);
  return;
}
```
注意：`data-replica-back` 的值是 build 时 `_relative_url(destination, back_abs)` 算好的相对路径（如 `../../../index.html` 或 `../index.html`），直接 `location.assign` 即可。

**自检**：`D:/Anaconda/envs/codegen-marker/python.exe -c "import ast; ast.parse(open('build_replica.py',encoding='utf-8').read()); print('ok')"`（语法）。

> 提示：RUNTIME 在 `build_replica.py` 中生成 `replica_runtime.js`；改完需 (§5) 重建才能作用到产物。

---

## 步骤 3：核对 s_003 截图画面（多模态优势，可选但推荐）

- 用 `Read` 打开 s_003 状态页引用的背景截图：
  - 构建后：`out/ftimage/runs/20260807T012243Z-d43b70/replica_v2/assets/by-hash/*.jpeg`
  - 或原始：`.../capture_v2/snapshots/a_002_001/after/assets/d_p_000_root.jpeg`
- 确认：它是「Tags 浮层 + 报告」完整画面；能看清 40+ 行 tag；`× 关闭`（面板右上角 close）在画面中的位置。
- 若截图就是完整体，则「看全量 + 滚动」靠截图背景即达成，无需再叠加 DOM。

**决策**：只有当截图确实看不清 / 想增强时，才考虑「保留截图 + 半透明可滚动浮层」。**默认不加**（避免再次遮挡整页）。

---

## 步骤 4：重建复刻站并验收

**第一条命令（重建到新目录，不覆盖旧产物）**：
```powershell
cd D:/00-Project/04-codegencopy
$env:PYTHONIOENCODING='utf-8'
D:/Anaconda/envs/codegen-marker/python.exe -c @"
import sys; sys.path.insert(0,'.')
from batch_capture_replicate import build_from_manifest
from pathlib import Path
RUN = Path('out/ftimage/runs/20260807T012243Z-d43b70')
out = RUN/'replica_v2b'
build_from_manifest(RUN/'capture_v2'/'manifest.json', RUN/'capture_v2', out, source_path=RUN/'source'/'processed_script_ftimage.py')
print('built ->', out)
"@
```

**第二条命令（启动静态服务，持久后台）**：
```powershell
cd out/ftimage/runs/20260807T012243Z-d43b70/replica_v2b
D:/Anaconda/envs/codegen-marker/python.exe -c "from http.server import SimpleHTTPRequestHandler,ThreadingHTTPServer; import functools; ThreadingHTTPServer(('127.0.0.1',8099), functools.partial(SimpleHTTPRequestHandler,directory='.')).serve_forever()"
```
（用 run_in_background / 独立终端跑；`serve_replica.py` 因 `input()` 在无 stdin 时会退出，不要用它。）

**验收清单**（浏览器 `http://127.0.0.1:8099/index.html`）：
- [ ] 入口 `index.html` 正常（报告图）。
- [ ] 进入 `states/s_003/index.html`：能看到完整 Tags/meta 内容（全量分组）。
- [ ] 内容超高时可滚动（`scrollHeight > clientHeight`）。
- [ ] 右上角有「× 关闭」按钮；点击后回到 `states/s_002/index.html`（或 `index.html`，视 back_abs 解析）。
- [ ] s_002 页面正常显示报告，**没有**白底面板盖住。

> 若 s_003 背景截图与预期不符（非 Tags 浮层画面），用多模态特性读图定位：看 `_state_root`/`asset_paths` 是否正确关联 s_003 的 `capture_v2` 截图（参考现有测试 `test_same_document_id_uses_state_specific_screenshot_assets`）。

---

## 步骤 5：提交（分逻辑，精确文件）

**第 1 个提交 —— 功能（未提交的增量）**：
```powershell
git add -- replica_models.py capture_snapshot.py build_replica.py test/test_capture_snapshot.py test/test_build_replica.py
git commit -m "feat: show full scrollable meta on Tags panel with a close-back button in replica"
```
（若你没把 orchestrator 修复提交过：它已在 `11e47b6`，勿重复。）

**注意**：
- 不要 `git add .`；精确文件。
- **不要动/不要提交** `docs/superpowers/specs/2026-08-06-replica-locator-annotation-panel-design.md`（用户手改稿）。
- 若步骤 2/3 改出其它遗留修复，可并入此提交或单列「fix:」提交。

---

## 6. 万一出问题的排查点

| 现象 | 排查 |
|---|---|
| s_003 还是全屏数据/白底 | 确认 build 用的是新 `build_replica`（无白底 panel）。重跑 §4 重建；检查产物 html 无 `data-replica-panel` |
| 关闭按钮不跳转 | RUNTIME 是否加了 `data-replica-back` 分支（§2）；`data-replica-back` 路径是否相对 destination 正确 |
| 关闭跳错页 | `back_abs`=前一 ordinal state 入口页；核对 ordinal 排序与 `_state_root` |
| 背景图不对 | 读图对照 s_003 截图；检查 `asset_paths` 关联（多 state 截图会 by-hash 区分）|
| 滚动不生效 | 确认截图本身高>可视（`scrollHeight>clientHeight`）；静态站滚动依赖内容高度，必要时给 body/replica 容器加 `overflow-y:auto` |
| capture_v2 未满 12 组 | 若 re-capture，用新代码 capture（含通用 Meta 面板捕获），勿回旧 capture |

---

## 7. 概念速览（下家速读）

- `build_replica.py::build_replica(flow, source_root, output_root)` 主入口。
- `_render_document(...)` 生成单 state 页；`_state_root` 定 URL（entry→`index.html`，其它→`states/{id}/index.html`）。
- `RUNTIME` = `replica_runtime.js` 内容。
- `capture_v2/` = 新代码 capture，s_003 含完整 `#tagsBox` full_html。
- Meta 信息工具 marker → `capture_marker_interaction_region` → metadata 分支 → `capture_marker_panel_region`（候选定位面板根 + 抓完整 outerHTML）。
- 本任务与 `replica_annotation_panel.py`（GUI 标注面板）无关，勿误伤。
