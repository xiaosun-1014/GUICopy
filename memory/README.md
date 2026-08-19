# 项目记忆文件

此目录存放项目的结构化记忆文件（Markdown 格式），供 agent 读取。
每个文件包含项目的关键约定、工作流、调试原则等持久化知识。

当前记忆：

| 文件 | 内容 |
|------|------|
| [three-stage-workflow.md](three-stage-workflow.md) | 三阶段工作流与调试原则 |
| [loop-stop-hook-json-validation.md](loop-stop-hook-json-validation.md) | /loop Stop hook 报 JSON validation failed 的根因与规避 |
| [sdd-closeout-experience.md](sdd-closeout-experience.md) | SDD 计划收尾执行经验（re-review → 关 task → final review triage → push） |
| [metadata-panel-sibling-controls-regression.md](metadata-panel-sibling-controls-regression.md) | metadata 面板兄弟控件离线回放回归（5c2e2d4 + b637886 叠加）定位与修复 |
| [multi-series-subprocess-mainpath-series-region-bug.md](multi-series-subprocess-mainpath-series-region-bug.md) | 子进程整链测试暴露的主路径序列选择快照 strict-mode bug（`.first` 修复） |
| [multi-series-activation-selector-divergence.md](multi-series-activation-selector-divergence.md) | review 发现：激活路径 `_locate_series_row` 漏接 `item_selector`，FTImage 批量激活全失败；发现/激活选择器必须同源 |
| [zscloud-dapeng-replica-adaptation.md](zscloud-dapeng-replica-adaptation.md) | 中山 zscloud 复刻：Dapeng viewer（无 `#popTagText_*`）/ 分享页 SPA popup 竞态 / build 层 series region 提升（`_promote_series_regions_to_earliest_documents`）/ 分支 series region 挂错 doc（`_reroute_branch_series_regions_to_viewer_documents`）/ annotation 重建 |
| [codex-windows-sandbox-1385.md](codex-windows-sandbox-1385.md) | cc 调 codex 报 `CreateProcessWithLogonW 1385` 根因与修复（`windows.sandbox=elevated` 不可用，改 `unelevated`） |
| [nonmultimodal-backend-subagent-read-image-api500.md](nonmultimodal-backend-subagent-read-image-api500.md) | 本机代理模型非多模态（DeepSeek-V4-Flash），子代理 Read 图片 API 500；看图验证改主会话 DOM 断言，截图 .jpeg 落盘不回读 |
| [report-popup-wait-regression.md](report-popup-wait-regression.md) | 报告截图 popup 等待语义收紧为「必须有 popup」导致无 popup 场景回归（9e045c3 修复）；审阅等待收紧必须对照既有测试断言 + 跑浏览器套件 |

> 这些记忆文件的纯文本内容也被整合在 [CLAUDE.md](../CLAUDE.md) 中供 agent 自动加载。
