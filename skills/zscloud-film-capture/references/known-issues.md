# 已知问题与对策

抓取 zscloud 影像过程中遇到的常见坑,以及对应的解决方案。

## 0. 共享链接直接 goto 后看不到 viewer(报告页拦截)

### 现象

```python
page.goto("https://zscloud.zs-hospital.sh.cn/film/#/shared?code=xxx")
iframe = page.locator("iframe").first
iframe.wait_for(state="attached", timeout=30000)  # 超时
```

### 原因

服务器会 **302 重定向**到 `https://zscloud.zs-hospital.sh.cn/film/web/#/thirdParty/share/sharedStudy`,
这是一个**报告概览页**(显示病人信息、检查所见、检查提示等),**不包含 viewer**。
viewer 必须由用户点「查看影像」链接才会出现,且通常**新开一个 tab**,
URL 变为 `/film/web/#/web2d?...&type=sharedStudy`。

### 解决

**两段式流程**:

```python
# 1. 等报告页 + 点「查看影像」
for _ in range(45):
    if "sharedStudy" in page.url and "查看影像" in page.content():
        break
    time.sleep(1)

# 2. 用 ctx.expect_page() 监听新 tab
with ctx.expect_page(timeout=15000) as info:
    page.get_by_text("查看影像").first.click()
viewer_page = info.value

# 3. 后续操作都在 viewer_page 上
viewer_page.locator("iframe").first.wait_for(...)
```

### 兜底:同 tab 跳转

老版本链接可能同 tab 跳转。如果 `ctx.expect_page()` 15s 超时,
重新读 `page.url`,等变成 `web2d` 即可,然后所有操作仍用 `page`(此时已变成 viewer)。

### 诊断

- 报告页正常:`/film/web/#/thirdParty/share/sharedStudy`,title `智元数影-数字影像`,有「查看影像」按钮
- viewer tab 正常:`/film/web/#/web2d?...`,title `智元数影-数字影像`,有 iframe

如果卡在报告页 → 检查「查看影像」按钮的 `cursor: pointer` 状态,
可能元素被遮挡或不可点击(这时要 dump DOM 排查)。

---

## 1. PNG 在 Windows 本机被自动加密

### 现象

脚本生成的 `.png` 文件在用 Read / Pillow / 任何二进制读取工具打开时报:
```
unrecognized bytes
```
或文件头被改写成非标准 PNG signature。

### 原因

**Windows 企业版**(本机 Windows 10 Enterprise LTSC 2021)的 EFS / 文件系统层会对 `.png` 文件自动加密或 metadata 改写。
这是 OS 级行为,与 Python 无关。

### 解决

**全部产物用 `.jpeg` 扩展名**。`canvas.toDataURL('image/jpeg', 0.92)` 即可。

```javascript
// ✅ 正确
canvas.toDataURL('image/jpeg', 0.92);

// ❌ 错误(会被加密)
canvas.toDataURL('image/png');
```

JPEG quality 0.92 肉眼无损,文件大小约 100KB/帧(同帧 PNG ~ 800KB)。

### 检测

```bash
file frame_000.png     # 如果输出 "data" 而非 "PNG image",已被加密
```

---

## 2. Mixed Content 阻塞:HTTPS iframe 不能 fetch HTTP localhost

### 现象

在 iframe 里执行:
```javascript
fetch('http://127.0.0.1:9876/save', {method: 'POST', body: ...})
```
报:
```
Mixed Content: The page at 'https://...' was loaded over HTTPS,
but requested an insecure resource 'http://127.0.0.1:9876/...'.
This request has been blocked.
```

### 原因

zscloud 用 HTTPS 加载,内部 iframe 继承父页的安全策略。HTTPS 页面不能 fetch HTTP 资源,
即使目标在本机 localhost。

### 解决

**不 fetch 到 localhost。** 直接让 iframe 把 base64 返回给 Python:

```python
# 在 iframe 内
b64 = frame.evaluate("() => canvas.toDataURL('image/jpeg', 0.92)")
# 返回 base64 字符串到 Python
# Python 端落盘
import base64
Path("frame.jpeg").write_bytes(base64.b64decode(b64.split(",", 1)[1]))
```

这样 iframe→Python 走的是 Playwright 的协议通道(websocket),不走 HTTP,不受 mixed content 限制。

### 备选(本 skill 不推荐)

如果一定要走 HTTP server,**必须给 server 上 HTTPS** 或在父页加 CSP 例外。
但本场景下 base64 回传更简单稳定。

---

## 3. 懒加载:每帧需等待 ~2.8s

### 现象

逐帧翻页时,如果立即截图,canvas 是**空白的**(width=0, height=0)。

### 原因

DICOM 序列非当前帧的图像是按需加载的。`pageTurnToCurrFileIndex('manual')` 触发后,viewer:
1. 检查目标帧是否在缓存
2. 不在则发起 HTTP 请求取 DICOM 文件
3. 解码 → 上传 GPU → 渲染到 canvas

整个过程在本机网络下经验值 **2.8 秒**。

### 解决

固定等待 2.8 秒:

```python
viewport.pageTurnToCurrFileIndex('manual')
time.sleep(2.8)  # 懒加载 + 解码 + 渲染
b64 = frame.evaluate("() => canvas.toDataURL(...)")
```

### 进阶:自适应等待

监听 canvas 的 `width/height` 变化:

```python
for _ in range(30):  # 最长等 3s
    cw = frame.evaluate("() => document.getElementById('0_0').width")
    if cw > 0:
        break
    time.sleep(0.1)
```

但**网络延迟**可能让 canvas 短暂返回非零后又被清空。固定 2.8s 更稳定。

---

## 4. `setCurrFileIndex` 只改状态,不触发渲染

### 现象

```javascript
viewport.setCurrFileIndex(5);
console.log(viewport.currFileIndex);  // 输出 5 ✓
// 但 canvas 还是帧 4 的内容 ✗
```

### 原因

`setCurrFileIndex` 是纯 setter,只更新内部状态,不触发渲染管线。
渲染由 `pageTurnToCurrFileIndex` 触发。

### 解决

**必须同时调用两者**:

```javascript
viewport.setCurrFileIndex(idx);
viewport.pageTurnToCurrFileIndex('manual');  // ← 不可少
```

### 诊断

如果所有帧截图文件大小完全相同(MD5 一致),几乎肯定是这个原因。

---

## 5. canvas `preserveDrawingBuffer: false` 抓图问题

### 现象

某些 WebGL canvas 配置下,`canvas.toDataURL()` 返回空白帧或长度不变。

### 原因

WebGL 默认 `preserveDrawingBuffer: false`,渲染后立即清空 buffer。
`toDataURL` 调到的可能是已清空的 buffer。

### 解决

用 **2D scratch canvas 中转**:

```javascript
const scratch = document.createElement('canvas');
scratch.width = source.width;
scratch.height = source.height;
const ctx = scratch.getContext('2d');
ctx.drawImage(source, 0, 0);  // 从 WebGL canvas 拷到 2D canvas
return scratch.toDataURL('image/jpeg', 0.92);
```

但 zscloud viewer 用的是 2D canvas,这个问题不常见。
**只有在 WebGL viewer 出现空白帧时才需要这个 workaround**(如 cornerstone3D 配 WebGL renderer)。

---

## 6. iframe `content_frame` 拿不到

### 现象

```python
frame = page.locator("iframe").content_frame
# frame 为 None
```

### 原因

- iframe 还没加载完
- iframe 是 sandbox 跨域
- iframe 选择器选错了(顶层有多个 iframe)

### 解决

```python
# 等 iframe attached
page.locator("iframe").first.wait_for(state="attached", timeout=30000)

# 兜底:遍历所有 frame 找含 canvas 的
for f in page.frames:
    if f == page.main_frame:
        continue
    try:
        if f.locator("canvas").count() > 0:
            frame = f
            break
    except Exception:
        continue
```

---

## 7. `mainview` undefined

### 现象

```javascript
window.mainview.getViewports()  // TypeError: Cannot read property of undefined
```

### 原因

viewer JS 还没加载完,或加载失败了(网络问题、URL 过期等)。

### 解决

**轮询等待** mainview 就绪:

```python
for i in range(60):  # 最多 60 秒
    ok = frame.evaluate("""
        () => typeof window.mainview !== 'undefined'
              && window.mainview
              && window.mainview.getViewports().length > 0
    """)
    if ok:
        break
    time.sleep(1)
else:
    die("mainview 60s 内未就绪")
```

如果 60 秒还没好,大概率是:
- URL `code` 已过期 → 重新生成共享链接
- viewer 内部错误 → 看 console 日志
- 网络问题 → 检查是否能访问 viewer

---

## 8. 协议项找不到

### 现象

双击协议步骤失败,所有候选选择器都返回 null。

### 原因

- 协议面板在折叠状态
- 协议名不是 "5*5" 而是 "Axial 5mm" 这种长文本
- 协议列表需要先点 "加载" 按钮

### 解决

1. 先 dump DOM 看真实结构
2. 用模糊正则匹配(如 `/\d+\s*[*xX×]\s*\d+/`)
3. 找展开按钮先展开

```python
html = frame.evaluate("() => document.body.innerHTML")
Path("debug_dom.html").write_text(html)
```

---

## 9. WW/WL 输入框设值后视图不变

### 现象

输入框 value 改了(浏览器 DevTools 能看到),但 canvas 渲染的窗宽窗位没变。

### 原因

- 没触发 `change` 事件(viewer 监听 change 不是 input)
- 没按 Enter(viewer 监听 keydown Enter)
- 输入框是受控组件,直接改 value 会被 React/Vue 同步回去

### 解决

```javascript
el.focus();
el.value = String(val);
el.dispatchEvent(new Event('input', {bubbles: true}));
el.dispatchEvent(new Event('change', {bubbles: true}));  // ← 必须
el.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true}));  // ← 必须
```

### 兜底

直接调 viewer 内部 API:

```javascript
const v = window.mainview.getViewports()[0];
// 试不同版本的内部方法
try { v.setWWWL?.(ww, wl); } catch {}
try { v.imageManager.setWindow?.(ww, wl); } catch {}
```

---

## 10. 5×5 切换到 1×1 时主画布 id 不固定

### 现象

切到 1×1 后 `document.getElementById('0_0')` 找不到。

### 原因

- 切布局瞬间,DOM 重建中,canvas 还没挂上
- viewer 用的是非标准 id 命名

### 解决

切完布局后**等 1.5 秒再操作**:

```python
frame.evaluate(CHANGE_LAYOUT_JS, layout)
time.sleep(1.5)  # 等 DOM 重建
```

如果 1.5 秒后还是找不到,用面积排序兜底:

```javascript
const cs = document.querySelectorAll('canvas');
let best = cs[0], bestArea = 0;
for (const c of cs) {
    const a = c.width * c.height;
    if (a > bestArea) { bestArea = a; best = c; }
}
// best 一定是主画布
```

---

## 11. 文件名大小写冲突

### 现象

Windows 下 `frame_000.jpeg` 和 `Frame_000.JPEG` 被认为是同一文件(不区分大小写),
Linux/Mac 下是两个文件。脚本在不同 OS 行为不一致。

### 解决

**统一小写 `.jpeg`**。命名格式 `frame_{idx:03d}.jpeg`,全部小写。

---

## 12. 大量截图内存累积

### 现象

抓几百帧后,base64 字符串在 Python 端累积,内存占用上升。

### 原因

每帧 b64 字符串在 Playwright 内部缓存 + Python 端缓存,没及时释放。

### 解决

**逐帧落盘,不缓存**:

```python
for i in range(total):
    b64 = frame.evaluate(CAPTURE_CANVAS_JS)
    if not b64:
        continue
    # 立即写盘 + 释放
    raw = base64.b64decode(b64.split(",", 1)[1])
    Path(f"frame_{i:03d}.jpeg").write_bytes(raw)
    del b64, raw  # 显式释放
```

每帧 ~100KB,68 帧序列总占用约 7MB,可控。
