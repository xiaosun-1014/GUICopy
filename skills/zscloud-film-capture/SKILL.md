---
name: zscloud-film-capture
description: >
  抓取 zscloud.zs-hospital.sh.cn(复旦中山医院云胶片)共享链接的全量 DICOM 影像。
  完整流程:打开链接(自动 302 到报告页)→ 1080p 视窗 → 等报告页加载 →
  点「查看影像」新开 viewer tab → 切到 viewer tab → 双击选中协议模板(默认 5×5) →
  切换布局到 1×1 → 设置窗宽窗位(WW/WL) → 解析总帧数 →
  逐帧翻页 + canvas toDataURL → JPEG 落盘。
  适用于任意 https://zscloud.zs-hospital.sh.cn/film/#/shared?code=... 链接,
  一次运行即可导出当前协议的全部影像到指定输出目录。
  触发关键词:zscloud 抓图、中山医院云胶片、批量截屏 DICOM、zs-hospital 共享链接、zscloud film capture。
---

# ZS Cloud Film Capture

## 触发关键词

- `zscloud 抓图`
- `中山医院云胶片`
- `批量截屏 DICOM`
- `zs-hospital 共享链接`
- `zscloud film capture`

## 概述

把 `https://zscloud.zs-hospital.sh.cn/film/#/shared?code=<code>` 这类中山医院云胶片共享链接
的全量 DICOM 切片导出为 JPEG。适用于任意 code 参数(其他病人 / 其他检查)。

## 快速使用

```bash
D:/Anaconda/envs/codegen-marker/python.exe \
    skills/zscloud-film-capture/scripts/auto_capture.py \
    "https://zscloud.zs-hospital.sh.cn/film/#/shared?code=<CODE>" \
    --out out/<hospital_name>/canvas_frames
```

可调参数:

| 参数 | 默认 | 说明 |
|---|---|---|
| `--ww` | 2000 | 窗宽 (Window Width) |
| `--wl` | 0 | 窗位 (Window Level) |
| `--protocol` | `5*5` | 要双击选中的协议名(可模糊匹配) |
| `--layout` | `1*1` | 切到的最终布局 |
| `--batch-size` | 5 | 单次 evaluate 内捕获的帧数(2.8s/帧,过大易超时) |
| `--headless` | False | 无头模式(默认有头便于调试) |

跑完后会在 `out/<hospital>/canvas_frames/` 下生成 `frame_000.jpeg ~ frame_{N-1}.jpeg`,
其中 N = 当前协议的总帧数。

## 输入与输出

### 输入

- URL 形如 `https://zscloud.zs-hospital.sh.cn/film/#/shared?code=xxxxx`
- 输出目录(会自动创建 `canvas_frames/` 子目录)
- 可选:WW/WL 值(默认肺窗 2000/-1000;按用户原话也支持 2000/0 的设定)

### 输出

```
<out>/
├── canvas_frames/
│   ├── frame_000.jpeg     ← 第 1 帧
│   ├── frame_001.jpeg
│   ├── ...
│   └── frame_{N-1}.jpeg
└── capture.log            ← 运行日志(诊断信息)
```

JPEG quality 0.92,每帧约 100-140KB,68 帧序列 ≈ 8MB。

## 设计要点

### 1. 共享链接会先重定向到报告页,不是直接进 viewer

```
用户 URL: https://zscloud.zs-hospital.sh.cn/film/#/shared?code=<CODE>
   ↓ 服务端 302 重定向
报告页: https://zscloud.zs-hospital.sh.cn/film/web/#/thirdParty/share/sharedStudy
   ↓ 点「查看影像」(顶部) 或「查看胶片」(底部)
viewer tab(新开): https://zscloud.zs-hospital.sh.cn/film/web/#/web2d?...&type=sharedStudy
   └─ <iframe>             ← 真正的 viewer(UIH / 联影 web2d)
        └─ <canvas id="0_0">  ← 1×1 布局下的主画布
```

⚠ **自动脚本必须捕获新 tab**,因为点「查看影像」是新开 tab 而不是同 tab 跳转。
脚本里用 Playwright 的 `ctx.expect_page(timeout=15s)` 配合 click 来监听。

报告页等待条件:
- URL 含 `sharedStudy`
- 页面有可见的「查看影像」按钮(`cursor: pointer` 或 `A`/`BUTTON` 标签)

所有 viewer 操作(双击协议 / dblclick)必须在 **viewer tab** 上执行,不是报告页。

兜底:如果 15s 内未出现新 tab,会尝试同 tab 跳转并等 URL 变为 `web2d`。

### 2. viewer 是 iframe,不是顶层 DOM

```
顶层 page (zscloud.zs-hospital.sh.cn/film)
   └─ <iframe>             ← 真正的 viewer(UIH / 联影 web2d)
        └─ <canvas id="0_0">  ← 1×1 布局下的主画布
```

所有 viewer 操作必须从 `iframe.content_frame` 入口:
```python
frame = page.locator("iframe").content_frame
frame.evaluate("() => window.mainview.getViewports()[0].pageTurnToCurrFileIndex('manual')")
```

### 3. `setCurrFileIndex` 只改状态,不触发渲染

```javascript
// ❌ 错误:只更新 currFileIndex,canvas 不变
viewport.setCurrFileIndex(i);

// ✅ 正确:必须调 pageTurnToCurrFileIndex 才触发真正的翻页 + 渲染
viewport.setCurrFileIndex(i);
viewport.pageTurnToCurrFileIndex('manual');
```

`pageTurnToCurrFileIndex('manual')` 内部走 `getImageInRealTime` → `displayImage` 管线,
canvas 才绘制新帧。`HangingProtocol` 来源会跳过这一步,但我们手动翻页必须显式调用。

### 4. 懒加载:每帧需等待 ~2.8s

非当前帧的 DICOM 数据是按需加载的(canvas width/height = 0 表示未加载)。
固定等待 2.8s 是经验值,够覆盖本机网速下的懒加载 + 解码 + 渲染。

### 5. 不要 fetch 到 localhost(mixed content)

HTTPS 页面的 iframe 不能 fetch HTTP localhost。
直接用 `frame.evaluate()` 返回 base64,在 Python 端 `base64.b64decode()` 落盘即可,
绕开浏览器→本地服务的网络请求。

### 6. PNG 在本机被自动加密,必须用 JPEG

Windows 环境的特殊问题:`.png` 文件会被自动加密(文件头改写),Read 工具无法读取。
本 skill 全部产物用 `.jpeg`。

## 目录结构

```
zscloud-film-capture/
├── SKILL.md                  ← 本文件(入口 + 使用指南)
├── scripts/
│   └── auto_capture.py       ← 完整自动化脚本(独立运行)
└── references/
    ├── viewer-api.md         ← mainview / viewport / imageManager JS API
    ├── dom-selectors.md      ← 协议/布局/窗宽窗位 DOM 选择器策略
    └── known-issues.md       ← mixed content / 懒加载 / PNG 加密 等已知坑
```

## 文件依赖

- **Python**: `D:/Anaconda/envs/codegen-marker/python.exe` (系统 Python 3.7 缺 PyQt6/playwright wheel)
- **依赖**: `playwright >= 1.40`,已安装 Chromium
- **前置 skill**: 无,这是独立工具
- **后续**: 输出 JPEG 可直接用于 `marker-canvas-capture` / VL 模型 / Meta 提取

## 调试模式

脚本内置 `print` 日志,关键节点会输出:

```
[zscloud] 视窗 1920x1080, headless=False
[zscloud] 导航: https://zscloud.zs-hospital.sh.cn/film/#/shared?code=...
[zscloud] 等待报告页加载...
[zscloud] 点击「查看影像」,等待 viewer 新 tab...
[zscloud]   已点击 '查看影像' (DIV)
[zscloud] 等待 viewer iframe (URL: https://zscloud.zs-hospital.sh.cn/film/web/#/web2d?...)...
[zscloud] viewer 已就绪
[zscloud] viewer URL: https://zscloud.zs-hospital.sh.cn/film/web/#/web2d?...&type=sharedStudy
[zscloud] 双击协议: 5*5
[zscloud] 切换布局: 1*1
[zscloud] 设置 WW=2000, WL=0
[zscloud] 总帧数: 68
[zscloud] 批次 0..4 / 68:捕获帧 0..4
[zscloud] ✓ frame_000.jpeg (109538B)
...
[zscloud] 完成:68/68 帧已保存
```

如果某一步失败(报告页不出现 / 查看影像未点击 / viewer iframe 30s 未挂载 / 协议找不到 / 布局不出现 / WW 输入框未渲染),
脚本会抛清晰错误并保留当前浏览器状态,方便手动排查。

## 常见坑

| 现象 | 原因 | 解决 |
|---|---|---|
| iframe 30s 超时 | 没有点「查看影像」,viewer 未启动 | 见「设计要点 1」,必须先在报告页点跳转 |
| dblclick 在协议上没反应 | 用错了 tab(`page` 是报告页) | 用 `viewer_page.mouse.dblclick(...)`,所有 viewer 操作必须在 viewer tab |
| 全部帧内容相同(同 MD5) | 只调了 `setCurrFileIndex` | 必须再调 `pageTurnToCurrFileIndex('manual')` |
| canvas id 找不到 | 当前布局不是 1×1 | 确认布局已切到 1×1,1×1 时主画布 id 是 `0_0` |
| `mainview` 未定义 | viewer 还没加载完 | 多 wait 几秒,或监听 iframe 的 `load` 事件 |
| 协议双击无反应 | 元素不可见(被遮挡) | 先滚到视口内,再 dispatch dblclick |
| WW/WL 输入框没找到 | 控件是 div + contenteditable 而非 input | 见 `references/dom-selectors.md` 兜底策略 |
| `.png` 文件 Read 报错 | 本机自动加密 | 全部产物用 `.jpeg` |

更多已知问题与对策见 `references/known-issues.md`。

## 与其他 skill 的协作

```
[本 skill] zscloud-film-capture
   │
   ├─→ 输出 canvas_frames/*.jpeg
   │
   └─→ 可送入:
       ├─ marker-canvas-capture   (录制脚本 marker 替换)
       ├─ marker-meta-extract     (DICOM tag 提取)
       └─ vl-config               (VL 模型分析)
```
