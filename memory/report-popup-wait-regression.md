---
name: report-popup-wait-regression
description: 报告截图 popup 等待语义被收紧为「必须有 popup」导致无 popup 场景回归（9e045c3 修复），教训：审阅看似合理的等待收紧要对照既有测试断言并跑浏览器套件
metadata:
  type: feedback
---

2026-08-19 审阅并提交 `_wait_for_report_popup_state` 重写（大提交 79ab3b1 内）时，把「无 popup → 立即成功」改成了「无 popup → 持续等待直到超时」，本意是修复中山 popup 延迟出现的场景。**破坏了「报告截图可在当前页、不必开 popup」的既有契约**。

**Why**：完整 `test_batch_capture_replicate` 整组跑出 2 个稳定 ERROR（`test_live_capture_session_writes_before_and_after_marked_action_snapshots` / `test_region_dom_change_creates_state_for_non_always_after_marker`），都是「简单页面 + 报告截图 marker + 无 popup」的真实浏览器用例。审阅 diff 时我只看到「无 popup 改为等待」这行改动本身合理，**没意识到它覆盖了旧契约**——推理时「中山 popup 必须等」压过了「报告截图未必开 popup」。

**How to apply**（[[three-stage-workflow]] 同款教训）：
- 等待/收紧语义的改动，**diff 审阅必须对照既有测试断言**，不能只看新代码合不合理。看被改函数的所有调用方 + 相关既有测试的预期路径（尤其「无 X 场景」）。
- **环境能跑浏览器就必须跑浏览器套件**（[[metadata-panel-sibling-controls-regression]] 已记过同款）。本次是 `test_batch_capture_replicate` 整组（72 用例 663s）才暴露。
- 修法（9e045c3）：无 popup → 立即成功（报告在当前页），有 popup 但未渲染 → 继续等待。同时把本次改动新增的过度收紧测试 `test_report_popup_wait_does_not_succeed_without_a_popup` 改为对齐正确契约（无 popup 快速成功 / 有 popup 未渲染不成功 —— 两个断言）。
- 何时用「有 popup 才等待」是安全的：after-hook 已被延迟到 popup trigger 的 `popup_info.value` 赋值之后，此时 popup 若真该出现必已存在；不存在即说明报告在当前页。
- 判断既有失败归属：`git worktree add <dir> <改动前commit>` 跑同一用例对照（注意 commit 要选「真正改动前」如 f80ae2e，而非 HEAD^ = 大提交自身）。