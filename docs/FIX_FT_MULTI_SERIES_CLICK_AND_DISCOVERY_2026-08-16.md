# FTImage 多序列自动发现与离线点击缺陷修复说明

> 日期：2026-08-16  
> 状态：代码修复已实施并经复核验证（2026-08-16 复核：`test_replica_regions` 17/17、`test_build_replica` 18/18、旧副本 6/6 route 均含几何、真实坐标点击 `(100,630)` 命中 `66f1f366f470` 并跳转 `bviewer_b002`、console 无异常）；旧捕获结果已离线重建；完整 8/8 序列仍需重新录制验收  
> 缺陷等级：P1（核心能力部分失效，但不涉及数据破坏）  
> 影响范围：FTImage 文本身份回退型序列列表，以及所有由 series region 生成的离线透明点击层

## 1. 结论

这是代码实施缺陷，不是 FTImage 站点本身的偶发故障。

FTImage 页面确实存在动态下载进度，但我们的代码有两项不正确的实现假设：

1. 把序列行完整可见文本当成稳定身份，未剔除会变化的下载进度。
2. 把序列节点写入离线副本时，只绑定了路由 key，没有恢复节点的绝对坐标和尺寸。

这两项缺陷分别造成：

- 实际 8 个序列被识别成 9 个；最后一个序列在进度变化后无法重新定位。
- 已经成功捕获的其他序列虽然有路由，但透明点击层没有覆盖截图中的真实行，用户按画面点击时无反应或命中错误节点。

修复必须同时覆盖“在线捕获阶段”和“离线副本构建阶段”。只修其中一处不能完整解决问题。

## 2. 用户可见症状

本次 FT 录制运行：

```text
out/ftimage/runs/20260815T165212Z-539277
```

用户观察到：系统似乎能够发现其他序列，但不能正常点击或录制全部序列。

运行清单显示：

| 指标 | 结果 |
|---|---:|
| 页面真实序列行 | 8 |
| 系统发现 descriptor | 9 |
| 成功捕获 | 7 |
| 失败 | 1 |
| 因预算跳过 | 1 |
| `overall_ok` | `false` |

失败分支为 `b007_c10e9ba181a7`，失败阶段为 `locate`，错误分类为 `hub_unrecoverable`。所谓第 9 个分支 `b008_60e64e6d3ec8` 并不是新序列，而是同一个 MPR-Sag 序列在下载进度变化后的重复身份。

## 3. 正确行为与实际行为

### 3.1 正确行为

```text
8 个真实序列行
  -> 8 个稳定 descriptor
  -> 每个 descriptor 均可重新定位原行
  -> 逐序列激活和捕获
  -> 离线副本中每个可用序列行都有与截图对齐的点击区域
```

### 3.2 缺陷行为

```text
MPR-Sag 行：共 131张 + 动态进度 106
  -> descriptor A

同一行稍后变为：共 131张 + 动态进度 109
  -> descriptor B（错误地当成新序列）

随后按 descriptor A 的全文重新定位
  -> 页面文本已变为 109
  -> 精确匹配失败
  -> hub_unrecoverable
```

离线构建阶段则是：

```text
Series route 已生成
  -> data-replica-series-key 存在
  -> 但节点缺少 position/left/top/width/height
  -> 透明节点没有覆盖背景截图中的序列行
  -> 用户按截图坐标点击不到对应 route
```

## 4. 根因分析

### 4.1 根因 A：动态展示文本被当成稳定序列身份

FTImage 的序列行没有可直接使用的稳定 `id` 或 `data-series-*` 属性，因此配置使用：

```yaml
item_selector: "a:has(span.total)"
identity_attrs: []
```

`identity_attrs: []` 表示发现逻辑回退到文本身份。旧实现只做空白压缩和小写转换：

```python
" ".join(text.split()).lower()
```

这会把下面两段文本视为不同身份：

```text
3.0 x 3.0 MPR-Sag_bone 共 131张 106
3.0 x 3.0 MPR-Sag_bone 共 131张 109
```

旧运行中的两个 descriptor 证据为：

| ordinal | 稳定部分 | 动态尾数 | 旧 `series_key` 结果 |
|---:|---|---:|---|
| 7 | `MPR-Sag_bone 共 131张` | 106 | `...131张 106::x0` |
| 8 | `MPR-Sag_bone 共 131张` | 109 | `...131张 109::x0` |

同一个动态文本问题同时存在于两个路径：

- 发现去重：决定是否创建新的 descriptor。
- 激活重定位：决定当前 DOM 行是否等于目标 descriptor。

如果只修发现去重、不修激活匹配，仍会出现“发现是 8 个，但点击时找不到最后一行”。因此两条路径必须共享同一个标准化函数。

### 4.2 根因 B：离线 route 节点遗漏几何定位

普通 overlay 节点通过 `_positioned_html()` 写入：

```text
position:absolute
left
top
width
height
```

但 series route 使用独立的 `_series_member_html()`。旧实现只注入：

```html
data-replica-series-key="..."
role="option"
aria-selected="..."
```

没有注入 `DomNodeSnapshot.rect` 中的几何信息。

由于离线副本的真实界面是背景截图，DOM overlay 默认透明，点击能力完全依赖 overlay 与截图坐标重合。缺少绝对定位后，即使路由表完全正确，肉眼点击仍无法命中正确序列。

### 4.3 根因 C：series route 与旧 action overlay 的层级竞争

部分状态中，同一位置可能同时存在：

- 录制脚本产生的旧 action overlay；
- 自动扩展产生的 series route overlay。

旧 action overlay 使用 `z-index: 1`，series route 没有明确层级。浏览器可能让旧 action 覆盖 route，尤其在进入后续 Viewer 状态后，旧 action 已无有效 transition 时会表现为“点击无反应”。

因此修复同时把 series route 层级设为 `z-index: 2`，让自动扩展路由优先处理序列点击。

## 5. 为什么判定为实施缺陷

| 判断维度 | 结论 |
|---|---|
| 外部页面是否出现动态值 | 是，这是正常页面行为 |
| 系统是否声明支持文本 fallback 和虚拟滚动列表 | 是 |
| 实现是否把不稳定字段错误纳入身份 | 是 |
| 离线 route 是否已有正确目标 URL | 是 |
| route 节点是否缺少必要点击几何信息 | 是 |
| 是否可通过共享代码修复 | 是 |

外部动态进度只是触发条件。稳定身份提取、重复去除、目标重定位和透明点击层定位都属于本系统职责，所以归类为代码实施缺陷。

## 6. 修复方案

### 6.1 统一稳定文本标准化

在 `capture_snapshot.py` 新增公共函数：

```python
def normalize_series_text(text: str) -> str:
    normalized = " ".join((text or "").split())
    normalized = re.sub(
        r"(\d{1,6}\s*(?:幅|帧|张|frames?|images?))\s+\d{1,6}$",
        r"\1",
        normalized,
        flags=re.IGNORECASE,
    )
    return normalized.lower()
```

该规则只在以下条件全部满足时删除尾数：

1. 文本中已出现明确的总帧数单位，如 `131张`、`80帧` 或 `41 images`。
2. 总帧数之后还有一个独立的末尾整数。
3. 整数位于整段文本末尾。

这样能删除 FTImage 的动态下载进度，同时不会删除序列名称中的层厚、总帧数或普通编号。

示例：

| 输入 | 标准化结果 |
|---|---|
| `MPR-Sag 共 131张 106` | `mpr-sag 共 131张` |
| `MPR-Sag 共 131张 109` | `mpr-sag 共 131张` |
| `Body 1.0 CE 共 400张` | `body 1.0 ce 共 400张` |
| `Series 109` | `series 109`，不删除 |

### 6.2 发现和激活共用同一规则

`batch_capture_replicate.py` 不再维护另一套文本标准化逻辑，而是调用 `capture_snapshot.normalize_series_text()`。

覆盖路径：

- `_series_identity()` 的文本 fallback 去重；
- descriptor `series_key` 生成；
- `_matches_descriptor()` 激活前重新定位；
- `_series_descriptor_matches()` 分支 route 绑定；
- readiness 中的当前序列 label 对比。

这保证“发现时认为相同”与“点击时认为相同”使用相同语义。

### 6.3 为 series route 恢复点击几何

`build_replica.py::_series_member_html()` 现在从 `snapshot.rect` 注入：

```python
style = (
    f"position:absolute;left:{snapshot.rect.x}px;top:{snapshot.rect.y}px;"
    f"width:{snapshot.rect.width}px;height:{snapshot.rect.height}px;"
)
```

生成结果示例：

```html
<a
  data-replica-series-key="66f1f366f470"
  style="position:absolute;left:15px;top:597px;width:169px;height:66px;"
  role="option"
>
```

用户点击背景截图中的 `(x, y)` 时，浏览器现在会命中与该行完全重合的透明 route 节点。

### 6.4 提高 series route 点击优先级

离线页面 CSS 增加：

```css
.overlay > [data-replica-series-key] { z-index: 2; }
```

旧 action overlay 保持 `z-index: 1`。不改变非序列 action 的行为，只确保序列 route 在重叠区域优先。

## 7. 代码变更清单

| 文件 | 变更 | 目的 |
|---|---|---|
| `capture_snapshot.py` | 新增 `normalize_series_text()` | 去除稳定总帧数后的动态尾进度 |
| `batch_capture_replicate.py` | 激活匹配复用公共标准化函数 | 防止发现与点击语义不一致 |
| `build_replica.py` | series route 注入绝对坐标与尺寸 | 让透明点击层覆盖背景截图 |
| `build_replica.py` | series route 设置 `z-index: 2` | 避免被旧 action overlay 覆盖 |
| `test/test_replica_regions.py` | 新增动态 `106 -> 109` 回归测试 | 锁住 8 个真实序列不会膨胀为 9 个，且仍可重定位 |
| `test/test_build_replica.py` | 增加 route 几何断言 | 锁住绝对定位信息不再丢失 |

修复保持在共享捕获/构建逻辑中，没有直接修改某个 `completed_*.py` 或手工篡改 manifest。

## 8. 回归测试设计

### 8.1 动态进度身份测试

测试构造一个匿名 FTImage 风格列表，最后一行 `innerText` 连续读取时从：

```text
MPR-Sag_bone /共 131张 106
```

变化为：

```text
MPR-Sag_bone /共 131张 109
```

断言：

1. `discover_series_candidates()` 仍只返回 8 个 descriptor。
2. `evidence.discovered_count == 8`。
3. 使用第一次发现的 descriptor，在进度变化后 `_locate_series_row()` 仍能找到原行。

这同时覆盖去重和激活重定位，避免只修一半。

### 8.2 离线点击层几何测试

构造一个 rect：

```text
x=11, y=22, width=100, height=30
```

断言 `_series_member_html()` 输出包含：

```html
style="position:absolute;left:11px;top:22px;width:100px;height:30px;"
```

### 8.3 当前 FT 副本真实坐标测试

使用旧捕获数据重新运行 offline build 后，以 1696×880 视口打开：

```text
out/ftimage/runs/20260815T165212Z-539277/replica/index.html
```

直接执行屏幕坐标点击：

```text
(100, 630)
```

命中结果：

```text
data-replica-series-key = 66f1f366f470
```

页面成功跳转到：

```text
states/bviewer_b002_fd19ce457cc2/index.html
```

这证明修复不是仅通过 Playwright locator 绕过布局问题，而是用户按截图实际坐标也能点击。

## 9. 已完成验证

| 验证项 | 结果 |
|---|---|
| 修改文件 `py_compile` | 通过 |
| series route 隐私/几何单测 | 2/2 通过 |
| FT 动态进度回归测试 | 通过 |
| FT 既有发现测试 | 通过 |
| FT 配置化激活定位测试 | 通过 |
| 旧捕获数据 offline rebuild | 通过 |
| 重建副本真实坐标点击 | 通过 |
| 重建入口 route 几何检查 | 6/6 route 节点均已定位 |

扩展测试运行中，`test_build_replica + test_replica_runtime` 共 23 项有 22 项通过；剩余一项 `test_series_option_click_updates_aria_selected` 等待 `#two`，与现有隐私逻辑会移除 series 原始 `id` 的契约不一致，不属于本缺陷修复路径。该项未作为本修复通过条件，也没有为通过它而放宽隐私规则。

## 10. 旧运行为什么不能被完全修复

代码修复后可以对旧 capture 执行 offline rebuild，恢复已捕获分支的点击层，但不能把失败分支补成成功分支。

原因：旧运行已经持久化了以下事实：

- MPR-Sag 被拆成两个 descriptor；
- ordinal 7 在 `locate` 阶段失败；
- ordinal 8 被当成额外分支并因预算跳过；
- 没有可用的 MPR-Sag Viewer/Metadata 快照。

offline build 只能消费已有快照，不能重新访问真实站点或生成不存在的影像状态。因此当前重建结果可以正常点击旧 run 已捕获的序列，但完整 8/8 必须重新录制。

## 11. 重新录制验收步骤

1. 使用包含本修复的工作区重新录制同类 FT 检查。
2. 开启自动扩展全部序列。
3. 等待 series expansion 完成，不在下载进度变化期间手工干预列表。
4. 检查新的 `series_capture_manifest.json`。
5. 检查离线副本中每个序列的屏幕点击。
6. 分别打开至少两个序列的 Metadata，确认内容对应当前序列。

必须满足：

```text
discovered_count = 8
captured_count + partial_count = 8
failed_count = 0
skipped_count = 0
count_conserved = true
overall_ok = true
```

离线交互验收：

- 点击第 1、2、最后一个可见序列，均进入不同 Viewer 状态；
- 当前序列 `aria-selected=true`，其他序列为 false；
- Viewer B 打开 Metadata 后显示 B 的数据；
- 关闭 Metadata 返回 Viewer B，而不是其他分支；
- 对需要滚动才可见的 MPR-Sag，滚动后仍能点击；
- 浏览器控制台无 route 缺失或 JavaScript 异常。

## 12. 风险和兼容性

### 12.1 文本标准化规则风险

规则只移除“明确总帧数单位之后的末尾整数”，不会全局删除数字。对有稳定属性的 viewer（例如使用 SeriesInstanceUID 的 zscloud）没有影响，因为其身份优先来自属性，不走文本 fallback。

### 12.2 点击层风险

series route 现在与普通 action overlay 一样使用 `snapshot.rect`。这是离线截图 overlay 的既有坐标模型，不引入新的坐标系。

若未来某 viewer 的 series rect 使用滚动内容坐标且离线页面支持独立滚动，需要继续处理 `scrollTop` 映射；本次 FT 首屏已通过真实坐标验证，折叠以下项目列入后续增强而非本次阻塞项。

### 12.3 既有 action 与 route 共存

本次通过层级保证 route 优先。入口页中录制时原始选中行仍可能保留 legacy action transition；已捕获的其他行使用 series route。后续可以进一步统一为“序列区域内 route 优先、action 仅保留审计映射”，但不应在本修复中扩大重构范围。

## 13. 回滚方案

若发现标准化误合并不同序列：

1. 回滚 `normalize_series_text()` 的尾进度规则。
2. 保留点击层几何修复，因为它与身份逻辑独立。
3. 为 FTImage 增加更精确的 `label_selector`，只读取 `.desc + .total`，不读取 `.dlCount`。
4. 增加对应真实结构的匿名 fixture 后再启用。

若发现点击层覆盖错误：

1. 保留动态文本修复。
2. 对出现问题的 viewer 检查 `rect.coordinate_space`、容器 `scrollTop` 和 iframe 偏移。
3. 不要回退为无定位 route；应修正坐标转换并添加按坐标点击测试。

## 14. 后续建议

### 必须完成

- 重新录制 FT，用新 run 验收 8/8。
- 将新 run 的 `series_capture_manifest.json` 与本次旧 run 对比。
- 点击最后一个 MPR-Sag 并验证对应 Metadata。

### 建议增强

- 为 viewer 配置增加可选 `label_selector`，FT 可明确选择 `.desc` 与 `.total`。
- 在 pipeline validation 中增加“同一稳定 label 只有一个 descriptor”的告警。
- 在构建验证中检查每个可用 branch 至少有一个定位后的 `data-replica-series-key` 节点。
- 增加真实坐标点击 smoke test，避免仅用 locator 测试掩盖 overlay 错位。
- 整理 `test_series_option_click_updates_aria_selected` 与 series 隐私去 id 规则之间的旧测试契约。

## 15. 关键文件与证据索引

### 实现

- `capture_snapshot.py::normalize_series_text`
- `batch_capture_replicate.py::_normalize_series_text`
- `batch_capture_replicate.py::_matches_descriptor`
- `build_replica.py::_series_member_html`
- `build_replica.py::_render_document`

### 测试

- `test/test_replica_regions.py::test_ft_dynamic_download_count_does_not_duplicate_or_break_row_matching`
- `test/test_build_replica.py::SeriesMemberPrivacyTests::test_series_member_removes_identity_from_root_and_descendants`

### 旧运行证据

- `out/ftimage/runs/20260815T165212Z-539277/capture/series_branches/series_capture_manifest.json`
- `out/ftimage/runs/20260815T165212Z-539277/capture/series_branches/b007_c10e9ba181a7/descriptor.json`
- `out/ftimage/runs/20260815T165212Z-539277/capture/series_branches/b008_60e64e6d3ec8/descriptor.json`
- `out/ftimage/runs/20260815T165212Z-539277/pipeline_events.jsonl`
- `out/ftimage/runs/20260815T165212Z-539277/replica/index.html`

## 16. 关闭标准

本缺陷只有在以下条件全部满足后才能关闭：

- [x] 根因 A：动态尾进度不再参与稳定身份。
- [x] 发现与激活使用同一标准化规则。
- [x] series route 节点包含绝对坐标和尺寸。
- [x] series route 高于重叠的旧 action overlay。
- [x] 匿名动态进度回归测试通过。
- [x] 旧捕获副本重建后真实坐标点击通过。
- [ ] 新 FT 录制发现数量为真实 8，不再是 9。
- [ ] 新 FT 录制 8 个序列全部 captured/partial，且无 failed/skipped。
- [ ] 新离线副本最后一个 MPR-Sag 可点击并能打开对应 Metadata。

在最后三项完成前，代码修复可以视为完成，但真实站缺陷状态应标记为“待重新录制验证”，不应标记为完全关闭。
