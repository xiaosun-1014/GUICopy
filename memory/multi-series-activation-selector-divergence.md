---
name: multi-series-activation-selector-divergence
description: review 发现：序列激活路径(_locate_series_row)未接配置 item_selector，FTImage 批量激活全失败；发现/激活选择器必须同源
metadata:
  type: project
---

## 背景

2026-08-16 三路子代理 review commit `2e9620f`（FTImage/zscloud 真实站多序列适配）时发现
**Critical C1**：**发现**路径（`discover_series_candidates`）已接 `viewers.yaml` 的
`item_selector`（FTImage = `a:has(span.total)`），但**激活**路径 `_locate_series_row`
仍用硬编码默认选择器 `option, [data-series], [role='option'], .series-item, li`——
对 `a` 行一个都不匹配 → `capture_one_series` 抛 `HubUnrecoverableError` → 每个分支
标记 failed → 离线无激活（「发现成功、激活全失败」的空架子）。zscloud 因
`li.ui-draggable` 恰好被默认 `li` 兜住才能跑。

修复（`6d56397`）：`_locate_series_row` / `_reparse_target_row` 加 `item_selector`
参数，4 处调用点（主定位 / retry / `_wait_for_series_ready` 轮询 / `_restore_hub_state`）
透传 `self._series_cfg.get("item_selector")`；补 ft 结构激活回归锁测试。

## 为什么测试全绿却漏掉它

- contract test 内置 fixture 的序列行带 `data-series` 属性，**恰好被默认选择器命中**
  → 主链路上 item_selector 是否透传根本测不到（viewers.yaml 配置注入无端到端覆盖）。
- `test_replica_regions` 只测了「发现路径」的配置参数化 + 默认命中 0 的负断言，
  没测「激活路径」能否用配置定位。

## How to apply（关键教训）

- 同一 viewer 的「发现」与「激活/重定位」选择器必须**同源读取**；改动发现算法或
  viewer 配置时，grep `_SERIES_ITEM_SELECTOR` 与 `_locate_series_row(` 的全部调用点，
  逐一点确认已透传配置（这是最容易断链的粘合处）。
- fixture 若包含默认选择器能命中的属性（`data-series` / `li` / `role=option`），会
  **在主路径无意绕过配置注入断链**——新 fixture 应与真实 viewer 结构一致到「默认
  选择器命中 0」，并同时覆盖发现 + 激活两条路径。
- 相关：[[multi-series-subprocess-mainpath-series-region-bug]]（同类「测试没走主路径
  就发现不了 bug」的教训）
