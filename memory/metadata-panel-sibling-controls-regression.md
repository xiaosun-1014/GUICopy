---
name: metadata-panel-sibling-controls-regression
description: 多序列收尾时发现并修复的 metadata 面板兄弟控件离线回放回归（5c2e2d4 + b637886 叠加）
metadata:
  type: project
---

## 背景

2026-08-15 多序列扩展收尾时，首次在真实浏览器跑 `test/test_replica_e2e.py`，发现旧测试 `test_popup_frame_sequence_transition_replays_offline` 失败（此测试自 7c92171 存在，因沙箱跑不了 Playwright 从未验证过）。

## 根因（两处叠加回归）

1. **5c2e2d4 "fix: handle metadata panels in replicas"**：`capture_marker_panel_region()` 命中面板后只保留面板 root（members=[]），把面板外的兄弟交互控件（WL/WW 输入、confirm 按钮、canvas）从 metadata region 剔除。离线回放 sequence「series → metadata → fill → confirm」在 fill 后停在原 state，但 confirm 已不在该 state → 点击超时。
2. **b637886（closure 修复本轮自己引入）**：为修 `test_non_action_overlay_child_cannot_block_action_target`，给无 `data-replica-action/input/series-key/role` 的 overlay 成员统一加 `pointer-events:none`。修复 1 后 confirm 虽渲染出来，但无 action 属性仍点不透（`<main> intercepts pointer events`）。

## 修复（commit 9198883，3 文件）

- `capture_snapshot.capture_marker_panel_region()`：收集 scope 内面板外的可见兄弟控件为 metadata region members，用 outerHTML 包含关系排除面板 root 内部元素（防重复）。
- `build_replica._render_document()`：metadata region 的 members 不再整体 `continue`，改为逐 member 跳过「已被面板 root 逐字包含」的重复项，其余（面板外兄弟控件）正常渲染为 overlay。
- `RUNTIME` CSS：`pointer-events:none` 仅作用于装饰性无 action overlay；`button/input/select/textarea/canvas/a/[data-testid]` 保持 `pointer-events:auto`。

## 关键教训（How to apply）

- **环境可跑浏览器就一定要跑**：closure review 曾把「沙箱跑不了浏览器」当环境限制搁置，导致这个历史回归一直没被验证。本会话实测 chromium 可启动（`PLAYWRIGHT OK → LAUNCH OK → SMOKE PASS`），立刻暴露并修复。
- **两处独立改动可能叠加成一个回归**：5c2e2d4（旧）与 b637886（新）单独看各自合理，叠加导致无法回放。定位靠 worktree 检出历史提交做二分。
- 修复策略：面板完整 outerHTML 渲染保留（产品特性），只补齐面板外兄弟控件；不破坏 `test_metadata_members_are_not_rendered_twice` 的不重复语义。
- 回归验证：新增 `test_metadata_panel_keeps_side_controls_reachable_and_panel_once`；全套 134 浏览器 tests GREEN。

相关：[[sdd-closeout-experience]]、[[codex-windows-sandbox-1385]]
