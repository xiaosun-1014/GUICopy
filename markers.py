"""标记菜单注册表。

设计要点：
- 不再做正则自动匹配；用户从下拉菜单里主动选择要插入的标记。
- 每个标记是固定的「注释 + TODO」文本，按原样插入当前行后面。
- 缩进由插入逻辑根据锚点行推断，无需在 marker 文本里写死。
- 新增标记只需往 DEFAULT_MARKERS 列表追加一个 Marker 实例。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List


@dataclass
class Marker:
    """单个可插入的标记模板。"""
    name: str   # 内部唯一标识
    label: str  # 下拉菜单里显示的文案
    code: str   # 多行模板，每行不应带前导缩进，由插入逻辑补上


# ---- 默认标记集 ----
# 注意：每行内容都以「lstrip 友好」的形式写（不带前导空白），插入时会按锚点缩进对齐。
# 末尾留一个空字符串用于插入后换行，让连续插入连续成段。
MARKER_REPORT_SCREENSHOT = (
    "# [MARKER: 报告截图 @ {ts}]\n"
    "# page.screenshot(path=str(SCRIPT_DIR / \"report.jpeg\"), "
    "type=\"jpeg\", quality=95, full_page=True)\n"
    ""
)

MARKER_PDF_REPORT = (
    "# [MARKER: PDF 报告动作]\n"
    "# TODO: 触发 PDF 下载 / 解析 / 上传\n"
    ""
)

MARKER_SEQUENCE_LAYOUT = (
    "# [MARKER: 序列布局切换]\n"
    "# TODO: 切换 1x1 / 2x2 / MPR 等布局并等待画布稳定\n"
    ""
)

MARKER_IMAGE_CANVAS = (
    "# [MARKER: 影像画布交互]\n"
    "# TODO: 调用 VL 模型对当前帧做判定 / 切帧\n"
    ""
)

MARKER_WINDOW_LEVEL = (
    "# [MARKER: 窗宽窗位 WL/WW]\n"
    "# TODO: 批量遍历预设窗 (肺窗/骨窗/软组织窗)\n"
    ""
)

MARKER_SEQUENCE_SELECT = (
    "# [MARKER: 序列选择]\n"
    "# TODO: 对当前序列帧做判定 / 切帧\n"
    ""
)

MARKER_META_INFO = (
    "# [MARKER: Meta 信息工具 @ {ts}]\n"
    "# TODO: 提取当前检查的 Meta 信息 (Patient / Study / Series)\n"
    ""
)


def _now() -> str:
    import time
    return time.strftime("%Y%m%d_%H%M%S")


DEFAULT_MARKERS: List[Marker] = [
    Marker(name="report_screenshot", label="📸 报告截图",      code=MARKER_REPORT_SCREENSHOT),
    Marker(name="sequence_layout",   label="🔲 序列布局",      code=MARKER_SEQUENCE_LAYOUT),
    Marker(name="image_canvas",      label="🖼️ 影像画布",     code=MARKER_IMAGE_CANVAS),
    Marker(name="window_level",      label="🎚️ 窗宽窗位",     code=MARKER_WINDOW_LEVEL),
    Marker(name="sequence_select",   label="🔲 序列选择",        code=MARKER_SEQUENCE_SELECT),
    Marker(name="meta_info",         label="📋 Meta 信息工具", code=MARKER_META_INFO),
]


def render(marker: Marker) -> str:
    """对带占位符（如 {ts}）的 marker 做一次替换，返回可插入的最终文本。"""
    return marker.code.format(ts=_now())


# ---- 单元自检 ----
if __name__ == "__main__":
    for m in DEFAULT_MARKERS:
        print("----", m.label, "----")
        print(render(m))
