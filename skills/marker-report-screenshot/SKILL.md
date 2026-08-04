---
name: marker-report-screenshot
description: 处理 Playwright 录制脚本中的「报告截图」marker：检查作用域、插入页面稳定等待、生成正确的 screenshot 调用。当需要补全或修复含 # [MARKER: 报告截图 @ ...] 的 Python 脚本时使用。
triggers:
  - 报告截图
---

# 报告截图 Marker 处理

当在 Playwright 录制脚本中遇到 `# [MARKER: 报告截图 @ {ts}]` 标记时，
将其替换为「等待页面稳定 → 截图」的完整逻辑。

## 处理规则

### 1. 定位 marker

搜索脚本中的行：
```
# [MARKER: 报告截图 @ YYYYMMDD_HHMMSS]
```

时间戳 `{ts}` 只用于标识 marker，**不得用于截图文件名**。报告产物固定为
脚本同级目录下的 `report.jpeg`。

### 2. 检查作用域

**如果 marker 在 `def run()` 外部（模块级）：**
将整个 marker 块移动到 `def run()` 内部、紧跟在产生报告页面的动作之后。
模块级没有 `page`/`page1` 变量，无法截图。

**如果 marker 在 `def run()` 内部：**
直接替换 marker 块即可。

### 3. 判断截图对象

查看 marker 前面最近的操作，确定应该对哪个 page 截图：
- 如果前面是 `page.xxx()` → 截图对象为 `page`
- 如果前面是 `page1.xxx()` 且在 `with page.expect_popup()...` 块之后 → 截图对象为 `page1`
- 如果前面是 `page1.locator(<iframe选择器>).content_frame.xxx()` → 截图对象仍是 `page1`（对主页面截图，iframe 内的操作不改变截图对象）

### 4. 插入等待逻辑

在 `page.screenshot()` 前加入页面稳定等待。根据 DICOM viewer 特点：

**DICOM viewer 陷阱**：DICOM web viewer 通常有持续的 WebSocket 或轮询连接（图像加载、状态同步），
`networkidle` 可能**永远不触发**。因此必须加 timeout 兜底。

```python
# 优先：等待网络空闲（加 timeout 避免被 DICOM 长连接挂死）
try:
    page.wait_for_load_state("networkidle", timeout=10000)
except Exception:
    # DICOM viewer 有持续网络流量时降级，靠后续 timeout 兜底
    print("[截图] networkidle 超时，降级继续")
# 补充：等待影像画布渲染完成（DICOM viewer 用 canvas 渲染）
page.wait_for_timeout(2000)
```

如果上下文中有特定元素出现表示加载完成（如某个按钮变为可用），应使用：
```python
page.locator("css=选择器").wait_for(state="visible", timeout=10000)
```

### 5. 生成最终代码

将原有 marker 块：
```python
# [MARKER: 报告截图 @ 20260624_111212]
# page.screenshot(path='report.jpeg', full_page=True)
```

替换为：
```python
# [MARKER: 报告截图 @ 20260624_111212]
from pathlib import Path
SCRIPT_DIR = Path(__file__).resolve().parent
try:
    page.wait_for_load_state("networkidle", timeout=10000)
except Exception:
    print("[截图] networkidle 超时，降级继续")
page.wait_for_timeout(2000)
page.screenshot(
    path=str(SCRIPT_DIR / "report.jpeg"),
    type="jpeg",
    quality=95,
    full_page=True,
)
```

### 6. 输出

修改后的完整脚本，保留所有其他 marker 和代码不变。输出必须满足：

- 固定文件名 `report.jpeg`
- 使用 `SCRIPT_DIR / "report.jpeg"`，不可依赖进程工作目录
- `type="jpeg", quality=95`
- 文件名不带 marker 时间戳
