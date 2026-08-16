---
name: nonmultimodal-backend-subagent-read-image-api500
description: 本机后端代理模型非多模态（DeepSeek-V4-Flash），子代理一旦 Read 截图/图片就 API 500 中断；看图类验证须改为主会话零读图 DOM 断言
metadata:
  type: project
---

## 背景

2026-08-16 主会话派子代理 C 做 Playwright 离线副本验收（FT 多序列修复，
`docs/FIX_FT_MULTI_SERIES_CLICK_AND_DISCOVERY_2026-08-16.md`）。子代理执行中尝试
读取截图（Read PNG/JPEG）时触发 API 500：

```text
API Error: 500 DeepSeek-V4-Flash is not a multimodal model.
```

按本机网关 `ANTHROPIC_BASE_URL: http://127.0.0.1:15721` 的 DeepSeek-V4-Flash
代理模型，**图片无法进入上下文**。用 SendMessage 恢复一次、明确要求「不要读图」后
仍再次以同一错误中断——子代理在环节里往往会反复尝试读图（截图验证是它的直觉路径）。

主会话随后用**零读图**的 Playwright 脚本直接跑通同一验收：`elementFromPoint` +
`getAttribute` + `wait_for_url` + console 事件全部走文本断言，截图只 `.jpeg` 落盘
不回读。证明该模型完全可以做浏览器验收，只是**不能把图喂给模型**。

## How to apply（关键教训）

- 需要看图/截图的任务，**不要让子代理 Read 任何图片**（PNG/JPEG 都会触发
  `not a multimodal model` 的 API 500，恢复后仍会复发，空烧上下文）。
- 浏览器断言一律用文本/DOM 结果：`page.evaluate` 的 `document.elementFromPoint(x,y)`
  抓元素 + `getAttribute`，`wait_for_url("**...**")` 等；console 用
  `page.on("console")` / `page.on("pageerror")` 收集。这些足够证明「按坐标命中谁、
  跳到哪个状态」。
- 截图仅供用户留档：保存为 `.jpeg`（`%TEMP%` 或 out/），**不要 Read 回来验证**。
- 遇到子代理因读图反复 API 500 时，别第三次恢复它——直接在主会话跑零读图脚本来
  完成该验证。
- 相关：[[metadata-panel-sibling-controls-regression]]（「环境可跑浏览器就一定要跑
  浏览器套件」——但要用零读图方式跑）。
