# 真实站点冒烟测试清单（Real-Site Smoke Checklist）

在真实 DICOM Web viewer 上按「生成 Adapter + 离线复刻」一键管道跑全流程的验收清单。
三类 viewer **分开记录**：弹出窗（popup）类、嵌套 iframe 类、FTImage 类。

> **保密约定**：本文件任何一行都**不得**包含病人数据或真实 token。
> 站点标识使用脱敏后的代号（如 `uicloud-demo`、`cxhospital-sandbox`）。
> 测试完成后如含敏感观察，只登记类别与代号，不复制实际影像/姓名/凭据。

## 通用记录列（每行一个 run）

以下 15 列用于逐项记录一次完整冒烟：

| # | 列 | 说明 |
|---|----|------|
| 1 | date | 测试日期（YYYY-MM-DD） |
| 2 | tester | 执行人代号 |
| 3 | sanitized site identifier | 脱敏后的站点代号（不带 token / 病人数据） |
| 4 | auth mode | `scripted` / `interactive` / `storage-state` |
| 5 | run ID | `runs/` 下的运行 ID |
| 6 | adapter generation | ✅ / ⚠️ / ❌（+ error_category） |
| 7 | live capture | 现场采集是否成功 + 帧数 |
| 8 | popup/frame topology | 弹出窗 vs iframe 及其选择器是否为预期拓扑 |
| 9 | replica build | 静态 Replica 是否成功构建 |
| 10 | offline adapter | 离线 Adapter（`completed_*_offline.py`）是否生成并执行 |
| 11 | external request count | 离线重放过程中的外部请求数（应趋于 0） |
| 12 | artifact validation | `report.jpeg` / `dicom_meta.json` / 画布帧等产物校验 |
| 13 | privacy validation | 脱敏 / 无凭据泄漏检查 |
| 14 | final status | `success` / `partial` / `failed` / `cancelled` |
| 15 | blocker category | 若失败：`authentication` / `adapter_generation` / `replica_build` / `privacy_violation` / `other` |

---

## A. Popup viewer（uicloud 类目标）

目标站把 viewer 以 `expect_popup()` 弹出独立窗口。冒烟记录：

| date | tester | site（脱敏） | auth mode | run ID | adapter generation | live capture | popup/frame topology | replica build | offline adapter | external request count | artifact validation | privacy validation | final status | blocker category |
|------|--------|--------------|-----------|--------|--------------------|--------------|----------------------|---------------|-----------------|------------------------|---------------------|--------------------|--------------|-----------------|
|      |        |              |           |        |                    |              |                      |               |                 |                        |                     |                    |              |                 |

## B. Nested iframe viewer（cxhospital 类目标）

目标站把 viewer 嵌在嵌套 `<iframe>` 中，需要 `.content_frame` 链定位。冒烟记录：

| date | tester | site（脱敏） | auth mode | run ID | adapter generation | live capture | popup/frame topology | replica build | offline adapter | external request count | artifact validation | privacy validation | final status | blocker category |
|------|--------|--------------|-----------|--------|--------------------|--------------|----------------------|---------------|-----------------|------------------------|---------------------|--------------------|--------------|-----------------|
|      |        |              |           |        |                    |              |                      |               |                 |                        |                     |                    |              |                 |

## C. FTImage viewer

FTImage 类目标的动态 marker（序列选择 / Meta 信息 / 影像画布）冒烟记录：

| date | tester | site（脱敏） | auth mode | run ID | adapter generation | live capture | popup/frame topology | replica build | offline adapter | external request count | artifact validation | privacy validation | final status | blocker category |
|------|--------|--------------|-----------|--------|--------------------|--------------|----------------------|---------------|-----------------|------------------------|---------------------|--------------------|--------------|-----------------|
|      |        |              |           |        |                    |              |                      |               |                 |                        |                     |                    |              |                 |

---

## 验收判定

- **privacy validation** 必须为 ✅ 才视为可交付：报告事件流脱敏、无真实 token / 病人数据进入本仓库文档。
- **external request count** 在离线 Adapter 重放阶段应稳定为 0（或仅本地 `serve_replica.py` 的本地请求）。
- 任一 run 出现 `privacy_violation` → 立即阻断，先修管道再继续冒烟。
