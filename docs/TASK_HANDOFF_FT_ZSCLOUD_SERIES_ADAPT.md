# 交接：真实站（FTImage / 中山 zscloud）多序列发现适配 + 真实验证

> 状态：**Steps 1–4 已实施并通过全部单测 / 整链契约测试（11 套件 106 测试全绿）；Step 5 真实站验证 与 Step 6 关闭验收未做；所有改动未提交**（工作区共 12 个文件）。
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

## 2. 已完成：代码与配置（Steps 1–4，工作区未提交）

> 与上一版交接文档（只有 capture_snapshot 半成品、其余未做）相比，本版把 Steps 1–4 全部落地并验证。改动分散在 6 个文件（3 改 + 3 新增），另有文档/memory/fixture。

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
- **`LiveCaptureSession._series_cfg` + `_ensure_series_cfg(page)`**（:608/:610）：session 级缓存，首次拿到有 URL 的 page 时加载一次；空结果不缓存（后续可重试）。
- **`_reparse_series_root(recipe, page, container_selector=None)`**（:1111）：透传给 `_series_scope_root`。

注入点（全链路覆盖，`cfg` 为空时全 None → 原逻辑）：

| 位置（方法） | 注入内容 | 行号 |
|---|---|---|
| `_capture` 主快照，`marker_label=="序列选择"`（top-page + frame 两个分支） | `item_selector`/`identity_attrs` → `capture_marker_interaction_region` | :675 / :695 |
| `_capture` / `before` / `after` / `expand_series` | `_ensure_series_cfg(page)` | :634 / :723 / :728 / :748 |
| `_capture_series_region` | `item_selector`/`identity_attrs` → discover | :791 |
| `capture_one_series` 主路径 root | `container` → `_reparse_series_root` / `_series_scope_root` | :863–864 / :896 |
| `capture_one_series` retry | `container` → `_reparse_series_root` | :927 |
| `_wait_for_series_ready` 轮询 reload | `container` → `_reparse_series_root` | :1088 |
| `finalize_series_branches`（:1392） | `_ensure_series_cfg` + `container` → root & discover | :1415–1416 / :1439 |
| `_snapshot_hub_state`（:1761）恢复 re-discover | `item_selector`/`identity_attrs` | :1766 |
| `_restore_hub_state`（:1807）恢复 re-discover | `item_selector`/`identity_attrs` | :1840 |

> **同批未提交的既有修复**（非本步骤新逻辑，但同一 diff 里）：`_capture` 的 frame-owner 探测改 `target_locator.first.evaluate(...)`（:655），避免多元素 locator（如序列列表命中多行）strict-mode 抛掉整个主快照。记忆见 `memory/multi-series-subprocess-mainpath-series-region-bug.md`，契约测试见 `test/test_capture_and_build_series_contract.py`（本批新增，见 2.5）。

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

- **`test/test_replica_regions.py`**（+152 行，8 个新测试）：
  - `test_ft_fixture_discovery_uses_a_span_total_selector` — 8 个、key 唯一、reached_end；**默认选择器命中 0**（证明必须显式传 selector）
  - `test_ft_fixture_interfering_viewport_counts_nothing` — 干扰容器 0
  - `test_zs_fixture_discovery_scopes_to_studylist_container` — 4 个、key=U 测 UID 唯一、容器外 li 不计入
  - `test_series_scope_root_prefers_first_visible_configured_container` — `_series_scope_root(..., "div.os-viewport")` 命中第一个容器
  - `test_series_viewer_config_matches_known_viewer_urls` — ft/zs URL 匹配
  - `test_series_viewer_config_unknown_url_or_missing_url_returns_empty` — 未知/缺 url → `{}`
  - `test_series_viewer_config_returns_empty_on_broken_yaml` — 坏 YAML / 缺文件 → `{}`（patch `_SERIES_VIEWERS_YAML`）
- **`test/test_capture_and_build_series_contract.py`**（375 行，整链契约测试）：子进程 production 管线（`capture_and_build` + `replica_annotations.json` + `REPLICA_EXPANSION_CONFIG` → 插桩脚本 → 真实 `series_branches` → v2 manifest → `build_replica` → 离线浏览器点击）。3 个测试：真实 capture-build 分支 manifest + 离线点击、stale annotation 拒绝、offline entry 可点击。**这是本批最重要的覆盖**——只有子进程整链才能测到 `session.before/after` 主路径。

---

## 3. 验证状态（2026-08-15 实测）

> 用 `-m unittest <module>` 逐个跑（**不要 `unittest discover`**，会把含浏览器集成的套件一起拉挂）。测试里所有浏览器都 `with sync_playwright()` 局部启动，无模块级挂起。

| 套件 | 结果 |
|---|---|
| `test_replica_regions`（含 8 个新 ft/zs/config 测试） | 15/15 ok（44.7s） |
| `test_capture_and_build_series_contract`（子进程整链） | 3/3 ok（131.3s） |
| `test_capture_snapshot` | 9/9 ok |
| `test_multi_series_capture` + `test_multi_series_budget` + `test_branch_topology_fixes` | 24/24 ok |
| `test_batch_capture_replicate`（55 个，改动直接影响） | 55/55 ok（371.6s） |
| `test_replica_runtime` / `test_replica_e2e` / `test_build_replica` / `test_replica_manifest` / `test_replica_topology` | 5+3+16+8+1 = 33/33 ok |

**合计 11 套件 106 测试全绿。** 这已覆盖：发现算法参数化（含默认行为不回退）、viewer 配置注入全链路、子进程整链 capture→build→离线点击、以及 replica 构建/运行时/拓扑的既有回归。

可复现命令：

```powershell
PYTHONIOENCODING=utf-8 D:/Anaconda/envs/codegen-marker/python.exe -m unittest test.test_replica_regions test.test_capture_snapshot test.test_multi_series_capture test.test_batch_capture_replicate test.test_capture_and_build_series_contract test.test_replica_runtime test.test_replica_e2e test.test_build_replica test.test_replica_manifest test.test_replica_topology -v
```

> ⚠️ **未验证的部分**：全部是单测/契约测试级。**没有在任何真实站上跑过 capture-build 扩展**（Step 5），`viewers.yaml` 选择器的真实命中、zs 的 popup/iframe URL 匹配、per-series Metadata 互异，都还是「由匿名 fixture 支撑的推断」。

---

## 4. 剩余步骤（按顺序）

### Step 5 — 真实站验证（用户已授权两站各跑一次小预算）

前置：Step 1–4 已绿（§3）；确认两个真实站 link 仍有效（上次探测暂有效）。

**FTImage（单文档最简单，先跑）：**
1. 现成模板 `out/ftimage/processed_script_ftimage.py` 已含「序列选择」marker + Meta「更多→Tags→#tagsBox a.close」完整动作（已核）。stm token 探测时有效；若过期需重新录制（动作 = 双击序列 `get_by_role("link", name="x 10.0_lung 共 41张")` → Meta）。
2. 跑扩展 capture-build（token 直进、免登录 → interactive 不需要，`--auth-mode scripted`）：
   ```powershell
   D:/Anaconda/envs/codegen-marker/python.exe pipeline_orchestrator.py `
     --script out/ftimage/processed_script_ftimage.py `
     --annotations out/ftimage/replica_annotations.json `
     --hospital ftimage --output-root out --auth-mode scripted `
     --operation capture-build --capture-timeout 600 `
     --expand-all-series --max-series 10 --per-series-timeout 20 --total-series-timeout 300
   ```
3. 核对（对照 §1 判断标准）：
   - `runs/{id}/capture/series_branches/series_capture_manifest.json`：`discovered_count` 对应 `a:has(span.total)` 枚举（预期 8 或按预算）；`count_conserved`
   - 挑 3 个分支的 `metadata/metadata_rows.json`，SeriesDescription/SeriesNumber **互不相同**
   - `serve_replica.py` 断网开离线复刻 → 点序列 → Meta 面板随序列变化 → 关回原序列

**中山 zscloud（popup+iframe，复杂，后跑）：**
1. **⚠️ 旧模板定位器大概率失效**：`out/zscloud/processed_script_zscloud.py` 的激活选择器仍是 `get_by_text("x 5.0MPR20368幅")`（已核）——在当前 Dapeng UI 里是 `li.ui-draggable`（文本 "5.0 x 5.0 / MPR / 201 / 68幅"，keys 形态已不同）。**必须先重录**（序列选择=点第一个 `li.ui-draggable`、Meta=「DICOM信息 F2」开+对话框×关），或用探测脚本确认当前可命中的激活选择器再改模板。
2. 录制：GUI 里 URL 填共享链接（主办方提供），得出完整模板 → 保存（`processed_script_zscloud.py` + `replica_annotations.json`）→ 跑上面同款 capture-build（`--hospital zscloud`）。
3. 核对同上（Metadata 逐序列不同）。

### Step 6 — 关闭验收 checklist

- [ ] 11 套件全绿（§3 命令）
- [ ] ft 真实扩展 `manifest.discovered_count` 与页面序列数一致（或预算内）
- [ ] zs 真实扩展同一检查（需新模板）
- [ ] 两站各 ≥2 个分支 `metadata_rows.json` 的 SeriesNumber/Description 互不相同
- [ ] 离线复刻点序列 B → Meta 是 B → 关回 B（每站至少 2 个序列）
- [ ] `pipeline_report.json` external request=0 / privacy 过
- [ ] `.gitignore` 不出现 out/ 产物（根 `.gitignore` 30/40/64 行已覆盖 `out/`、`out/**/series_branches/`、`out/*/runs/`）

### Step 7 — 提交（新增；当前**全部未提交**）

见 §8 建议分组。提交前确认：`git status --short` 不出现 `out/` 任何产物；`viewers.yaml` / fixture / 文档无真实病人文本。

---

## 5. 安全红线（探测/验证时已遵守，继续遵守）

- 绝不在仓库文档/git 里放：真实 token（stm）、病人姓名、检查号、accession、原始 SeriesInstanceUID、`title` 病人文本。
- 会话输出里若出现脱敏前的真实 URL/姓名，**不回写任何文件**。
- 真实验证产物只在 `out/` 本地敏感区；`.gitignore` 已覆盖 `out/`、`out/**/series_branches/`。
- viewer URL 的 query（README/他人可见处）一律 `REDACTED`。
- fixture 的 UID 一律虚构段（`1.2.826.0.1.3680043.201.*`），绝不用真实 UID 形态。

---

## 6. 风险与已知问题（接手前必读）

1. **中山旧模板已过期（当前 Dapeng UI）**：`x 5.0MPR20368幅` 很可能命中不了 → 真实验证的最大前置，必须重录或先探当前可点选择器（§4 Step 5 / zs）。
2. **zscloud 的 cfg 匹配依赖 popup 顶层 URL 含 `zscloud.zs-hospital.sh.cn`**：`_series_viewer_config_for` 是 `page.url` 子串匹配。viewer 动作发生在 popup 的**第二层 iframe** 内，若 popup 顶层 URL 不含该域（例如 viewer 部署在别的 originserver），cfg 会落到 `{}`（默认选择器，zs 认不出）。真实站验证时如果 `discovered_count` 异常，先查这一步。
3. **同名序列弱身份（已知 P2#9）**：discover 对「无稳定属性 + 同名」会先合并再编号，fallback occurrence 实际失效。ft 的 8 个文本唯一，实测无影响；若以后遇同名需修 `_series_identity` fallback。
4. **zs 的 `li#id` 是 UID 形态**：内部 identity OK，**公开面必须走 `series_key_slug()`/hash**（`validate_series_privacy` 会查）；新 fixture/测试别把原始 UID 写进 public 面。
5. **滚动容器**：ft `.os-viewport` 有 2 个必须取首个可见（`_series_scope_root` 已按配置容器处理，测试覆盖）；zs 容器在 **viewer 第二层 iframe** 内——root 推导必须 `ancestor::html`（frame 内），禁止 `contentDocument` 顶层穿越。
6. **`.first` 修复 + contract test 未提交**（同批 diff）：勿回退，`batch_capture_replicate.py:655` 的 `target_locator.first.evaluate` 是子进程整链测试暴露的生产 bug 修复（多元素 locator strict-mode 会吞掉主快照 → offline entry 无 series region → 不可点击）。
7. **预算/partial 正常**：真实站先用 `max_series 10 / per 20s / total 300s` 探路；FTImage 终态 `partial` 属正常（画布动态帧 unsupported，见 `docs/REAL_SITE_SMOKE_TEST.md` C 类）。
8. **headless 单测挂起已知坑**：`unittest discover` 会把含浏览器集成的套件一起拉挂；务必用 §3 的 `-m unittest <module>` 逐个跑。

---

## 7. 关键文件索引

- 实测结构依据（两站序列行/容器/身份属性结论）：本文档 §2.1 的历史版已并入 §2.3 + `skills/_shared/viewers.yaml` 注释；Spike SOP `docs/MULTI_SERIES_REAL_SITE_SPIKE_SOP.md` §4
- 发现算法：`capture_snapshot.py`（`discover_series_candidates` :255 / `_SERIES_ITEM_SELECTOR` / `_SERIES_IDENTITY_ATTRS` / `_series_identity` :219）
- 捕获注入：`batch_capture_replicate.py`（`_series_viewer_config_for` :377 / `_series_scope_root` :414 / `_ensure_series_cfg` :610 / `_reparse_series_root` :1111 / `finalize_series_branches` :1392 / `capture_one_series` :845 / `_snapshot_hub_state` :1761 / `_restore_hub_state` :1807）
- 站点配置：`skills/_shared/viewers.yaml`
- 匿名 fixture + 说明：`test/fixtures/multi_series/ft_series_list.html` / `zs_series_list.html` / `README.md`
- 整链契约测试：`test/test_capture_and_build_series_contract.py`
- 冒烟标准：`docs/REAL_SITE_SMOKE_TEST.md`；多序列运行手册：`docs/MULTI_HOSPITAL_REPLICA_RUNBOOK.md`

---

## 8. 未提交清单与建议提交顺序（接手后第一步）

当前工作区（全部未 commit，也全部未 staged）：

```
 M batch_capture_replicate.py            # Step 1 注入 + .first 修复
 M capture_snapshot.py                   # Step 1 发现参数化
 M skills/_shared/viewers.yaml           # Step 2 两站条目
 M test/replica/fixtures README（fixtures/multi_series/README.md）
 M test/test_replica_regions.py          # Step 4 新增 8 测试
 M memory/README.md                      # 新 memory 指针
?? test/fixtures/multi_series/ft_series_list.html
?? test/fixtures/multi_series/zs_series_list.html
?? test/test_capture_and_build_series_contract.py
?? memory/multi-series-subprocess-mainpath-series-region-bug.md
?? docs/MULTI_SERIES_REAL_SITE_SPIKE_SOP.md
?? docs/TASK_HANDOFF_FT_ZSCLOUD_SERIES_ADAPT.md   # 本文档
?? PRODUCT.md                          # 产品定位文档（与本功能无直接关系，可单独提或不提）
```

建议提交分组（每组独立可审）：

1. **fixture + 单测 + 发现的 bug 修复**：`capture_snapshot.py`、`batch_capture_replicate.py`（含 `.first` 修复）、`test/test_replica_regions.py`、`test/test_capture_and_build_series_contract.py`、`test/fixtures/multi_series/*` + README、`memory/multi-series-subprocess-mainpath-series-region-bug.md` + `memory/README.md`。
2. **站点配置**：`skills/_shared/viewers.yaml`（第二步，等真实站验证都可能要进一步改）。
3. **文档**：`docs/MULTI_SERIES_REAL_SITE_SPIKE_SOP.md`、`docs/TASK_HANDOFF_FT_ZSCLOUD_SERIES_ADAPT.md`。

> 推进顺序建议：**先提交分组 1（代码+测试已全绿），再做 Step 5 真实站验证**；验证若需改 `viewers.yaml` 单独提交分组 2；全绿后把本文档改为「已完成」状态收尾（Step 6）。
