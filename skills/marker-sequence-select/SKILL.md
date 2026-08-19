---
name: marker-sequence-select
description: 从 DICOM viewer 序列列表中自动选择最优序列。三层策略：A) JS遍历全量DOM + 多模式帧数提取 + 坐标点击；B) 文本块解析 + get_by_text 点击；C) VL截图坐标回退。借鉴 guiagent 生产级方案。
triggers:
  - 序列选择
---

# 序列选择 Marker 处理

处理 `# [MARKER: 序列选择]` 标记，从 DICOM viewer 的序列列表中自动选择诊断价值最高的序列并点击。

**设计来源**：本 skill 借鉴了生产级项目 `guiagent` 中 `UicloudViewerAdapter` 的方案
（`_dom_series_candidates` + `_select_largest_series`），并针对 Playwright 录制脚本场景做了适配简化。

## 核心思想

1. **全量 DOM 遍历**：用 JS 遍历 viewer 所在文档（主文档或 iframe）内**所有可见元素**，提取文本 + 位置 + 尺寸
2. **多模式帧数识别**：`_parse_slice_count` 覆盖各种帧数表达格式，含关键字兜底
3. **位置去重 + 尺寸过滤**：同一系列被父子元素多次匹配时只计一次，过滤超大元素
4. **帧数优先评分**：`(slice_count, keyword_pref, -y_pos)`，帧数越多序列越优
5. **统一坐标点击**：全部策略都用 `page.mouse.dblclick(x, y)`，避免 `get_by_text` 文本匹配的不确定性。注意嵌套 iframe 场景下需做坐标偏移补偿（见下文）

## 三层策略（逐级降级）

```
A: JS DOM全量遍历  ──成功──→ 评分 → 坐标点击
   │失败（跨域/JS禁⽤）
   ▼
B: 文本块解析       ──成功──→ 评分 → get_by_text 点击
   │失败
   ▼
C: VL 截图回退      ────────→ 截图 → VL返回坐标 → 坐标点击
```

## 数据结构

```python
@dataclass
class SeriesCandidate:
    name: str               # 元素的完整文本
    slice_count: int        # 提取到的帧数
    x: float                # 元素左上⻆ x
    y: float                # 元素左上⻆ y
    width: float
    height: float
    source: str = "dom"     # "dom" | "block" | "vlm"

    @property
    def center(self) -> tuple[float, float]:
        return self.x + self.width / 2, self.y + self.height / 2

    @property
    def score(self) -> tuple[int, int, int]:
        """评分：(帧数, 关键字偏好, -y坐标)。帧数优先。
        
        关键字偏好值参考 priority_keywords.md，保持一致的评分体系。
        """
        name_lower = self.name.lower()
        # 跳过项检查（返回最低分，_select_best_series 会过滤）
        for skip_kw in ("scout", "localizer", "surview", "topogram", "dose", "radiation"):
            if skip_kw in name_lower:
                return -1, 0, 0
        # 关键字→偏好分（与 priority_keywords.md 保持一致）
        pref = 0
        if "aiir_lung" in name_lower or "aiir lung" in name_lower:
            pref = max(pref, 100)   # AIIR_Lung = 薄层+MPR+Lung，诊断价值最高
        if "thin" in name_lower or "hrct" in name_lower or "0.625" in name_lower:
            pref = max(pref, 100)   # 薄层原始数据
        if "mpr" in name_lower or "coronal" in name_lower or "sagittal" in name_lower:
            pref = max(pref, 100)   # 多平面重建
        if "axial" in name_lower:
            pref = max(pref, 90)
        if "vr" in name_lower or "volume" in name_lower or "3d" in name_lower:
            pref = max(pref, 80)    # 容积重建
        if "mip" in name_lower or "maxip" in name_lower:
            pref = max(pref, 80)    # 最大密度投影
        if "soft" in name_lower:
            pref = max(pref, 60)    # 软组织窗
        if "lung" in name_lower or "肺" in name_lower:
            pref = max(pref, 60)    # 肺窗
        if "bone" in name_lower:
            pref = max(pref, 60)    # 骨窗
        if "brain" in name_lower:
            pref = max(pref, 60)    # 脑窗
        if "mediastinum" in name_lower or "med" in name_lower:
            pref = max(pref, 55)    # 纵隔窗
        if "minip" in name_lower:
            pref = max(pref, 50)    # 最小密度投影
        return self.slice_count, pref, -int(self.y)
```

## 策略 A：JS DOM 全量遍历 ⭐（主策略）

**来源**：`guiagent/uicloud.py` → `_dom_series_candidates` + `_select_largest_series`

### A1. 获取 iframe 的 Frame 对象

需要 **Frame** 而非 FrameLocator（FrameLocator 不支持 `evaluate()`）。

> **⚠️ 必须用 `frame.evaluate()`，不要用 `page.evaluate` + `contentDocument`**
>
> 错误的做法：
> ```python
> # ❌ contentDocument 在 Playwright 中经常返回 null
> page.evaluate("document.querySelector('#iframe').contentDocument...")
> ```
> 正确的做法（两种方式均可）：
>
> **方式一**：从 `page.frames` 中找到 Frame 对象
> ```python
> def _find_viewer_frame(page1) -> Frame | None:
>     """在 page1 的所有 iframe 中找到含 canvas 的那个。"""
>     # ...（见下方完整代码）
> ```
>
> **方式二**：如果已知 iframe 的 name，直接引用
> ```python
> for f in page1.frames:
>     if f.name == "imageFrame":
>         return f
> ```
>
> 然后用 `frame.evaluate()` 访问 DOM：
> ```python
> nodes = frame.evaluate("""() => {
>     return Array.from(document.querySelectorAll('body *'))...
> }""")
> ```

```python
def _find_viewer_frame(page1) -> Frame | None:
    """找到 viewer 所在 Frame；无 iframe 的 viewer 返回主文档 Frame。"""
    for f in page1.frames:
        if f == page1.main_frame:
            continue
        try:
            if f.locator("canvas").count() > 0:
                return f
        except Exception:
            continue
    # 飞图等 viewer 直接渲染在主文档，不能因没有子 iframe 而跳过策略 A。
    try:
        if page1.main_frame.locator("canvas").count() > 0:
            return page1.main_frame
    except Exception:
        pass
    # 最后兜底：返回第一个非主 frame
    for f in page1.frames:
        if f != page1.main_frame:
            return f
    return None
```

### A2. JS 遍历全部 DOM 元素

```python
def _dom_series_candidates(frame) -> list[SeriesCandidate]:
    """JS 遍历 frame 内全部可见元素，返回候选序列列表。"""
    nodes = frame.evaluate("""() => {
        const norm = (value) => (value || '').replace(/\\s+/g, ' ').trim();
        const vp = {width: window.innerWidth, height: window.innerHeight};
        return Array.from(document.querySelectorAll('body *')).map((el) => {
            const rect = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            return {
                text: norm(el.innerText || el.textContent || ''),
                x: rect.x, y: rect.y,
                width: rect.width, height: rect.height,
                viewport: vp,
                visible: style.visibility !== 'hidden' &&
                    style.display !== 'none' &&
                    rect.width > 20 && rect.height > 12 &&
                    rect.bottom > 0 && rect.right > 0 &&
                    rect.x < vp.width && rect.y < vp.height,
            };
        }).filter((item) => item.visible && item.text);
    }""")
    return nodes
```

### A2.1 重试循环（必须！）

**场景**：页面加载后，序列列表 DOM 可能还没渲染完成。一次扫描返回空是常见现象。

**要求**：必须加 15 秒超时循环，反复扫描直到找到候选。

```python
def _collect_sequences_with_retry(frame, timeout=15.0) -> list[dict]:
    """带重试的 DOM 扫描：最长等 timeout 秒，不断重试直到找到候选。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        nodes = _dom_series_candidates(frame)
        if nodes:
            # 检查是否有帧数候选
            candidates = [n for n in nodes if _parse_slice_count(n.get("text",""))[0] is not None]
            if candidates:
                return nodes
        time.sleep(0.8)
    return nodes  # 超时也返回最后一次结果（可能为空）
```

> ⚠️ 没有重试循环的后果：序列列表比 JS 遍历晚加载 → 返回空列表 → 策略 A 跳过 → 降级到 B/C。
> cxhospital 实测约需 1-3 次重试（0.8-2.4s）DOM 才加载完整。

### A3. 帧数提取（`_parse_slice_count`）

每个元素独立判断，覆盖多种帧数表达格式。三层逐级降级：

**模式 1（显式标记）**：`"362幅"` `"205 images"` `"Images: 362"`
**模式 2（关键字兜底）**：文本含 DICOM 关键字时提取 ≥50 的数字（避免误抓序列名中的小数字）
**模式 3（厚度推断）**：`"Body 1.0 CE"` → 1.0mm → 推断约 400 帧

```python
# 厚度 → 估算帧数映射
_THICKNESS_TO_FRAMES = [(0.6, 500), (1.0, 400), (1.25, 320),
                        (2.0, 200), (3.0, 130), (5.0, 80), (10.0, 30)]

def _infer_frames_from_thickness(text: str) -> int | None:
    """从层厚数字推断帧数。如 'Body 1.0 CE' → 1.0mm → 400帧。"""
    if len(text) < 8:
        return None
    if not re.search(r"(Body|Chest|Abdomen|Head|Lung|CT|MR|MPR|Thin|CE)", text, re.I):
        return None
    # 模式 A：带 "mm" 后缀的厚度，如 "1.0 mm"
    m = re.search(r"(\d+(?:\.\d+)?)\s*mm", text, re.I)
    if m:
        thickness = float(m.group(1))
        for limit, frames in _THICKNESS_TO_FRAMES:
            if thickness <= limit:
                return frames
        return None
    # 模式 B：裸数字厚度（如 cxhospital 的 "Body 1.0 CE" 无 mm 后缀）
    # 只匹配带小数点的浮点数，避免误抓 "(1帧)" 中的整数
    m = re.search(r"(?:\b|(?<=\D))(\d+\.\d+)(?:\b|(?=\D))", text)
    if m:
        thickness = float(m.group(1))
        if thickness <= 10.0:
            for limit, frames in _THICKNESS_TO_FRAMES:
                if thickness <= limit:
                    return frames
    return None

def _parse_slice_count(text: str) -> tuple[int | None, str] | tuple[None, None]:
    """从文本中提取帧/切片数。返回 (count, source) 或 (None, None)。

    source: "explicit" | "keyword" | "thickness"
    """
    # 模式 1：显式标记（最高优先级）
    for pat in (
        r"(?:切片数|图像数|张数|层数|帧数|Images?|Slices?|Frames?)\s*[:：]?\s*(\d{1,4})",
        r"(\d{1,4})\s*(?:张|幅|层|帧|images?|imgs?|slices?|frames?)",
    ):
        matches = re.findall(pat, text, re.I)
        if matches:
            candidates = [int(m) for m in matches if 1 <= int(m) <= 2000]
            if candidates:
                return max(candidates), "explicit"

    # 模式 2（关键字兜底）：含 DICOM 关键字，取 ≥50 的数字
    if re.search(r"(AIIR|Lung|CT|薄层|肺|序列|Series|MPR|VR|MIP|Body)", text, re.I):
        nums = [int(m) for m in re.findall(r"\b(\d{2,4})\b", text) if 1 <= int(m) <= 2000]
        nums = [v for v in nums if v >= 50]  # ≥50 避免误抓序列名内含的小数字
        if nums:
            return max(nums), "keyword"

    # 模式 3（厚度推断）："Body 1.0 CE" → 400 帧
    inferred = _infer_frames_from_thickness(text)
    if inferred is not None:
        return inferred, "thickness"

    return None, None
```

> **为什么不加排除规则？** 如果病人信息（如 "RM5413344 CT Hu Ping (1幅)"）被误选为序列，
> 不要加 `_is_excluded_text` 排除函数。这是帧数解析不足的副作用——
> 真实序列 "Body 1.0 CE" 通过厚度推断得到 ~400 帧后评分远高于病人信息（1 帧），自然胜出。
> 排除规则会误杀真实序列，且需为每个 viewer 单独维护。

### A4. 去重 + 评分 + 坐标点击

```python
def _select_best_series(nodes: list[dict], page1,
                       iframe_outer_selectors: list[str] | None = None,
                       iframe_inner_selector: str | None = None) -> SeriesCandidate | None:
    """从 JS 返回的节点列表中过滤、去重、评分、点击。
    
    Args:
        nodes: _dom_series_candidates 返回的节点列表
        page1: viewer 页面
        iframe_outer_selectors: 可选，外层 iframe 选择器列表，用于坐标偏移补偿
        iframe_inner_selector: 可选，内层 iframe 选择器（嵌套 iframe 场景）
    """
    candidates: list[SeriesCandidate] = []
    seen: set[tuple[int, int, int]] = set()

    for node in nodes:
        text = node["text"].strip()
        if not text or len(text) > 260:
            continue

        w, h = float(node["width"]), float(node["height"])
        vp = node.get("viewport", {})
        vp_area = float(vp.get("width", 1)) * float(vp.get("height", 1))
        # 过滤：超大元素（>视口35%）
        if w * h > vp_area * 0.35 or w > 760 or h > 360:
            continue

        slices = _parse_slice_count(text)
        if not slices:
            continue

        # 位置去重
        key = (round(float(node["x"]) / 10), round(float(node["y"]) / 10), slices)
        if key in seen:
            continue
        seen.add(key)

        candidates.append(SeriesCandidate(
            name=text,
            slice_count=slices,
            x=float(node["x"]),
            y=float(node["y"]),
            width=w, height=h,
        ))

    if not candidates:
        return None

    # 过滤跳过项（score.first == -1）
    candidates = [c for c in candidates if c.score[0] >= 0]
    if not candidates:
        return None

    target = sorted(candidates, key=lambda x: x.score, reverse=True)[0]
    # 嵌套 iframe 场景需坐标偏移补偿，参见「坐标系陷阱」章节
    try:
        ox, oy = _get_iframe_offset(page1, outer_selectors=iframe_outer_selectors or [],
                                     inner_selector=iframe_inner_selector)
    except Exception:
        ox, oy = 0.0, 0.0
    page1.mouse.dblclick(target.center[0] + ox, target.center[1] + oy)
    page1.wait_for_timeout(1500)
    return target
```

### 完整调用

**跨 marker 输出契约（必须）**：序列选择代码块结束前必须定义
`seq_name` 和 `seq_frames`。后续「影像画布交互」直接读取 `seq_frames`，
不得只保留 `best`、`selected_series` 等局部变量。

```python
frame = _find_viewer_frame(page1)
best = None
if frame:
    nodes = _dom_series_candidates(frame)
    # 选择器由调用者根据 viewer 类型决定（参考 viewers.yaml 的已知 viewer 选择器，或从录制脚本反推）
    # uicloud 单层: iframe_outer_selectors=['[id="2d-iframe"]']
    # cxhospital 嵌套: iframe_outer_selectors=['#iframe'], iframe_inner_selector='iframe[name="imageFrame"]'
    best = _select_best_series(nodes, page1,
                               iframe_outer_selectors=['[id="2d-iframe"]'],
                               iframe_inner_selector=None)
    if best:
        print(f"[序列选择] A策略命中: {best.name} ({best.slice_count}幅)")

seq_name = best.name if best else None
seq_frames = best.slice_count if best else None
```

## 策略 B：文本块解析（回退）

用于 `evaluate()` 不可用时（FrameLocator 模式、跨域 iframe 等）。

保持原方案：`body.inner_text()` → 空行分块 → 块首行=系列名 → 匹配帧数行。

但点击方式从 `get_by_text` 改为先 `get_by_text` 取元素再取 bounding box → 坐标点击：

```python
def _click_by_text(fl, name: str) -> bool:
    """先用 get_by_text 找元素，再取 bounding box 做坐标点击。"""
    try:
        loc = fl.get_by_text(name).first
        box = loc.bounding_box(timeout=5000)
        if box:
            x = box["x"] + box["width"] / 2
            y = box["y"] + box["height"] / 2
            page1.mouse.dblclick(x, y)
            return True
    except Exception:
        pass
    return False
```

## 策略 C：VL 截图回退

截图 → VL 模型返回坐标 → `page1.mouse.dblclick(x, y)`。

**必须执行点击**：VL 返回坐标后必须调用 `page.mouse.dblclick`，不能只返回名字。

**必须满足的落盘/调用约束**：
- 回退截图固定为 `SCRIPT_DIR / "series_select_fallback.jpeg"`，JPEG quality=95；禁止 cwd 相对路径。
- `vl_script` 必须从项目内 `skills/vl-config/scripts/call_vl.py` 确定性解析并检查存在；禁止依赖未定义的 global。
- 必须实际执行 `subprocess.run(... --task series_extract --image ...)`；不能只截图或 print 后返回。

```python
def _vl_fallback(page1, fl):
    # 所有产物必须相对 completed 脚本自身目录，不能依赖进程 cwd。
    from pathlib import Path
    SCRIPT_DIR = globals().get("SCRIPT_DIR", Path(__file__).resolve().parent)
    path = SCRIPT_DIR / "series_select_fallback.jpeg"
    try:
        fl.locator("body").screenshot(
            path=str(path), type="jpeg", quality=95,
        )
    except Exception:
        page1.screenshot(
            path=str(path), type="jpeg", quality=95, full_page=True,
        )
    print(f"[序列选择] 截图已保存: {path}")

    # 调用 VL 获取坐标
    import subprocess, json
    project_root = SCRIPT_DIR.parent.parent
    vl_script = project_root / "skills" / "vl-config" / "scripts" / "call_vl.py"
    if not vl_script.exists():
        return None
    result = subprocess.run(
        [sys.executable, str(vl_script), "--task", "series_extract",
         "--image", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=30,
    )
    if result.returncode == 0:
        vl_data = json.loads(result.stdout)
        seqs = vl_data.get("sequences", [])
        if seqs:
            best = max(seqs, key=lambda s: s.get("frames", 1))
            vl_x, vl_y = best.get("x"), best.get("y")
            if vl_x is not None and vl_y is not None:
                # 用 VL 坐标点击
                ox, oy = _get_iframe_offset(page1, outer_selectors=["#iframe"])
                page1.mouse.dblclick(vl_x + ox, vl_y + oy)
                page1.wait_for_timeout(1500)
                print(f"[序列选择] ✓ C策略点击: {best.get('name')}")
                return
    # 兜底：截图已保存供人工检查
    print(f"[序列选择] C策略: 截图已保存至 {path}, 请手动检查")
```

> **常见错误**：策略 C 只 `return` 了名字和帧数但没有点击。这导致 `select_series` 返回了非空值，
> 调用方以为序列已选中，跳过原始录制回退，但实际没做任何点击操作。
> 下游的窗宽窗位、画布交互都会因序列未选中而失败。

## 坐标系陷阱：嵌套 iframe 坐标偏移

### 问题

`frame.evaluate()` 中 `getBoundingClientRect()` 返回的是 **iframe 视口坐标**。
但 `page.mouse.dblclick(x, y)` 需要的是**主页面坐标**。
嵌套 iframe 场景下两者相差各层 iframe 的偏移量，直接使用会导致点击偏移。

```
主页面 (0,0)
  └─ iframe#1 (ox1, oy1)          ← bounding_box()['x'], ['y']
       └─ iframe#2 (ox2, oy2)      ← 同上
            └─ 目标元素 (x, y)      ← frame.evaluate 返回相对该 iframe 的坐标
```

### 修复

在点击前计算 iframe 偏移并加到坐标上：

```python
def _get_iframe_offset(page, outer_selectors: list[str], inner_selector: str | None = None) -> tuple[float, float]:
    """累加每层 iframe 的 bounding_box 偏移。
    
    Args:
        page: Playwright page 对象
        outer_selectors: 外层 iframe 的 Playwright locator 选择器列表
                          （如 ['#iframe', 'iframe[name="imageFrame"]']）
        inner_selector: 可选，内层 iframe 选择器（嵌套 iframe 场景）
    
    说明：选择器由调用者根据 viewer 类型传入——
          - uicloud（单层 iframe）: outer_selectors=['[id="2d-iframe"]']
          - cxhospital（嵌套 iframe）: outer_selectors=['#iframe'], inner_selector='iframe[name="imageFrame"]'
          - 未知 viewer: 从录制脚本已有 locator() 调用反推
    """
    ox, oy = 0.0, 0.0

    # 累加外层 iframe 偏移
    for selector in outer_selectors:
        try:
            box = page.locator(selector).bounding_box()
            if box:
                ox += box["x"]; oy += box["y"]
        except Exception:
            pass

    # 处理内层 iframe（嵌套场景）
    if inner_selector and outer_selectors:
        try:
            outer_loc = page.locator(outer_selectors[0])
            inner = outer_loc.content_frame.locator(inner_selector).bounding_box()
            if inner:
                ox += inner["x"]; oy += inner["y"]
        except Exception:
            pass

    return ox, oy
```

使用时：

```python
# 选择器由调用者根据 viewer 类型决定（参考 viewers.yaml 的已知 viewer 选择器，或从录制脚本反推）
# uicloud 单层 iframe 场景：
ox, oy = _get_iframe_offset(page, outer_selectors=['[id="2d-iframe"]'])
# cxhospital 嵌套 iframe 场景：
# ox, oy = _get_iframe_offset(page, outer_selectors=['#iframe'], inner_selector='iframe[name="imageFrame"]')
page_coords = (target.center[0] + ox, target.center[1] + oy)
page.mouse.dblclick(*page_coords)
```

### 注意事项

- iframe 选择器由调用者传入，常见 viewer 示例见函数 docstring
  - 可从 `viewers.yaml` 对应 viewer 的 `iframe_selectors` 字段获取
  - 也可从录制脚本中已有的 `locator(...)` 调用反推
- 单层 iframe 传入 `outer_selectors=[...]`，嵌套 iframe 追加 `inner_selector`
- 无 iframe（飞图等主文档 viewer）时，`_find_viewer_frame()` 返回
  `page1.main_frame`，偏移选择器传入空列表，坐标偏移为 `(0, 0)`

## 与 guiagent 原版的差异

| 维度 | guiagent (uicloud.py) | 本 skill 适配版 |
|---|---|---|
| 运行环境 | async Playwright | sync Playwright（录制脚本场景） |
| 帧数提取 | `_parse_slice_count`（同左） | 同上，完整移植 |
| 评分 | `(slice_count, pref, -y)` | 同上 |
| 点击方式 | `page.mouse.dblclick(x, y)` | 同上（统一坐标点击） |
| 回退 | VLM `choose_series` | 文本块解析 → VL 截图 |
| 定位 iframe | 轮询含 canvas 的 frame | 子 frame 优先 + 主文档 viewer + 非主 frame 兜底 |
| viewer 配置依赖 | 无（viewer-agnostic） | 无（viewer-agnostic，选择器由调用者传入） |
