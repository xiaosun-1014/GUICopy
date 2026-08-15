---
name: multi-series-subprocess-mainpath-series-region-bug
description: 子进程整链测试暴露：主路径「序列选择」快照被 strict-mode evaluate 吞掉导致 entry 无 series region；修复取 `.first`
metadata:
  type: project
---

## 背景

2026-08-15 为封住 codex final review 的 P0#1 测试缺口，新增整链 contract test
`test/test_capture_and_build_series_contract.py`，走**子进程 production 管线**
（`capture_and_build` + `replica_annotations.json` + `REPLICA_EXPANSION_CONFIG`
→ 插桩脚本 → `capture_hook_expand_series` → 真实 `series_branches` → v2 manifest
→ `build_replica` → 离线浏览器点击）。首个版本在「离线 entry 没有可点击的序列成员」停下，
顺藤摸出一个从未被测试覆盖的生产 bug。

## 根因

`LiveCaptureSession.before("a_000_001", …, '序列选择')` → `_capture()` 第 663 行（修复后行号；修复前 574–578）：

```python
frame_owner = target_locator.evaluate(...)   # 多元素 locator → strict mode violation
```

- 录制脚本里 `page.locator("#series .item").first.click()` 的 `.first` 在插桩 recipe 里被丢弃
  （instrumented 变成 `lambda: page.locator('#series .item')`）。
- 序列列表天然命中多个 item，`target_locator.evaluate` 触发 Playwright strict mode violation，
  异常被 `capture_hook_failed` 吞掉 → **`snapshots/a_000_001/before|after` 整个没落盘**
  → `build_flow_from_snapshots` 跳过该动作 → entry 状态（`s_000`）只有 metadata region、
  没有 series region → 离线入口页序列成员没有 `data-replica-series-key`，不可点击。
- 为什么以前没发现：in-process 测试（`test_multi_series_capture`）只直接调
  `finalize_series_branches()`，**从不走 `session.before` 的主路径 hook**；唯一的
  `run_live_capture` 测试用的是唯一 locator（`#go`），单元素不触发。

## 修复（.py 非测试改动，唯此一处）

`batch_capture_replicate.py` `_capture()` 的 frame-owner 探测改 `target_locator.first.evaluate(...)`
（仅作单元素解析；单元素时 `.first` 是 no-op）。

## 关键教训（How to apply）

- **in-process 调用 vs 子进程插桩是两条不同的覆盖通道**：`LiveCaptureSession` 类内
  直接调用跳过 `capture_hook_*` 的注入和 recipe 归一化，`session.before/after` 主路径
  只有 `capture_and_build`/`run_live_capture` 子进程才能测到。整链测试必须走子进程。
- **多元素 locator 的 recipe 会在插桩时丢 `.first`**：对序列这类天然多匹配的目标，
  主快照层面对 target 的任何单元素操作都要 `.first`，否则 strict-mode 一票否决整个快照。
- 一个「离线 entry 可点击」的断言（entry 有 `[data-replica-series-key]` 且有路由）就能守住
  这类回归；配合字段细节：`status.json` 只有 `series_key_sha256`，原始 key 在
  `descriptor.json`；`viewer_state_id` 本身已带 `bviewer_` 前缀；离线页渲染的是
  「背景截图 + series 选项 overlay + meta-open 按钮」，不是全套 DOM（没有 `#current-series`）。

相关：[[metadata-panel-sibling-controls-regression]]、[[sdd-closeout-experience]]
