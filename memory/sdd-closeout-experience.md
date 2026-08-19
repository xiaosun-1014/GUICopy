---
name: sdd-closeout-experience
title: SDD 计划收尾执行经验（手动推进到 ready_to_merge + push）
description: SDD 计划收尾：scoped re-review 验证 fix → 关 task → final whole-branch review triage → push，及可复用落点
metadata:
  type: project
---

## 适用范围

用 superpowers SDD（subagent-driven-development）跑多任务实现计划时，把所有 task 收尾到 `origin/main` 的标准推进路径与关键落点。

## 推进路径（How to apply）

1. **逐 task 收尾**：scoped review 若发现 Critical/Important，implementer 出 fix round → 提交 → **必须派发 scoped re-review** 独立验证 fix 真关闭了 finding（只审 fix 改动，不重审整 task）。
2. **关 task**：re-review approved 后，在 `.superpowers/sdd/{plan-date}-{plan}/progress.md`（ledger）记录「fix round N/N + re-review ✅ + complete」。
3. **final whole-branch review**：所有 task 关完才做，聚焦**跨 task 集成一致性 + plan 全局约束合规**，不逐行重审已批准改动；对 ledger 里 deferred minor / parked 项做 **triage 定案**（修复 / 规格对齐 / minor 不阻断 / 留待后续，逐一给 reason）。
4. **push**：只 push `origin/main..HEAD` 的未推送提交（`git log --oneline origin/main..HEAD` 看范围）。

## 本分支沉淀的可复用落点（2026-08-05 event-protocol）

- 审查分支主题、提交、ledger 记录全在 `.superpowers/sdd/` 工作区，`review-{a}..{b}.diff` 即 review package。
- 仓库解释器硬约束：测试/子进程一律 `D:/Anaconda/envs/codegen-marker/python.exe`，**禁止静默回退 `sys.executable`**（系统 Python 3.7 缺 PyQt6/playwright wheel）。
- **测试环境澄清（2026-08-18 修正）**：`test_batch_capture_replicate` **不是 import 挂起**——实测 import 0.49s，`chromium.launch()` 逐用例正常。它整组慢是因为 70 个用例里多个真实 Playwright 浏览器用例（每个 15-40s），整组 10+ 分钟。跑它给足 timeout（>`600s`）或按子集；不要被「headless import 挂起」的过时记忆误导。快速单测组仍用：
  `PYTHONIOENCODING=utf-8 D:/Anaconda/envs/codegen-marker/python.exe -m unittest test.test_orchestrator_events test.test_agent_marker_boundaries test.test_replica_gui -v`

## 一次具体 triage 案例

agent_failed 事件字段：实现侧全部用 `status`（4 值稳定枚举），spec §4.7 文本误写 `reason`。triage 定为 **规格对齐代码**（改 spec 文本），因为同事件族 marker_finished/agent_finished 都用 `status`，`reason` 是 spec 内部孤立偶发；且 orchestrator 未构建、无消费方，改文本零风险。教训：**实现跨事件一致、spec 单点不一致时，倾向改 spec 而非改稳定代码**。

相关：[[three-stage-workflow]]、[[loop-stop-hook-json-validation]]
