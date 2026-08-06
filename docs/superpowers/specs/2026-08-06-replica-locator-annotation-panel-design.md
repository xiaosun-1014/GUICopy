# 复刻 Locator 人工标注面板设计

**日期：** 2026-08-06
**状态：** 待用户审核
**范围：** Playwright Codegen 录制脚本的复刻 locator 检查、编辑、风险反馈与安全回写

## 1. 背景

当前 GUI 支持插入业务 Marker，但细粒度 locator 精修仍只能通过直接修改
`processed_script_*.py` 完成。`parse_action_plan()` 会从脚本中生成
`ActionTarget` 和 `LocatorRecipe`，后续捕获、复刻构建与离线验证都依赖这些
自动解析结果。

现有工作流有三个主要问题：

1. 开发人员需要在完整脚本中人工查找关键动作及 iframe 链。
2. 修改后没有即时 locator 风险反馈，只能等待管道运行结束后查看报告。
3. GUI 没有安全的结构化回写事务，多行 locator、中文 selector 和 Marker 锚点
   都可能因文本级修改产生漂移。

本设计面向熟悉 Playwright 的开发人员。目标是降低定位和精修成本，同时保持
processed 脚本为唯一事实来源。

## 2. 目标与非目标

### 2.1 目标

- 在 GUI 中按 Marker 展示所有已解析的 `ActionTarget`。
- 展示动作类型、源码位置、完整 locator、iframe 链和静态风险档位。
- 允许编辑已有 locator 动作的完整 Playwright receiver 表达式。
- 修改过程中即时预览新 locator 和风险。
- 通过 AST 源码范围执行原子回写，支持单行、多行及中文 selector。
- 回写后重新解析完整脚本，确保动作、Marker 和 iframe 拓扑仍然有效。
- 统一 GUI、构建和验证阶段使用的 locator 风险分类。
- 保持现有保存、导出、捕获和离线复刻管道的行为。

### 2.2 非目标

首版不包含：

- 浏览器内元素拾取和高亮。
- 自动生成候选 locator。
- 将绝对坐标动作自动转换为 locator。
- 对真实页面执行在线 `count()`、`visible()` 或唯一性验证。
- 修改动作类型或动作参数。
- 用 sidecar override 替代 processed 脚本。

坐标动作会显示为只读高风险项。开发人员仍可在左侧代码编辑器中手动将整条
动作改写为 locator 动作。

## 3. 方案选择

### 3.1 采用：源码绑定的 Locator 编辑面板

面板编辑完整 locator receiver，并安全回写 processed 脚本。现有解析、插桩、
捕获、构建和验证流程继续读取同一份脚本。

选择该方案的原因：

- processed 脚本继续作为唯一事实来源。
- 不需要给所有下游阶段增加 override 合并逻辑。
- 与现有 `parse_action_plan()` 和 GUI 代码编辑器直接兼容。
- 对开发人员而言，完整 Playwright 表达式比简化表单更准确、更灵活。

### 3.2 不采用：sidecar locator overrides

将覆盖值存入 `replica_annotations.json` 会引入两套事实来源，并要求解析、
插桩、捕获、构建和验证阶段全部支持覆盖合并。当前按解析顺序生成的
`action_id` 也不适合作为长期稳定的覆盖键。

### 3.3 延后：浏览器元素拾取器

元素拾取器需要浏览器注入、跨 iframe 通信、候选 selector 生成与评分以及
页面高亮，工程范围远大于本次需求。它可以在首版稳定后作为第二阶段能力。

## 4. 总体架构

GUI 使用水平 `QSplitter`：

```text
┌──────────────────────────────┬─────────────────────────┐
│ processed 脚本编辑器          │ 复刻标注面板             │
│                              │                         │
│                              │ Marker / Action 列表     │
│                              │ Locator 编辑区           │
│                              │ iframe 链预览            │
│                              │ 修改前风险 → 修改后风险   │
└──────────────────────────────┴─────────────────────────┘
```

功能拆分为四个边界：

1. `locator_risk.py`：统一风险分类。
2. `rewrite_script.py`：解析源码范围、校验 locator、原子替换。
3. `replica_annotation_panel.py`：独立 Qt 标注组件。
4. `main_gui.py`：连接代码编辑器、录制状态、保存状态和面板信号。

## 5. 解析与源码范围

### 5.1 SourceSpan

源码范围只属于解析和编辑阶段，不写入最终 `ActionTarget` 或复刻 manifest。

`rewrite_script.py` 增加：

```python
@dataclass(frozen=True)
class SourceSpan:
    start_line: int
    start_col: int
    end_line: int
    end_col: int


@dataclass
class ActionPlan:
    bootstrap: BootstrapPlan
    marker_groups: list[MarkerGroup]
    popup_expectations: list[PopupExpectation]
    instrumented_source: str
    locator_source_spans: dict[str, SourceSpan]
```

对于：

```python
page.locator("#confirm").click()
```

范围对应 `call.func.value`，即 `page.locator("#confirm")`，不包含 `.click()` 和
动作参数。

### 5.2 UTF-8 列偏移

Python AST 的 `col_offset` 和 `end_col_offset` 是 UTF-8 字节偏移。源码替换工具
必须把每行的字节列转换为 Python 字符索引，不能直接切片。测试必须覆盖中文
selector 和中文 accessible name。

### 5.3 动作身份

面板在一次有效解析中使用 `action_id` 定位动作。Locator 替换不改变动作顺序、
动作类型或 Marker 位置，因此重新解析后的对应动作应保持同一 `action_id`。
Marker 身份继续使用 GUI 已有的稳定 UUID。

如果重新解析后目标动作不存在、动作类型变化或 Marker UUID 无法保留，事务
失败且不更新源码。

## 6. Locator 编辑事务

`rewrite_script.py` 提供两个公开纯函数：

```python
def parse_locator_expression(expression: str) -> LocatorRecipe

def replace_action_locator(
    source: str,
    action_id: str,
    expression: str,
    marker_annotations: Sequence[Mapping[str, object]] | None = None,
) -> str
```

`parse_locator_expression()` 的约束：

- 输入必须是一个 Python expression。
- 必须能解析为支持的 `LocatorRecipe`。
- root 变量必须是原动作使用的 `page`、`page1` 等页面变量。
- locator 和 iframe selector 参数必须是静态字面量。
- 不允许输入 `.click()`、`.fill()` 等动作调用；编辑目标仅为 receiver。

`replace_action_locator()` 执行以下事务：

1. 解析原脚本和原动作。
2. 解析并验证新 locator expression。
3. 检查页面变量与原动作一致。
4. 通过原动作 `SourceSpan` 在内存中生成候选脚本。
5. 对候选脚本执行 `ast.parse()`。
6. 使用相同 Marker annotations 重新运行 `parse_action_plan()`。
7. 确认目标动作仍存在、动作类型未改变并能生成 locator。
8. 成功时返回完整候选脚本；失败时抛出带用户可读原因的异常。

调用方只有在函数成功返回后才更新编辑器，因此不存在半应用状态。

## 7. 风险分类统一

新建 `locator_risk.py`，提供唯一的：

```python
def classify_locator_risk(target: ActionTarget) -> str
```

风险从低到高：

| 风险 | 判定 |
|---|---|
| `stable_id` | 精确 ID selector |
| `aria` | role、label、title 等语义 locator |
| `stable_attribute` | test id、name 等稳定属性 |
| `text` | `get_by_text` 等纯文本定位 |
| `ordinal` | `.first`、`.last`、`.nth()` |
| `structural` | 层级、位置型或复杂结构 CSS |
| `coordinate` | 鼠标绝对坐标动作 |
| `non_locator` | 键盘等不以元素为目标的动作 |

以下位置全部改为调用共享函数：

- `rewrite_script.locator_risk_report()`
- `build_replica._locator_risk_metadata()`
- `pipeline_validation.validate_locator_risk()`
- GUI 标注面板

分类必须基于明确规则，而不是把所有属性 selector 判为 `structural`，也不能把
`get_by_text` 与 role locator 合并为同一档。

风险只表示静态稳定性，不承诺真实页面上的唯一性或可见性。最终成功状态仍以
捕获和离线验证结果为准。

## 8. GUI 交互

### 8.1 Action 列表

面板按 Marker 分组展示动作：

```text
▼ 序列选择
  a_001  dblclick  L68  ordinal
▼ Meta 信息工具
  a_001  click     L72  structural
  a_002  click     L73  stable_id
▼ 影像画布交互
  a_001  click     L80  coordinate
```

列表支持“只看高风险”。风险必须同时使用文字与颜色表达，不能只依赖颜色。

选择 locator 动作时：

- 左侧编辑器跳转并选中 receiver 的 `SourceSpan`。
- 下方显示动作类型、页面变量、iframe 链和完整表达式。
- 编辑框载入原始源码片段，而不是 `ast.unparse()` 后的格式化结果。

选择坐标动作时：

- 显示原始坐标和 `coordinate` 风险。
- 编辑器只读。
- 明确提示开发人员在左侧手动改写整条动作。

### 8.2 即时预览

编辑 locator 时只解析当前表达式，不立即修改脚本。成功时显示解析后的 iframe
链和风险变化；失败时显示具体错误，并禁用“应用”。

```text
风险：ordinal → text
```

支持“恢复”和“应用”，`Ctrl+Enter` 等同于应用。

### 8.3 源码同步

左侧代码手动变化后，以约 300ms 防抖重新运行 `parse_action_plan()`：

- 解析成功：刷新 Action 列表。
- 解析失败：保留最后一次有效列表作为只读参考，显示“当前源码无法解析”，并
  禁用应用。

应用成功后使用统一的 `set_editor_source()` 更新：

- `code_view`
- `_latest_code`
- `_display_items`
- `_marker_anchors`
- Marker UUID 映射
- 保存和导出按钮状态

该同步入口必须支持多行数量变化，不能只更新 `_latest_code`。

### 8.4 录制和保存状态

- 录制期间面板只读，避免 codegen 推送与人工修改竞争。
- 停止录制后面板启用。
- 应用修改后脚本进入未保存状态。
- 现有导出规则继续生效：必须先保存处理后脚本，才能运行复刻导出。

## 9. 错误处理

面板在编辑区下方显示可操作错误：

- Python expression 语法错误。
- 不支持的 locator 方法。
- selector 参数不是静态字面量。
- iframe 链无法解析。
- 页面变量与原动作不一致。
- 完整候选脚本无法解析。
- 目标动作在重新解析后消失。
- 动作类型发生变化。
- Marker UUID 或锚点无法安全保留。

错误不得通过模糊的“修改失败”统一吞掉。事务失败时原源码、保存状态和面板
选择保持不变。

## 10. 测试设计

### 10.1 纯逻辑测试

- 单行 receiver 的 `SourceSpan` 正确。
- 多行 receiver 的 `SourceSpan` 正确。
- 中文 selector 和 accessible name 的字符索引正确。
- popup `page1` 和嵌套 iframe 链正确。
- 替换只影响 receiver，不改变动作方法、参数和相邻代码。
- 非法表达式不会产生候选源码。
- 动态 locator 参数被明确拒绝。
- 页面变量变化被拒绝。
- 替换后 Marker UUID 保持不变。
- 七种 locator 风险和 `non_locator` 分类符合表格定义。
- GUI、构建与验证消费相同风险结果。

### 10.2 GUI 测试

- 录制期间面板只读，停止后启用。
- Marker/Action 树与 `ActionPlan` 一致。
- 选择动作能跳转并选中正确源码范围。
- 编辑过程中即时更新风险预览。
- 应用成功后编辑器、`_latest_code`、`_display_items` 和 anchors 同步。
- 应用失败后源码保持不变。
- 修改后导出按钮要求重新保存。
- 坐标动作只读并显示转换提示。
- 左侧源码暂时无效时面板不崩溃。

### 10.3 回归测试

- `cxhospital` 嵌套 iframe 和 ordinal 解析不退化。
- `uicloud` popup、`page1` 和文本 locator 不退化。
- Marker 插入、删除、稳定 UUID 和锚点重定位不退化。
- 捕获插桩、复刻构建和离线验证继续通过。
- 完整单元测试套件通过。

## 11. 实施顺序

1. 用测试固定统一风险分类，增加 `locator_risk.py` 并迁移所有消费者。
2. 用测试增加 `SourceSpan`、UTF-8 列转换和 locator expression 解析。
3. 实现并测试 `replace_action_locator()` 原子事务。
4. 实现 `ReplicaAnnotationPanel` 及组件级测试。
5. 在 `main_gui.py` 中接入 splitter、录制状态和源码跳转。
6. 实现统一 `set_editor_source()`，覆盖多行编辑后的 Marker/anchor 同步。
7. 运行医院 fixture 回归及完整测试套件。

## 12. 验收标准

功能完成必须同时满足：

1. 开发人员无需在完整脚本中搜索，即可定位所有 Marker 下的 locator 动作。
2. 可以安全修改单行或多行 locator 并补充 iframe 链。
3. 修改前后风险即时可见，所有模块的分类结果一致。
4. processed 脚本仍是唯一事实来源。
5. 失败修改不会改变脚本或 GUI 保存状态。
6. 保存后的脚本能通过 `parse_action_plan()`、捕获管道和离线复刻验证。
7. 将高风险 locator 改为稳定 locator 后，对应 `risk_counts` 确实改善。
8. 现有 Marker、popup、嵌套 iframe 和 GUI 导出行为无回归。
