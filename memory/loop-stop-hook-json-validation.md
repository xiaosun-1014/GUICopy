---
name: loop-stop-hook-json-validation
title: /loop Stop hook 报 "JSON validation failed" 的根因与规避
description: /loop Stop hook 要求裸 JSON 判定；proxy 模型输出围栏/散文导致解析失败；非阻塞根因与规避
metadata:
  type: project
---

## 现象

用 `/loop 用子代理把所有计划完成…` 跑长任务时，每轮结束时终端出现：

```
● Ran 1 stop hook
  ⎿  Stop hook error: JSON validation failed
```

## 根因

`/loop <条件>` 会在运行时（内存中，非持久化 settings）注册一个 **Stop 钩子**。每次 assistant 停止，harness 拿循环条件问 LLM「条件是否已满足」，并**要求模型整个 stdout 恰好是 一个裸 JSON 判定**：`{"ok": true}` 或 `{"ok": false, "impossible": bool, "reason": "..."}`。

本机 proxy 模型（DeepSeek-V4-Flash，见 `~/.claude/settings.json` 的 `ANTHROPIC_BASE_URL: http://127.0.0.1:15721`）不输出裸 JSON，而是输出一整段散文，再把 `{"ok": …}` 包进 markdown 反引号/```json 围栏。harness 对整段 stdout 执行 `JSON.parse()`，开头是散文不是 `{` → **JSON validation failed**，exitCode 1。

关键：`stop_hook_summary` 的 `preventedContinuation: false` —— 失败**非阻塞**，循环不会崩，只是无法干净判定，于是原地反复自检反复报错。

## 为什么现有配置查不到

已核实：`~/.claude/settings.json`、`~/.claude.json`（含 projects 段）、项目 `.claude/`、superpowers 插件 hooks.json（6.1.1/6.2.0，只有 SessionStart）**都没有 Stop hook**。`/loop` 的 Stop 钩子是 built-in `/loop` 命令在运行时自建的内存钩子，没有可编辑的持久化文件。

## 如何规避（How to apply）

- 判定逻辑往往本来就对（它返回 `ok:false` 因为任务确实没做完），错只在格式。先看 stdout 内容确认判定意图，别被报错误导。
- `/loop` 条件措辞强制「只输出一个裸 JSON 对象 `{"ok":true 或 false}`，不要任何散文、不要代码围栏」——对 DeepSeek-V4-Flash 只是尽力而为，该模型对这种约束也不稳定。
- **最稳妥：`/loop` 用于「自动判定是否继续」对当前 proxy 模型太脆弱，长任务收尾直接手动在当前会话推进（派发 review→关闭 task→final review→push），让条件真正满足**，再依赖 `/loop` 判定（此时正确输出 `{"ok": true}`）。

相关：[[three-stage-workflow]]（项目主工作流）
