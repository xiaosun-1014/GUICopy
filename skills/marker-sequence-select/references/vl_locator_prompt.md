# 分支 A VL 坐标定位 Prompt

当「序列选择」marker 后没有 get_by_text(...) 调用时，走分支 A：截图 → VL 识别最优序列 → 返回像素坐标 → 点击。

## 完整 Prompt

```
这是 DICOM 影像查看器的序列列表区域。找到所有序列条目后，按以下优先级选择最优序列：

🥇 最高：Thin / HRCT / 0.625 / 1.0 — 薄层原始数据，信息量最大，可重建任意面
🥇 最高：MPR / Coronal / Sagittal — 多平面重建，冠状/矢状面，诊断必需
🥈 高：VR / Volume / 3D — 容积重建，三维立体，骨科/血管首选
🥈 高：MIP / MaxIP — 最大密度投影，血管显示，肺结节检测
🥉 中：Lung / Bone / Soft / Brain / Mediastinum — 特定窗宽优化
🥉 中：MinIP — 最小密度投影，气道/含气结构
❌ 跳过：Scout / Localizer / Surview / Topogram — 定位像，无诊断价值
❌ 跳过：Dose / Radiation — 剂量报告，不是影像
❌ 跳过：厚层（帧数 < 50 且无 MPR/VR 标记）

要求：
1. 返回该序列条目中心的像素坐标（基于截图分辨率）
2. 如果有多个序列条目并列展示，选诊断价值最高的那一个
3. 如果截图中有多个区域（侧边栏 / 主视图 / 缩略图），只关注「序列列表区域」
4. 只输出 JSON，不要任何解释文字

输出格式：
{
  "name": "<选中的完整序列名>",
  "x": <像素坐标，数字>,
  "y": <像素坐标，数字>,
  "reason": "<简短的选择理由>"
}
```

## 调用流程（viewer-agnostic）

```python
# 1. 截图（page 级，full_page 拿全）
page1.screenshot(path='series_select.jpeg', full_page=True)

# 2. VL 识别
vl_result = call_vl_model(
    image_path='series_select.jpeg',
    prompt=VL_LOCATOR_PROMPT,
)

# 3. 解析坐标
data = parse_vl_json(vl_result)
name = data['name']
x = data['x']
y = data['y']

# 4. 点击 — 用 viewer.sequence_select.canvas_selectors[0]
canvas = viewer_cfg['sequence_select']['canvas_selectors'][0]
iframe_selector = viewer_cfg['iframe_selectors'][0]  # 或从录制脚本反推

frame = page1.locator(iframe_selector).content_frame
frame.locator(canvas).click(position={"x": x, "y": y})
```

## 坐标系说明

- VL 返回的坐标基于**截图分辨率**（通常 1920×1080 或视口大小）
- Playwright `position={"x": x, "y": y}` 也是相对**画布元素的 viewport** 坐标系
- 如果画布是缩放/平移过的，需要把截图坐标换算到画布坐标
- 简单场景（画布铺满 iframe）下两者一致，可直接用

## 与分支 B 的对比

| 维度 | 分支 A（VL 坐标） | 分支 B（DOM + LLM 选择） |
|---|---|---|
| 适用场景 | 用户没录任何序列点击 | 用户已点击过某个序列 |
| 依赖 | 视觉模型 | DOM + LLM |
| 准确率 | 中（坐标可能偏移） | 高（直接用 DOM 文本） |
| 速度 | 慢（视觉模型推理） | 快 |
| 成本 | 高（每张截图都要调 VL） | 低（只调一次 LLM） |

优先用分支 B；只有分支 B 提取不到序列列表时才回退到分支 A。

## 已知 viewer 的画布选择器（参考值，非自动加载）

| viewer | canvas_selector | 参考来源 |
|---|---|---|
| uicloud | `#overlaycanvas-0_0` | viewers.yaml（人工查阅） |
| generic | 从录制脚本反推（找 `#overlaycanvas-...` / `[class*=canvas]` / `[id*=viewer]`） | infer_canvas_from_script() |
