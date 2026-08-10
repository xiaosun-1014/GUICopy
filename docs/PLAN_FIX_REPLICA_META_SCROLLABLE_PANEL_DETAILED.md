# 【执行计划·扩展版】复刻站「全量 Meta 面板 + 关闭返回」修复

> 本文件是 `PLAN_FIX_REPLICA_META_SCROLLABLE_PANEL.md` 的**代码级扩展版**：把步骤 1/2/4/5 展开成可直接照抄的完整代码/命令。背景、目标、验收标准与原版一致。
> 项目根：`D:/00-Project/04-codegencopy`；解释器一律 `D:/Anaconda/envs/codegen-marker/python.exe`
> 当前 git：`main`，已有 `11e47b6` + `9bbb2c3`；本计划是 `9bbb2c3` 之上的增量修复（工作区未提交的 3+2 文件）。

---

## 步骤 1 —— 重写 `test_build_replica.py` 渲染测试（完整代码）

### 文件
`test/test_build_replica.py`

### 动作
把现有 `test_metadata_region_renders_scrollable_panel_with_full_html`（断言 `data-replica-panel`/`overflow-y:auto`，已失效）**整体替换**为下面这个"断言关闭返回按钮 + 无白底面板"的测试。

> 关键：要触发 `_render_document` 的 `back_target` 分支，需要构造**非入口状态**（即 ordinal>0）。而 `back_abs` 由主循环取"前一 ordinal state 的入口页绝对路径"。所以构造两个 state：`s_000`(entry) 和 `s_001`。下面是完整可跑测试。

```python
    def test_non_entry_page_renders_close_back_button_without_covering_panel(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            assets = root / "assets"; assets.mkdir()
            (assets / "main.png").write_bytes(b"png")
            # 两个 state：entry s_000 + 非入口 s_001。
            # 用不同文档（不同 asset）避免 by-hash 去重覆盖。
            doc0 = ReplicaDocument(
                "d_main", "p_main", "page", "main", None, None, None, None,
                {"width": 800, "height": 600}, 1, "css", 0, 0, "assets/main.png", "main", 3,
            )
            doc1 = ReplicaDocument(
                "d_meta", "p_main", "page", "main", None, None, None, None,
                {"width": 800, "height": 600}, 1, "css", 0, 0, "assets/main.png", "meta", 3,
            )
            flow = ReplicaFlow(
                1, "meta", "recorded.py", "hash", "now",
                {"width": 800, "height": 600},
                BootstrapPlan(1, 1, True, {}), [],
                CaptureTimingProfile(), "s_000",
                [
                    ReplicaState("s_000", 0, "", "page",
                                 [ReplicaPage("p_main", "page", "main", None, None, "d_main", True, False)],
                                 [doc0], [], StateEvidence(False, False, False, False, 0, 0, 0, 0, "entry")]),
                    ReplicaState("s_001", 1, "", "page", None,
                                 [ReplicaPage("p_main", "page", "main", None, None, "d_meta", True, False)],
                                 [doc1], [], StateEvidence(True, False, False, False, 0, 0, 0, 0, "nav")]),
                ],
                [],
            )
            output = root / "replica"
            build_replica(flow, root, output)

            entry_html = (output / "index.html").read_text(encoding="utf-8")
            # 入口页（s_000）不应有返回按钮
            self.assertNotIn("data-replica-back", entry_html)
            # 非入口页（s_001, states/s_001/index.html）应有返回按钮
            s1 = (output / "states" / "s_001" / "index.html").read_text(encoding="utf-8")
            self.assertIn('data-replica-back=', s1)
            self.assertIn("关闭", s1)
            # 按钮应指向 s_000 入口页（相对）: ../../index.html 或 ./index.html 之一即可
            self.assertRegex(s1, r'data-replica-back="[^"]*index\.html"')
            # 不应再有白底覆盖面板
            self.assertNotIn("data-replica-panel", s1)
            # 背景截图仍在
            self.assertIn("replica-bg", s1)
```

> 注意：`ReplicaState` 第 4 个参数是 `page_var`（前面一个 state 传 `"page"`，s_001 传 `None` 会怎样？若报错改传 `"page"`）。`ReplicaState` 字段顺序以 `replica_models.py` 为准（可见现有测试构造）。**若字段顺序/必填不一致，照抄现有多-state 测试（`test_same_document_id_uses_state_specific_screenshot_assets`）的构造方式调整。**

### 自检（绿色才算过）
```powershell
cd D:/00-Project/04-codegencopy
D:/Anaconda/envs/codegen-marker/python.exe -m unittest test.test_build_replica -v
```
**预期**：该测试通过，且 `test_metadata_region...` 旧方法已被替换（不再存在于文件中，避免重名冲突）。

---

## 步骤 2 —— 给 `RUNTIME` 加「×关闭」跳转（完整代码）

### 文件
`build_replica.py` 顶部 `RUNTIME` 常量。

### 动作
在 `RUNTIME` 的 click 监听器里加 `[data-replica-back]` 分支。把现有：

```javascript
document.addEventListener('click', event => {
  const option = event.target.closest('[role="option"]');
  if (option) { ... }
  const element = event.target.closest('[data-replica-action]');
  if (!element) return;
  const transition = window.__REPLICA_TRANSITIONS__[element.dataset.replicaAction];
  ...
});
```

改为（在 `const element = ...` 之前插入 back 分支）：

```javascript
document.addEventListener('click', event => {
  const backEl = event.target.closest('[data-replica-back]');
  if (backEl) {
    event.preventDefault();
    const target = backEl.getAttribute('data-replica-back');
    if (target) window.top.location.assign(target);
    return;
  }
  const element = event.target.closest('[data-replica-action]');
  if (!element) return;
  ...
});
```

> 说明：`data-replica-back` 的值是 `_render_document` 用 `_relative_url(destination, back_abs)` 算好的相对路径，`location.assign` 直接可用（相对当前页）。

### 自检
```powershell
D:/Anaconda/envs/codegen-marker/python.exe -c "import ast; ast.parse(open('build_replica.py',encoding='utf-8').read()); print('ok')"
```
输出 `ok`。

---

## 步骤 3 —— 核对 s_003 截图（多模态，可选）

用 `Read` 打开：
- 原始：`out/ftimage/runs/20260807T012243Z-d43b70/capture_v2/snapshots/a_002_001/after/assets/d_p_000_root.jpeg`
- 或构建后：`.../replica_v2/assets/by-hash/<hash>.jpeg`（以 `states/s_003/index.html` 的 `.replica-bg src` 为准）

确认画面为「Tags 浮层 + 报告」完整画面、能看清 40+ 行 tag、close 在右上角。满足则**不叠加任何 DOM 浮层**（避免再遮整页）。只在截图不清时考虑「保留截图 + 半透明可滚动浮层」，且需多模态验证不挡字。

---

## 步骤 4 —— 重建 + 启动服务 + 验收（完整命令）

### 4a. 重建到 `replica_v2b`（一行 PowerShell 用 here-string）
```powershell
cd D:/00-Project/04-codegencopy
$env:PYTHONIOENCODING='utf-8'
D:/Anaconda/envs/codegen-marker/python.exe - <<'PY'
import sys; sys.path.insert(0,'.')
from pathlib import Path
from batch_capture_replicate import build_from_manifest
RUN = Path('out/ftimage/runs/20260807T012243Z-d43b70')
out = RUN/'replica_v2b'
build_from_manifest(RUN/'capture_v2'/'manifest.json', RUN/'capture_v2', out, source_path=RUN/'source'/'processed_script_ftimage.py')
print('built ->', out)
PY
```
> PowerShell 的 `- <<'PY'` 语法不可用；正确做法是把这段放到一个 `.py` 临时文件再执行，或直接 `python -c "..."`。标准的**可执行命令（Git Bash / sh）**：
```bash
cd /d/00-Project/04-codegencopy
PYTHONIOENCODING=utf-8 D:/Anaconda/envs/codegen-marker/python.exe - <<'PY'
import sys; sys.path.insert(0,'.')
from pathlib import Path
from batch_capture_replicate import build_from_manifest
RUN = Path('out/ftimage/runs/20260807T012243Z-d43b70')
out = RUN/'replica_v2b'
build_from_manifest(RUN/'capture_v2'/'manifest.json', RUN/'capture_v2', out, source_path=RUN/'source'/'processed_script_ftimage.py')
print('built ->', out)
PY
```
若你的平台没有 bash 工具，就 `python -m pip` 免了——直接把上面 py 内容存成 `out/ftimage/runs/20260807T012243Z-d43b70/_build_v2b.py` 再 `python _build_v2b.py`。

### 4b. 启动静态服务（持久，避 stdin 坑）
```bash
cd /d/00-Project/04-codegencopy/out/ftimage/runs/20260807T012243Z-d43b70/replica_v2b
PYTHONIOENCODING=utf-8 D:/Anaconda/envs/codegen-marker/python.exe -c "from http.server import SimpleHTTPRequestHandler,ThreadingHTTPServer; import functools; ThreadingHTTPServer(('127.0.0.1',8099), functools.partial(SimpleHTTPRequestHandler,directory='.')).serve_forever()"
```
（后台/独立终端运行；若 8099 被占，换 8098 并记住）。

### 4c. 验收清单（浏览器）
- [ ] `http://127.0.0.1:8099/index.html` 报告图正常。
- [ ] `http://127.0.0.1:8099/states/s_003/index.html` 能看到完整 Tags/meta 全量分组。
- [ ] 内容超高可滚动（devtools 看容器 `scrollHeight>clientHeight`）。
- [ ] 右上角「×关闭」，点击回 `states/s_002/index.html`。
- [ ] s_002 正常、**无白底面板**。
- [ ] 其它状态页（s_000、s_005）不受影响。

### 4d. 命令行快速校验（无浏览器也可）
```bash
cd /d/00-Project/04-codegencopy
PYTHONIOENCODING=utf-8 D:/Anaconda/envs/codegen-marker/python.exe - <<'PY'
from pathlib import Path
s1 = Path('out/ftimage/runs/20260807T012243Z-d43b70/replica_v2b/states/s_003/index.html').read_text(encoding='utf-8')
print('has back btn:', 'data-replica-back=' in s1)
print('no covering panel:', 'data-replica-panel' not in s1)
PY
```
预期两行 `True` / `True`。

---

## 步骤 5 —— 提交（分逻辑、精确文件）

```bash
gcd /d/00-Project/04-codegencopy
git add -- replica_models.py capture_snapshot.py build_replica.py test/test_capture_snapshot.py test/test_build_replica.py
git commit -m "feat: show full scrollable meta on Tags panel with a close-back button in replica"
```
（若期间改出独立 bug 修复，另起 `fix:` 提交。）

**铁律**：
- 不用 `git add .`；精确文件。
- **不要动/不要提交** `docs/superpowers/specs/2026-08-06-replica-locator-annotation-panel-design.md`。
- 别因路径在 Windows（`cd D:\...`）与 bash（`cd /d/...`）混用报错——用你当前 shell 的正确写法。

---

## 排查表（对应的"出问题怎么办"）

| 现象 | 解决 |
|---|---|
| `test_metadata_region...` 重名冲突 | 确保已整体替换旧方法（文件里不再有旧方法名） |
| 测试构造 ReplicaState 报字段错 | 照抄 `test_same_document_id_uses_state_specific_screenshot_assets` 的构造 |
| s_003 仍白底/全屏数据 | 确认 build 用新代码；产物无 `data-replica-panel`（用 §4d 校验）|
| 关闭按钮不跳转 | 确认 RUNTIME 加了 back 分支 + 重建过（§2/§4a）|
| 关闭跳错页 | `back_abs` 逻辑 = 前一 ordinal state 入口页；核对 ordinal |
| 静态站滚动不生效 | 截图高>可视即可；必要时 body/`.replica` 加 `overflow-y:auto` |
| 端口被占 | 换 8098 等，并更新验收 URL |
