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

> 这些记忆文件的纯文本内容也被整合在 [CLAUDE.md](../CLAUDE.md) 中供 agent 自动加载。
