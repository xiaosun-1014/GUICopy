# 交接：真实站（FTImage / 中山 zscloud）多序列发现适配 + 真实验证

> 状态：**Steps 1–5 已全部完成并验证（12 套件 140 测试全绿；FTImage / 中山 zscloud 两站真实站整链验证通过，见 §10）；所有改动已随 commit `2e9620f` 提交（25 文件 +1954/−93），随后叠加一轮 review 修复（C1 激活路径 / I2 / I3 / I4 + 回归测试，见 §9）；仅剩 Step 6 关闭验收与本文档收尾。**
> 本文档给接手者完整上下文：已完成什么、验证到什么程度、还剩什么、怎么做、怎么验收、有什么坑。
>
> 关联计划：`docs/superpowers/plans/2026-08-14-multi-series-replica-expansion.md` Task 0.2（真实站 Spike，实际在 FTImage / 中山 zscloud 两站完成）；
> Spike SOP：`docs/MULTI_SERIES_REAL_SITE_SPIKE_SOP.md`。

---

## 1. 目标

让两个真实 DICOM viewer 走通「**录一个序列 → 自动发现其余序列 → 逐序列采集各自 Metadata → 形成离线可点击的完整复刻**」这条链路，并验证 **per-series Metadata 互不相同**（即每个序列的 Meta 面板内容是它自己的）。

用户原话关注点：**序列选择**（自动发现+可点击）与**选择不同序列后要有对应的 metadata 信息**。

判断标准（Step 5 真实验证）：`series_branches/` 里 ≥2 个分支的 `metadata_rows.json` 的 SeriesNumber / SeriesDescription 互不相同；离线复刻点序列 B → Meta 是 B → 关回 B。

---

## 2. 已完成：代码与配置（Steps 1–4）

> 与上一版交接文档（只有 capture_snapshot 半成品、其余未做）相比，本版把 Steps 1–4 全部落地并验证。核心改动集中在 6 个文件（`capture_snapshot.py` / `batch_capture_replicate.py` / `skills/_shared/viewers.yaml` 三改 + `test/test_capture_and_build_series_contract.py` / 2 个 fixture 三新增），另有同批一并合入的既有修复（`build_replica.py` / `capture_readiness.py` / `pipeline_orchestrator.py` / `pipeline_validation.py` / `replay_helpers.py` / `rewrite_script.py` 等，见 §9 提交说明）与文档/memory/fixture。

### 2.1 `capture_snapshot.py` — 发现算法可配置化（向后兼容）

`_SERIES_ITEM_SELECTOR` / `_SERIES_IDENTITY_ATTRS` 常量保留为默认；新增可选参数，**不传 = 现行为逐字节不变**：

| 函数 | 新增参数 | 行号 |
|---|---|---|
| `_series_stable_attributes(snapshot, identity_attrs=None)` | `identity_attrs` | capture_snapshot.py:209 |
| `_series_identity(snapshot, identity_attrs=None)` | `identity_attrs` | capture_snapshot.py:219 |
| `discover_series_candidates(root, doc_id, …, item_selector=None, identity_attrs=None)` | `item_selector` / `identity_attrs` | capture_snapshot.py:255 |
| `capture_series_interaction_region(root, doc_id, …, item_selector=None, identity_attrs=None)` | 透传 | capture_snapshot.py:360 |
| `capture_marker_interaction_region(scope, marker_label, doc_id, …, item_selector=None, identity_attrs=None)` | `series` 分支透传 | capture_snapshot.py:405 |

### 2.2 `batch_capture_replicate.py` — viewer 配置注入

- **`_SERIES_VIEWERS_YAML`**（batch_capture_replicate.py:374）：模块常量指向 `skills/_shared/viewers.yaml`，便于测试 patch。
- **`_series_viewer_config_for(page)`**（:377）：按 `page.url` 子串匹配 `viewers[<name>].url_patterns` → 返回 `sequence_select` 里的 `item_container_selector / item_selector / identity_attrs`；**缺字段省略 / 任何异常或未知 URL → `{}`**（绝不因配置坏中断捕获）。
- **`_series_scope_root(target_locator, container_selector=None)`**（:414）：有 `container_selector` 时在 target 所在 frame（`ancestor::html`）内取**第一个可见**匹配，失败 fallback 到原 candidate/body 逻辑（ft 的 `div.os-viewport` 有 2 个；zs 容器在 viewer 第二层 iframe 内，天然被 frame 限定）。
- **`LiveCaptureSession._series_cfg` + `_ensure_series_cfg(page)`**（:618）：session 级缓存，首次拿到有 URL 的 page 时加载一次；空结果不缓存（后续可重试）。
- **`_reparse_series_root(recipe, page, container_selector=None)`**（:1153）：透传给 `_series_scope_root`。
- **`_locate_series_row(..., item_selector=None)`**（:553，review C1 修复）：激活路径重新定位序列行时同样使用配置的 `item_selector`，否则 FTImage 的 `a:has(span.total)` 行默认选择器匹配不到、批量激活全失败。⚠️ 行号为当前工作区（含 C1 修复）状态。

注入点（全链路覆盖，`cfg` 为空时全 None → 原逻辑）：

| 位置（方法） | 注入内容 | 行号（当前工作区） |
|---|---|---|
| `_capture` 主快照，`marker_label=="序列选择"`（top-page + frame 两个分支） | `item_selector`/`identity_attrs` → `capture_marker_interaction_region` | :683 / :703 |
| `_capture` / `before` / `after` / `expand_series` | `_ensure_series_cfg(page)` | :642 / :731 / :736 / :756 |
| `_capture_series_region`（def :776） | `item_selector`/`identity_attrs` → discover | :799（调用 :836） |
| `capture_one_series` 主路径 root（def :853） | `container` → `_series_scope_root` + 定位 `item_selector` | :907 / :917 |
| `capture_one_series` retry | `container` → `_reparse_series_root` + 定位 `item_selector` | :941 / :944 |
| `_wait_for_series_ready` 轮询 reload（def :1101） | `container` → `_reparse_series_root`；`item_selector` → `_reparse_target_row` | :1126 / :1131 |
| `finalize_series_branches`（def :1453） | `_ensure_series_cfg` + `container` → root & discover + `item_selector` | :1476 / :1490 / :1500 |
| `_snapshot_hub_state`（def :1831）恢复 re-discover | `item_selector`/`identity_attrs` | :1836 |
| `_restore_hub_state`（def :1877）恢复 re-discover | `item_selector`/`identity_attrs`（含 `_locate_series_row` :1919） | :1910 / :1919 |

> **同批合入的既有修复**（非本步骤新逻辑，但同一 diff）：`_capture` 的 frame-owner 探测改 `target_locator.first.evaluate(...)`（:663），避免多元素 locator（如序列列表命中多行）strict-mode 抛掉整个主快照。记忆见 `memory/multi-series-subprocess-mainpath-series-region-bug.md`，契约测试见 `test/test_capture_and_build_series_contract.py`（本批新增，见 2.5）。

### 2.3 `skills/_shared/viewers.yaml` — 两站条目

在 `generic` 之前插入了 `ftimage` / `zscloud` 两个 viewer（**只改 `_shared` 共享份**，`.reasonix/viewers.yaml` 旧副本不动）：

- **ftimage**（url `yyx.ftimage.cn`）：`item_container_selector: "div.os-viewport"`、`item_selector: "a:has(span.total)"`、`identity_attrs: []`（文本 fallback）；`meta_panel.open_button_names: ["更多","Tags"]`、`close_button_selectors: ["#tagsBox a.close"]`。
- **zscloud**（url `zscloud.zs-hospital.sh.cn`）：`item_container_selector: "#HLeftThumnail"`、`item_selector: "li.ui-draggable"`、`identity_attrs: ["id"]`；`meta_panel.open_button_names: ["DICOM信息 F2"]`、`close_button_selectors: [".ui-dialog-titlebar-close"]`。

不含任何真实病人文本/token/UID（安全红线见 §5）。

### 2.4 `test/fixtures/multi_series/` — 匿名 fixture（新增 2 个）

- **`ft_series_list.html`**：第一个 `div.os-viewport` 内 8 个 `a > div.desc > span.total`（第 8 行滚动后可见），**无** id/data-\* 身份属性；页面另含第 2 个无序列行的干扰 `div.os-viewport`。
- **`zs_series_list.html`**：`div.StudyList#HLeftThumnail` 内 4 个 `li.ui-draggable[id]`（虚构 UID `1.2.826.0.1.3680043.201.1001…1004`，第 2 行 `select` 选中态）；容器外另放 2 个 `li.ui-draggable`（病人头/检查 LI，须不计入）。
- **`README.md`**：追加两 fixture 的结构说明、发现调用示例、已知边界（zs 的 `selected` 字段恒 False——当前 `_series_selected` 只按属性判定、不看 class 令牌）。

### 2.5 单测（新增）

- **`test/test_replica_regions.py`**（+152 行，提交时新增 7 个测试 + review 修复补 1 个 C1 回归锁 = 现 8 个新测试）：
  - `test_ft_fixture_discovery_uses_a_span_total_selector` — 8 个、key 唯一、reached_end；**默认选择器命中 0**（证明必须显式传 selector）
  - `test_ft_fixture_interfering_viewport_counts_nothing` — 干扰容器 0
  - `test_zs_fixture_discovery_scopes_to_studylist_container` — 4 个、虚构 UID key 唯一、容器外 li 不计入
  - `test_series_scope_root_prefers_first_visible_configured_container` — `_series_scope_root(..., "div.os-viewport")` 命中第一个容器
  - `test_series_viewer_config_matches_known_viewer_urls` — ft/zs URL 匹配
  - `test_series_viewer_config_unknown_url_or_missing_url_returns_empty` — 未知/缺 url → `{}`
  - `test_series_viewer_config_returns_empty_on_broken_yaml` — 坏 YAML / 缺文件 → `{}`（patch `_SERIES_VIEWERS_YAML`）
  - `test_activation_path_locate_series_row_uses_configured_item_selector` — **（review C1 回归锁）**：ft 结构下 `_locate_series_row` / `_reparse_target_row` 不传 `item_selector` 命中 0、传配置后能定位，锁住激活路径必须接配置
- **`test/test_capture_and_build_series_contract.py`**（~390 行，整链契约测试）：子进程 production 管线（`capture_and_build` + `replica_annotations.json` + `REPLICA_EXPANSION_CONFIG` → 插桩脚本 → 真实 `series_branches` → v2 manifest → `build_replica` → 离线浏览器点击）。3 个测试：真实 capture-build 分支 manifest + 离线点击（**含 review 补的 per-series metadata 互异显式断言**）、stale annotation 拒绝、offline entry 可点击。**这是本批最重要的覆盖**——只有子进程整链才能测到 `session.before/after` 主路径。

---

## 3. 验证状态（2026-08-15 实测）

> 用 `-m unittest <module>` 逐个跑（**不要 `unittest discover`**，会把含浏览器集成的套件一起拉挂）。测试里所有浏览器都 `with sync_playwright()` 局部启动，无模块级挂起。

| 套件 | 结果 |
|---|---|
| `test_replica_regions`（含 8 个新 ft/zs/config/C1 回归锁测试） | 16/16 ok |
| `test_capture_and_build_series_contract`（子进程整链） | 3/3 ok |
| `test_capture_snapshot` | 9/9 ok |
| `test_multi_series_capture` + `test_multi_series_budget` + `test_branch_topology_fixes` | 24/24 ok |
| `test_batch_capture_replicate`（55 个，改动直接影响） | 55/55 ok |
| `test_replica_runtime` / `test_replica_e2e` / `test_build_replica` / `test_replica_manifest` / `test_replica_topology` | 5+3+16+8+1 = 33/33 ok |

**合计 12 套件 140 测试全绿。** 这已覆盖：发现算法参数化（含默认行为不回退）、viewer 配置注入全链路、子进程整链 capture→build→离线点击、per-series metadata 互异、以及 replica 构建/运行时/拓扑的既有回归。

可复现命令：

```powershell
PYTHONIOENCODING=utf-8 D:/Anaconda/envs/codegen-marker/python.exe -m unittest test.test_replica_regions test.test_capture_snapshot test.test_multi_series_capture test.test_batch_capture_replicate test.test_capture_and_build_series_contract test.test_replica_runtime test.test_replica_e2e test.test_build_replica test.test_replica_manifest test.test_replica_topology -v
```

> ✅ **Step 5 真实站验证已完成（2026-08-15，由 luna_worker 子代理协同执行，结果见 §10）**：FTImage / 中山 zscloud 两站均走通「录一序列 → 自动发现 → 逐序列 Metadata → 离线可点击复刻」，`viewers.yaml` 选择器真实命中、per-series Metadata 互异、离线点 B → Meta 是 B → 关回 B 全部验证通过。此前的「由匿名 fixture 支撑的推断」已在真实站落地。

---

## 4. 剩余步骤（按顺序）

### Step 5 — 真实站验证 ✅ 已完成

> 两站先后跑通（顺序：FTImage → 中山 zscloud），结论与数据见 §10「真实验证记录」。后续复验时沿用本节的跑法与核对点，产物规则（`--auth-mode scripted`、`--expand-all-series --max-series 10 --per-series-timeout 20`、只写 `out/`）与 §5 红线不变。

### Step 6 — 关闭验收 checklist（当前状态）

- [x] 12 套件全绿（§3 命令）
- [x] ft 真实扩展 `manifest.discovered_count` 与页面序列数一致（FT 8 序列，两分支完整捕获）
- [x] zs 真实扩展同一检查（zs 4 序列，两分支 metadata 取得；1 个因视觉变化证据不足标记 partial，见 §10）
- [x] 两站各 ≥2 个分支 `metadata_rows.json` 互不相同（逐序列 Metadata 不同，已实测）
- [x] 离线复刻点序列 B → Meta 是 B → 关回 B（两站均验证）
- [x] `pipeline_report.json` external request=0 / privacy 过（两站 manifest、普通隐私与序列身份隐私校验全部通过）
- [x] `.gitignore` 不出现 out/ 产物（根 `.gitignore` 30/40/64 行覆盖 `out/`、`out/**/series_branches/`、`out/*/runs/`，`git ls-files out/` = 0）
- [ ] （可选）把两站 `viewers.yaml` 的 zs `meta_panel` 缺省字段（`tag_row_format`/`tag_pattern`/`panel_container_selectors`）按实际面板 DOM 补上——当前走默认 `flex_div` 策略，不阻断但不精确

### Step 7 — 提交 ✅ 已完成 + review 修复轮

主体已随 commit `2e9620f` 提交（25 文件 +1954/−93，无 `out/` 产物）。随后三路子代理审阅发现 1 Critical + 若干 Important，修复见 §9（**当前工作区含该修复轮，尚未提交**）。

---

## 5. 安全红线（探测/验证时已遵守，继续遵守）

- 绝不在仓库文档/git 里放：真实 token（stm）、病人姓名、检查号、accession、原始 SeriesInstanceUID、`title` 病人文本。
- 会话输出里若出现脱敏前的真实 URL/姓名，**不回写任何文件**。
- 真实验证产物只在 `out/` 本地敏感区；`.gitignore` 已覆盖 `out/`、`out/**/series_branches/`。
- viewer URL 的 query（README/他人可见处）一律 `REDACTED`。
- fixture 的 UID 一律虚构段（`1.2.826.0.1.3680043.201.*`），绝不用真实 UID 形态。

---

## 6. 风险与已知问题（接手前必读）

1. **中山旧模板已过期（当前 Dapeng UI）**：`x 5.0MPR20368幅` 很早已失效。真实验证时已按 §4 重录（序列选择 = 点第一个 `li.ui-draggable`、Meta = 「DICOM信息 F2」开 + 对话框×关），当前 zs 模板可用。后续若 UI 再变，重复「重录/探测」。
2. **zscloud 的 cfg 匹配依赖 popup 顶层 URL 含 `zscloud.zs-hospital.sh.cn`**：`_series_viewer_config_for` 是 `page.url` 子串匹配。**本次真实验证已确认 zs 顶层 URL 含该域、cfg 命中**（`li.ui-draggable`+`#HLeftThumnail` 成功发现 4 序列）。若以后 viewer 部署在别的 originserver，cfg 会落 `{}`（默认选择器 zs 认不出）——届时 `discovered_count` 异常，先查这一步。
3. **同名序列弱身份（已知 P2#9）**：discover 对「无稳定属性 + 同名」会先合并再编号，fallback occurrence 实际失效。ft 的 8 个文本唯一，实测无影响；若以后遇同名需修 `_series_identity` fallback。
4. **zs 的 `li#id` 是 UID 形态**：内部 identity OK，**公开面必须走 `series_key_slug()`/hash**（`validate_series_privacy` 会查）；新 fixture/测试别把原始 UID 写进 public 面。
5. **滚动容器**：ft `.os-viewport` 有 2 个必须取首个可见（`_series_scope_root` 已按配置容器处理，测试覆盖）；zs 容器在 **viewer 第二层 iframe** 内——root 推导必须 `ancestor::html`（frame 内），禁止 `contentDocument` 顶层穿越。**本次真实验证两条都已按配置走通。**
6. **`.first` 修复 + contract test 已随 `2e9620f` 提交（勿回退）**：`batch_capture_replicate.py:663` 的 `target_locator.first.evaluate` 是子进程整链测试暴露的生产 bug 修复（多元素 locator strict-mode 会吞掉主快照 → offline entry 无 series region → 不可点击）。
7. **预算/partial 正常**：真实站用 `max_series 10 / per 20s / total 300s` 探路；zs 出现 1 个 `partial`（视觉变化证据不足）属正常降级，其 metadata 与离线交互仍有效，非阻断。
8. **headless 单测挂起已知坑**：`unittest discover` 会把含浏览器集成的套件一起拉挂；务必用 §3 的 `-m unittest <module>` 逐个跑。

---

## 7. 关键文件索引

- 实测结构依据（两站序列行/容器/身份属性结论）：本文档 §2.1 的历史版已并入 §2.3 + `skills/_shared/viewers.yaml` 注释；Spike SOP `docs/MULTI_SERIES_REAL_SITE_SPIKE_SOP.md` §4
- 发现算法：`capture_snapshot.py`（`discover_series_candidates` :255 / `_SERIES_ITEM_SELECTOR` / `_SERIES_IDENTITY_ATTRS` / `_series_identity` :219）
- 捕获注入：`batch_capture_replicate.py`（`_series_viewer_config_for` :377 / `_series_scope_root` :414 / `_locate_series_row` :553（C1 修复后含 `item_selector`）/ `_ensure_series_cfg` :618 / `_reparse_series_root` :1153 / `finalize_series_branches` :1453 / `capture_one_series` :853 / `_snapshot_hub_state` :1831 / `_restore_hub_state` :1877）
- 站点配置：`skills/_shared/viewers.yaml`
- 匿名 fixture + 说明：`test/fixtures/multi_series/ft_series_list.html` / `zs_series_list.html` / `README.md`
- 整链契约测试：`test/test_capture_and_build_series_contract.py`
- 冒烟标准：`docs/REAL_SITE_SMOKE_TEST.md`；多序列运行手册：`docs/MULTI_HOSPITAL_REPLICA_RUNBOOK.md`

---

## 8. 提交记录（已完成）

主体一次提交完成（**无 `out/` 产物，隐私红线已守**）：

```
2e9620f  fix: FTImage/zscloud multi-series real-site adaptation + series contract test
        25 files changed, +1954 / −93
```

覆盖：`capture_snapshot.py` / `batch_capture_replicate.py`（含 `.first` 修复）/ `skills/_shared/viewers.yaml` / `capture_readiness.py` / `pipeline_orchestrator.py` / `pipeline_validation.py` / `build_replica.py` / `replay_helpers.py` / `rewrite_script.py` / `memory/README.md` + 新 memory / 6 个改测试 + 2 fixture + README / `test_capture_and_build_series_contract.py` / `PRODUCT.md` / 两份 docs + SOP。

当前**未提交**的部分仅为 review 修复轮（见 §9），提交前同样确认 `git status --short` 无 `out/` 产物。

---

## 9. 三路子代理 review + 修复轮（2e9620f 之后，未提交）

提交后由 3 个并行子代理审阅（核心代码 / 测试 / 配置·文档·隐私）。Critical 1 个、Important 若干（隐私维度零问题）。修复内容：

| 编号 | 级别 | 问题 | 修复 |
|---|---|---|---|
| C1 | Critical | 激活路径 `_locate_series_row` 未接 `item_selector` → FTImage `a:has(span.total)` 批量激活**全分支失败**（发现成功、激活全失败；zs 因默认 `li` 兜住） | `_locate_series_row` 加 `item_selector` 参数并 4 处透传（:553/维护/`, 主定位 :917、retry :944、`_reparse_target_row` :1143、`_restore_hub_state` :1919）；补 ft 结构激活回归锁测试 |
| I2 | Important | `build_replica` 全局 `str.replace` 脱敏短值（如 `"1"`/`"202"`）会破坏整段 HTML | 加 `_REDACT_MIN_IDENTITY_LEN = 8`，短值跳过替换（真实 UID 远超阈值，仍全量脱敏） |
| I3 | Important | post-capture query scrub `errors="replace"` 会不可逆损坏 GBK 文件 | 改 `errors="strict"` + 解码失败 `continue`（宁可不 scrub 也不损坏） |
| I4 | Important | metadata 未稳定但抓到内容降级成功；`uid_hash_prefix` 可能 None → 互异无保证 | `uid_hash is None` 时降 `partial`（`metadata_unstable_no_uid`） |
| B-2 | 测试缺口 | 验收判据「≥2 分支 metadata 互异」无显式断言 | contract test 补跨分支 `metadata_rows.json` 内容 + `uid_sha256_prefix` 互异断言 |

用户明确**不调整** A-I1（metadata 候选过滤收紧行为保持现状，风险已记录为「若遇既有站回归再议」）。测试/文档/隐私三轮结论全文在会话记录，无隐私泄露。

---

## 10. 真实验证记录（2026-08-15）

> 由 luna_worker 子代理协同执行，产物只落 `out/`，未进 git。以下均为匿名化结论。

**FTImage（yyx.ftimage.cn）**
- 发现 **8 个序列**；前两个分支均**完整捕获**（captured）；逐序列 Metadata 各不相同。
- 离线副本验证：点序列 B → 打开 B 的 Metadata → 关闭后返回 B ✅。

**中山 zscloud（zscloud.zs-hospital.sh.cn）**
- 发现 **4 个序列**；前两个分支均取得 Metadata；其中 1 个因**视觉变化证据不足**标记 `partial`（数据与离线交互仍有效，属已知降级，见 §6-7）。
- 离线副本验证：点序列 B → 查看 B 的 Metadata → 关闭后返回 B ✅。

**两站共性校验**
- manifest、普通隐私及**序列身份隐私校验全部通过**；无真实链接/患者信息/UID 进入 git 状态。
- zscloud 剩余 3 条高风险 locator 警告（非阻断 `partial`）。

**已知残余（记录后处理）**
- zs `viewers.yaml` 的 `meta_panel` 缺 `tag_row_format` / `tag_pattern` / `panel_container_selectors` → 真实站 meta 提取走默认 `flex_div` 策略（见 Step 6 可选勾选项）。
- zs 的 `_series_scope_root` iframe 定根分支、viewers.yaml 配置在真实 live-session 的注入粘合仍无独立自动化测试（handoff 原标风险，真实验证已实际走过该路径）。
