"""PyQt6 主窗口。

数据模型（v2 — 行列表驱动）：
- 代码面板由 `_display_items`（有序行列表）驱动，每行有 type（codegen / marker）和 text。
- codegen 推送 → 新行追加到最后一个 codegen 条目之后（不会跨越 marker 跑到面板末尾）。
- marker 插入 → 右键菜单在点击位置插入 → 写入 `_display_items` → 重建面板。
- 录制停止 → 断开推送通道，用户可继续编辑；最终脚本由 `_display_items` 合成。

线程模型：
- 主线程：Qt 事件循环。
- 子线程：CodegenManager 的 watchdog Observer + 轮询线程。
- 桥接：worker 线程通过 CodeUpdateEmitter.code_ready 投递原文到主线程。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import uuid
from collections import defaultdict, deque
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from PyQt6.QtCore import QObject, QProcess, Qt, QTimer, pyqtSignal, QPoint
from PyQt6.QtGui import (
    QAction,
    QColor,
    QFont,
    QFontMetrics,
    QPainter,
    QSyntaxHighlighter,
    QTextCharFormat,
    QTextCursor,
)
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QSplitter,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from agent import parse_markers
from codegen_manager import CodegenManager
from markers import DEFAULT_MARKERS, Marker, render
from orchestrator_events import (
    SERIES_EVENT_NAMES,
    MarkerTracker,
    SeriesTracker,
    MARKER_STATUSES,
)
from replica_annotation_panel import ReplicaAnnotationPanel
from rewrite_script import (
    LocatorEditError,
    SourceSpan,
    parse_action_plan,
    replace_action_locator,
    source_span_offsets,
)
from runtime_python import codegen_python_executable


PROJECT_ROOT = Path(__file__).resolve().parent
# Storage state is kept out of the GUI until a file picker + explicit warning are
# implemented (scripted / interactive auth modes remain available).
FTIMAGE_URL = os.environ.get(
    "FTIMAGE_RECORDING_URL",
    "https://yyx.ftimage.cn/dimage/index.html",
)
FTIMAGE_OUTPUT = PROJECT_ROOT / "out" / "ftimage" / "processed_script_ftimage.py"

# Phase 8 series-expansion defaults. Mirror ``PipelineConfig`` (pipeline_models.py)
# safe defaults so the GUI and the orchestrator agree even before a run is saved.
SERIES_EXPAND_DEFAULT = False
SERIES_MAX_DEFAULT = 40
SERIES_PER_TIMEOUT_DEFAULT = 20
SERIES_TOTAL_TIMEOUT_DEFAULT = 900
# MVP capture modes: only first_stable_frame is implemented. Do NOT expose an
# un-implemented "all frames" option as selectable.
SERIES_CAPTURE_MODES = ("first_stable_frame",)


def replica_python_executable() -> str:
    """Return the documented replica interpreter, or raise if it is missing.

    Wraps runtime_python.codegen_python_executable(). Never silently falls back
    to sys.executable: system Python (3.7) lacks the PyQt6/playwright wheels and
    is forbidden for subprocesses.
    """
    return codegen_python_executable()


def write_source_text(path: str | Path, source: str) -> None:
    """Persist generated source with LF newlines so annotation hashes stay stable."""
    destination_path = Path(path)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    with destination_path.open("w", encoding="utf-8", newline="\n") as destination:
        destination.write(source)


def normalize_ftimage_codegen(source: str) -> str:
    """Replace FTImage's ambiguous Tags controls with unique panel-scoped selectors."""
    if "yyx.ftimage.cn" not in source:
        return source
    source = source.replace(
        'page.get_by_role("link", description="Tags", exact=True).click()',
        'page.locator("#moreBox a.tool.tool-tags").click()',
    )
    return source.replace(
        'page.get_by_role("link").filter(has_text=re.compile(r"^$")).click()',
        'page.locator("#tagsBox a.close").click()',
    )


_PAGE_GOTO_URL_RE = re.compile(
    r'(?m)^(?P<prefix>\s*page\.goto\(\s*)(?:"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\')'
)


def preserve_entry_url(source: str, entry_url: str) -> str:
    """Keep the user-supplied recording URL as the replay entry point."""
    if not entry_url:
        return source
    return _PAGE_GOTO_URL_RE.sub(
        lambda match: match.group("prefix") + json.dumps(entry_url, ensure_ascii=False),
        source,
        count=1,
    )


def export_preflight_errors(source: str) -> list[str]:
    """Reject only malformed recordings; report markers may be standalone captures."""
    try:
        parse_action_plan(source)
    except SyntaxError as error:
        return [f"录制脚本语法错误：{error.msg}"]
    return []


# ---- 纯文本逻辑（不依赖 Qt，方便单元测试） ----


def detect_indent(line_text: str) -> str:
    """取行首的连续空白（空格 / Tab）作为缩进；空行则按零缩进处理。"""
    i = 0
    while i < len(line_text) and line_text[i] in (" ", "\t"):
        i += 1
    return line_text[:i]


def compute_codegen_appendix(last_count: int, new_content: str) -> Tuple[int, str]:
    """比对 new_content 和上次推送的行数，返回 (新增行数, 追加内容)。

    - new_content 行数 > last_count：返回 (delta, 新增部分文本)
    - new_content 行数 <= last_count：返回 (0, "") —— codegen 罕见地减少行数时
      不破坏面板，等下次真正有增量再追加。

    边界：用 splitlines() 而不是 split("\\n")，避免末尾空行被当成"新增一行空"。
    """
    lines = new_content.splitlines()
    new_count = len(lines)
    if new_count <= last_count:
        return 0, ""
    appendix = "\n".join(lines[last_count:])
    return new_count - last_count, appendix


def insert_marker_after_line(source: str, line_idx: int, marker_text: str) -> str:
    """在 source 的第 line_idx 行（0-based）后面插入 marker_text。
    （保留用于向后兼容和纯文本测试，GUI 不再使用此函数。）
    """
    lines = source.split("\n")
    if line_idx < 0 or line_idx >= len(lines):
        return source
    indent = detect_indent(lines[line_idx])
    marker_lines = marker_text.split("\n")
    while marker_lines and marker_lines[-1] == "":
        marker_lines.pop()
    inserted = [indent + ln for ln in marker_lines]
    return "\n".join(lines[: line_idx + 1] + inserted + lines[line_idx + 1 :])


def _fingerprint(line: str) -> str:
    """code 行指纹：rstrip 去掉尾部空白，容错 trailing 空格差异。"""
    return line.rstrip()


def relocate_markers(
    codegen_lines: List[str], anchors: List[Dict]
) -> List[Dict[str, str]]:
    """根据新 codegen 序列重定位 marker，返回新的 _display_items。

    每个 anchor: {"marker_id": str, "codegen_idx": int, "fingerprint": str, "items": List[dict]}
    - 先按 codegen_idx 定位：若该偏移行指纹匹配，定位成功。
    - 否则用指纹在整序列里找匹配行（优先 codegen_idx 附近），更新 codegen_idx。
    - 仍找不到（锚点行被彻底删除）→ 丢弃该 marker，不再甩到末尾。

    多个 anchor 锚定同一 codegen 行时，按 anchors 列表顺序成组插入（保持插入顺序）。
    """
    new_items: List[Dict[str, str]] = [
        {"type": "codegen", "text": ln} for ln in codegen_lines
    ]

    # 按 codegen_idx 分组，组内保持原顺序
    groups: Dict[int, List[Dict]] = {}
    for anchor in anchors:
        idx = anchor["codegen_idx"]
        fp = anchor["fingerprint"]
        resolved = _resolve_anchor_idx(codegen_lines, idx, fp)
        if resolved is None:
            # 锚点行已彻底消失 → 丢弃（不再追加到末尾）
            continue
        anchor["codegen_idx"] = resolved
        groups.setdefault(resolved, []).append(anchor)

    # 从大到小插入，避免索引漂移（在 i 后插入不影响 < i 的位置）
    for codegen_idx in sorted(groups.keys(), reverse=True):
        block_items: List[Dict[str, str]] = []
        for anchor in groups[codegen_idx]:
            block_items.extend(anchor["items"])
        new_items[codegen_idx + 1 : codegen_idx + 1] = block_items

    return new_items


def build_replica_annotations(display_items: List[Dict[str, str]], source_code: str) -> Dict[str, object]:
    """Serialize stable marker IDs with their current source line for replica capture."""
    markers = []
    for line_number, item in enumerate(display_items, start=1):
        if item.get("type") != "marker" or not item.get("marker_id"):
            continue
        text = item.get("text", "").strip()
        if text.startswith("# [MARKER:"):
            label = text.removeprefix("# [MARKER:").split("@", 1)[0].rstrip("] ").strip()
            markers.append({"marker_id": item["marker_id"], "line": line_number, "label": label})
    return {
        "schema_version": 1,
        "source_script_sha256": hashlib.sha256(source_code.encode("utf-8")).hexdigest(),
        "markers": markers,
    }


def _resolve_anchor_idx(
    codegen_lines: List[str], idx: int, fingerprint: str
) -> Optional[int]:
    """在 codegen 序列中定位锚点行偏移。

    1) idx 在范围内且指纹匹配 → 返回 idx
    2) 否则用指纹全局查找，返回最接近 idx 的匹配位置
    3) 无任何匹配 → 返回 None
    """
    if 0 <= idx < len(codegen_lines) and _fingerprint(codegen_lines[idx]) == fingerprint:
        return idx
    matches = [i for i, ln in enumerate(codegen_lines) if _fingerprint(ln) == fingerprint]
    if not matches:
        return None
    return min(matches, key=lambda i: abs(i - idx))


def build_annotations_from_source(
    source_code: str, anchors: List[Dict]
) -> Dict[str, object]:
    """Derive marker annotations from the ACTUAL editor source, not _display_items.

    Runs ``agent.parse_markers`` over the live editor text to get authoritative
    1-based line numbers, then reuses each known anchor's ``marker_id`` where the
    marker header line fingerprint still matches. Markers that cannot retain a
    known id (manually retyped / relabeled) receive a fresh UUID. This keeps
    annotations correct even after the user edits text above a marker after
    recording, when ``_display_items`` line numbers are stale.
    """
    lines = source_code.split("\n")

    # index known ids by the fingerprint of each known marker item line,
    # occurrence-safe: duplicate marker headers preserve distinct ids in order
    ids_by_fingerprint: Dict[str, deque[str]] = defaultdict(deque)
    for anchor in anchors:
        marker_id = anchor.get("marker_id")
        header = next(
            (
                item
                for item in anchor.get("items") or []
                if item.get("type") == "marker"
                and (item.get("text") or "").lstrip().startswith("# [MARKER:")
            ),
            None,
        )
        if marker_id and header:
            fingerprint = _fingerprint(header.get("text") or "")
            ids_by_fingerprint[fingerprint].append(str(marker_id))

    markers = []
    for parsed in parse_markers(source_code):
        line_number = parsed.get("line_start", 0)
        header = (
            lines[line_number - 1] if 1 <= line_number <= len(lines) else ""
        )
        fp = _fingerprint(header)
        known_ids = ids_by_fingerprint.get(fp)
        marker_id = known_ids.popleft() if known_ids else str(uuid.uuid4())
        label = (parsed.get("name") or "").split("@", 1)[0].strip()
        markers.append(
            {"marker_id": marker_id, "line": line_number, "label": label}
        )

    return {
        "schema_version": 1,
        "source_script_sha256": hashlib.sha256(source_code.encode("utf-8")).hexdigest(),
        "markers": markers,
    }


def rebuild_display_state_from_source(
    source_code: str,
    old_anchors: List[Dict],
) -> tuple[List[Dict[str, str]], List[Dict]]:
    """Rebuild line items and anchors after an arbitrary source edit."""
    annotations = build_annotations_from_source(source_code, old_anchors)
    marker_id_by_line = {
        int(marker["line"]): str(marker["marker_id"])
        for marker in annotations["markers"]
    }
    old_anchor_by_id = {
        str(anchor["marker_id"]): anchor
        for anchor in old_anchors
        if anchor.get("marker_id")
    }
    lines = source_code.splitlines()
    marker_ranges: dict[int, tuple[int, str]] = {}
    for start_line, marker_id in marker_id_by_line.items():
        previous = old_anchor_by_id.get(marker_id)
        item_count = max(1, len(previous.get("items", []))) if previous else 1
        end_line = min(len(lines), start_line + item_count - 1)
        marker_ranges[start_line] = (end_line, marker_id)

    display_items: List[Dict[str, str]] = []
    rebuilt_anchors: List[Dict] = []
    codegen_lines: List[str] = []
    line_number = 1
    while line_number <= len(lines):
        marker_range = marker_ranges.get(line_number)
        if marker_range is None:
            text = lines[line_number - 1]
            display_items.append({"type": "codegen", "text": text})
            codegen_lines.append(text)
            line_number += 1
            continue
        end_line, marker_id = marker_range
        marker_items = [
            {
                "type": "marker",
                "text": lines[index - 1],
                "marker_id": marker_id,
            }
            for index in range(line_number, end_line + 1)
        ]
        display_items.extend(marker_items)
        codegen_idx = max(0, len(codegen_lines) - 1)
        fingerprint = _fingerprint(codegen_lines[-1]) if codegen_lines else ""
        rebuilt_anchors.append({
            "marker_id": marker_id,
            "codegen_idx": codegen_idx,
            "fingerprint": fingerprint,
            "items": marker_items,
        })
        line_number = end_line + 1
    return display_items, rebuilt_anchors


# ---- 代码面板：行号栏 + marker 行背景 ----

MARKER_LINE_COLOR = "#FFF8DC"  # cornsilk，marker 行整行淡黄背景


class MarkerHighlighter(QSyntaxHighlighter):
    """给以 `# [MARKER:` 开头的行整行设淡黄背景。

    挂在 document 上后，setPlainText 全量替换会自动触发逐 block 重高亮，
    因此 codegen 推送后 marker 背景自动恢复，无需手动重绘。
    """

    def __init__(self, document) -> None:
        super().__init__(document)
        self._fmt = QTextCharFormat()
        self._fmt.setBackground(QColor(MARKER_LINE_COLOR))

    def highlightBlock(self, text: str) -> None:
        if text.lstrip().startswith("# [MARKER:"):
            self.setFormat(0, len(text), self._fmt)


class CodeEditor(QPlainTextEdit):
    """带左侧行号栏的代码面板。

    行号栏通过 setViewportMargins 留出左边距，paintEvent 里先画文本再画行号。
    纯只读格式操作，不改 block 数量/顺序/文本，不影响 blockNumber() 作 _display_items 索引。
    """

    def __init__(self) -> None:
        super().__init__()
        self._line_number_width = 0
        self.blockCountChanged.connect(self._update_line_number_width)
        self.updateRequest.connect(self._update_line_number_area)
        self._update_line_number_width()

    def _update_line_number_width(self, _new_block_count: int = 0) -> None:
        digits = max(1, len(str(self.blockCount())))
        fm = QFontMetrics(self.font())
        self._line_number_width = fm.horizontalAdvance("9") * digits + 12
        self.setViewportMargins(self._line_number_width, 0, 0, 0)

    def _update_line_number_area(self, rect, dy: int) -> None:
        # 滚动时重绘行号区
        if dy:
            self.scroll(0, dy)
        else:
            self.update(0, rect.y(), self._line_number_width, rect.height())
        if rect.contains(self.viewport().rect()):
            self._update_line_number_width()

    def paintEvent(self, event) -> None:
        # 先画文本内容，再在左边距画行号
        super().paintEvent(event)
        painter = QPainter(self.viewport())
        painter.setPen(QColor("#888888"))
        block = self.firstVisibleBlock()
        block_number = block.blockNumber()
        top = round(self.blockBoundingRect(block).translated(self.contentOffset()).top())
        bottom = top + round(self.blockBoundingRect(block).height())
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                painter.drawText(
                    0, top, self._line_number_width - 6, round(self.blockBoundingRect(block).height()),
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                    str(block_number + 1),
                )
            block = block.next()
            block_number += 1
            top = bottom
            bottom = top + round(self.blockBoundingRect(block).height())
            if not block.isValid():
                break


# ---- Qt 信号桥接 ----


class CodeUpdateEmitter(QObject):
    """跨线程信号发射器：从 CodegenManager 回调线程安全更新 GUI。"""
    code_ready = pyqtSignal(str)
    status_ready = pyqtSignal(str, int)  # message, timeout_ms


# ---- 主窗口 ----


class MainWindow(QMainWindow):
    DEFAULT_OUTPUT = str(FTIMAGE_OUTPUT)

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Playwright Codegen 智能标记工具")
        self.resize(1000, 720)

        self._emitter = CodeUpdateEmitter()
        self._emitter.code_ready.connect(self._on_code_ready)
        self._emitter.status_ready.connect(self._show_status)

        self._manager: CodegenManager | None = None
        self._export_process: QProcess | None = None
        self._latest_code = ""
        self._entry_url = ""
        self._saved_source_hash: str | None = None

        # ---- pipeline orchestrator state (per-stream JSONL buffering) ----
        self._pipeline_buffers: Dict[str, str] = {"stdout": "", "stderr": ""}
        self._last_pipeline_event: Optional[Dict[str, object]] = None
        self._final_pipeline_report: Optional[Path] = None
        self._final_pipeline_status: Optional[str] = None
        self._hospital: str = ""
        self._output_root: Optional[Path] = None
        self._pipeline_cancel_requested = False
        self._cancel_timer: Optional[QTimer] = None
        self._kill_timer: Optional[QTimer] = None
        # D3 marker_result upsert + summary overlay (counts semantic)
        self._marker_tracker = MarkerTracker()
        # Phase 8: series expansion progress (discovered/captured/partial/failed).
        self._series_tracker = SeriesTracker()

        # ---- 数据模型：有序行列表 ----
        # 每项: {"type": "codegen"|"marker", "text": str}
        self._display_items: List[Dict[str, str]] = []
        # marker 锚定信息：每项 {"codegen_idx": int, "fingerprint": str, "items": List[dict]}
        # codegen_idx = 锚定的 codegen 行在【纯 codegen 序列】中的 0-based 偏移（跨推送稳定）。
        # fingerprint = 该 codegen 行 rstrip() 后的文本，用于在锚点行被细微改写时容错重定位。
        # 推送时按偏移定位 + 指纹校验/回退，不再依赖整行原文比对。
        self._marker_anchors: List[Dict] = []
        # 首次推送标志
        self._panel_initialized = False
        # 置顶状态
        self._pinned = False

        self._annotation_refresh_timer = QTimer(self)
        self._annotation_refresh_timer.setSingleShot(True)
        self._annotation_refresh_timer.setInterval(300)
        self._annotation_refresh_timer.timeout.connect(
            self._refresh_annotation_panel
        )

        self._build_ui()
        self._show_status("就绪（右键代码面板插入标记）", 5000)

    # ---------- UI ----------
    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)

        layout = QVBoxLayout(central)

        # URL 行
        url_row = QHBoxLayout()
        url_row.addWidget(QLabel("URL:"))
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText(FTIMAGE_URL)
        self.url_input.setText(FTIMAGE_URL)
        url_row.addWidget(self.url_input, 1)

        # Output 行
        out_row = QHBoxLayout()
        out_row.addWidget(QLabel("输出:"))
        self.output_input = QLineEdit(self.DEFAULT_OUTPUT)
        out_row.addWidget(self.output_input, 1)
        browse_btn = QPushButton("浏览…")
        browse_btn.clicked.connect(self._on_browse)
        out_row.addWidget(browse_btn)

        # 控制按钮行
        ctrl_row = QHBoxLayout()
        self.start_btn = QPushButton("▶ 启动录制")
        self.start_btn.clicked.connect(self._on_start)
        self.stop_btn = QPushButton("■ 停止录制")
        self.stop_btn.clicked.connect(self._on_stop)
        self.stop_btn.setEnabled(False)
        self.save_btn = QPushButton("💾 保存处理后代码")
        self.save_btn.clicked.connect(self._on_save)
        self.export_replica_btn = QPushButton("⚙️ 生成 Adapter + 离线复刻")
        self.export_replica_btn.clicked.connect(self._on_export_replica)
        self.export_replica_btn.setEnabled(False)
        self.cancel_export_btn = QPushButton("取消导出")
        self.cancel_export_btn.clicked.connect(self._on_cancel_export)
        self.cancel_export_btn.setEnabled(False)
        self.replica_auth_mode = QComboBox()
        self.replica_auth_mode.addItem("脚本登录", "scripted")
        self.replica_auth_mode.addItem("手动登录", "interactive")
        self.replica_operation_combo = QComboBox()
        self.replica_operation_combo.addItem("完整（Adapter + 复刻）", "full")
        self.replica_operation_combo.addItem("只复刻（跳过 Adapter）", "capture-build")
        self.replica_operation_combo.currentIndexChanged.connect(
            self._update_replica_operation_text
        )
        self.continue_auth_btn = QPushButton("登录完成，继续")
        self.continue_auth_btn.clicked.connect(self._on_continue_auth)
        self.continue_auth_btn.setEnabled(False)
        self.pin_btn = QPushButton("📌 置顶")
        self.pin_btn.setCheckable(True)
        self.pin_btn.clicked.connect(self._toggle_pin)
        self.clear_btn = QPushButton("清空展示")
        self.clear_btn.clicked.connect(self._on_clear)
        ctrl_row.addWidget(self.start_btn)
        ctrl_row.addWidget(self.stop_btn)
        ctrl_row.addStretch(1)
        ctrl_row.addWidget(self.save_btn)
        ctrl_row.addWidget(self.export_replica_btn)
        ctrl_row.addWidget(self.cancel_export_btn)
        ctrl_row.addWidget(self.replica_auth_mode)
        ctrl_row.addWidget(self.replica_operation_combo)
        ctrl_row.addWidget(self.continue_auth_btn)
        ctrl_row.addWidget(self.pin_btn)
        ctrl_row.addWidget(self.clear_btn)

        # 代码展示面板 — 右键弹出标记菜单
        self.code_view = CodeEditor()
        font = QFont("Consolas, Menlo, monospace", 10)
        self.code_view.setFont(font)
        # marker 行整行淡黄背景（setPlainText 后自动重高亮）
        self._highlighter = MarkerHighlighter(self.code_view.document())
        self.code_view.setPlaceholderText(
            "实时生成的代码将在此显示…\n"
            "提示：\n"
            "  - 输入目标 URL，点击「启动录制」开始。\n"
            "  - Playwright 浏览器窗口会弹出，操作时此面板会自动同步新产生的代码。\n"
            "  - 右键代码面板 → 「插入标记」→ 选择标记类型即可插入。\n"
            "  - 右键代码面板 → 「删除当前行」可删除光标所在行。\n"
            "  - 录制过程中可自由增删改代码，codegen 推送只追加不覆盖已有内容。"
        )
        self.code_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.code_view.customContextMenuRequested.connect(self._on_context_menu)
        self.code_view.textChanged.connect(self._on_text_changed)

        layout.addLayout(url_row)
        layout.addLayout(out_row)
        layout.addLayout(ctrl_row)

        # ---- Phase 8: series-expansion configuration (opt-in, default OFF) ----
        series_row = QHBoxLayout()
        self.expand_all_series_chk = QCheckBox("自动探索全部序列")
        self.expand_all_series_chk.setChecked(SERIES_EXPAND_DEFAULT)
        self.expand_all_series_chk.stateChanged.connect(self._sync_series_controls)
        series_row.addWidget(self.expand_all_series_chk)
        series_row.addWidget(QLabel("最大序列数:"))
        self.max_series_spin = QSpinBox()
        self.max_series_spin.setRange(1, 100)
        self.max_series_spin.setValue(SERIES_MAX_DEFAULT)
        series_row.addWidget(self.max_series_spin)
        series_row.addWidget(QLabel("单序列超时(s):"))
        self.per_series_timeout_spin = QSpinBox()
        self.per_series_timeout_spin.setRange(1, 3600)
        self.per_series_timeout_spin.setValue(SERIES_PER_TIMEOUT_DEFAULT)
        series_row.addWidget(self.per_series_timeout_spin)
        series_row.addWidget(QLabel("总超时(s):"))
        self.total_series_timeout_spin = QSpinBox()
        self.total_series_timeout_spin.setRange(1, 3600)
        self.total_series_timeout_spin.setValue(SERIES_TOTAL_TIMEOUT_DEFAULT)
        series_row.addWidget(self.total_series_timeout_spin)
        series_row.addWidget(QLabel("capture 模式:"))
        self.viewer_capture_mode_combo = QComboBox()
        for mode in SERIES_CAPTURE_MODES:
            self.viewer_capture_mode_combo.addItem(mode, mode)
        series_row.addWidget(self.viewer_capture_mode_combo)
        self.series_status_label = QLabel("")
        self.series_status_label.setStyleSheet("color: #555;")
        series_row.addSpacing(8)
        series_row.addWidget(self.series_status_label)
        series_row.addStretch(1)
        layout.addLayout(series_row)
        self._sync_series_controls()

        self.annotation_panel = ReplicaAnnotationPanel()
        self.annotation_panel.source_jump_requested.connect(
            self._select_source_span
        )
        self.annotation_panel.locator_apply_requested.connect(
            self._apply_locator_edit
        )
        self.editor_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.editor_splitter.addWidget(self.code_view)
        self.editor_splitter.addWidget(self.annotation_panel)
        self.editor_splitter.setStretchFactor(0, 3)
        self.editor_splitter.setStretchFactor(1, 2)
        layout.addWidget(self.editor_splitter, 1)

        self.setStatusBar(QStatusBar())

    # ---------- 右键菜单 ----------
    def _on_context_menu(self, pos: QPoint) -> None:
        """在代码面板右键 → 显示插入标记菜单。"""
        # 把光标移到右键点击位置
        cursor = self.code_view.cursorForPosition(pos)
        self.code_view.setTextCursor(cursor)

        menu = QMenu(self.code_view)
        marker_submenu = menu.addMenu("➕ 插入标记")
        for marker in DEFAULT_MARKERS:
            act = QAction(marker.label, marker_submenu)
            act.triggered.connect(
                lambda _checked=False, m=marker: self._insert_marker(m)
            )
            marker_submenu.addAction(act)
        menu.addSeparator()
        delete_act = QAction("🗑 删除当前行", menu)
        delete_act.triggered.connect(self._delete_current_line)
        menu.addAction(delete_act)

        menu.exec(self.code_view.viewport().mapToGlobal(pos))

    # ---------- 槽 ----------
    def _on_browse(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "选择输出文件", self.output_input.text(), "Python (*.py)"
        )
        if path:
            self.output_input.setText(path)

    def _on_start(self) -> None:
        url = self.url_input.text().strip()
        if not url:
            QMessageBox.warning(self, "URL 为空", "请先填写目标 URL。")
            return
        output_path = self.output_input.text().strip() or self.DEFAULT_OUTPUT

        if self._manager is not None:
            try:
                self._manager.stop()
            except Exception:
                pass
            self._manager = None

        available_geometry = self.screen().availableGeometry()
        self._manager = CodegenManager(
            output_path=output_path,
            on_update=self._on_update_from_worker,
            desktop_size=(available_geometry.width(), available_geometry.height()),
        )
        self._entry_url = url

        try:
            self._manager.start(url)
        except FileNotFoundError:
            QMessageBox.critical(
                self,
                "未找到 playwright",
                "无法启动 codegen：未在 PATH 中找到 playwright。\n"
                "请先执行：pip install playwright && playwright install",
            )
            self._manager = None
            return
        except Exception as exc:
            QMessageBox.critical(self, "启动失败", str(exc))
            self._manager = None
            return

        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self._update_export_enabled()
        self._refresh_annotation_panel()
        self._show_status(f"录制中 → {output_path}（右键面板插入标记）", 0)
        # 自动置顶，避免被浏览器遮挡
        self._set_pinned(True)

    def _toggle_pin(self) -> None:
        """切换窗口置顶状态。"""
        self._set_pinned(not self._pinned)

    def _set_pinned(self, pinned: bool) -> None:
        """设置窗口置顶状态并同步按钮外观。"""
        self._pinned = pinned
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, pinned)
        if pinned:
            self.pin_btn.setText("📍 取消置顶")
            self.pin_btn.setChecked(True)
        else:
            self.pin_btn.setText("📌 置顶")
            self.pin_btn.setChecked(False)
        self.show()  # setWindowFlag 后需要 show() 才能生效

    def _on_stop(self) -> None:
        if self._manager is None:
            return
        self._manager.stop()
        self._manager = None
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self._update_export_enabled()
        self._refresh_annotation_panel()
        self._show_status("已停止 — 可自由编辑或保存", 5000)

    def _on_save(self) -> None:
        if not self._latest_code:
            QMessageBox.information(self, "无内容", "当前没有可保存的代码。")
            return
        path = self.output_input.text().strip() or self.DEFAULT_OUTPUT
        write_source_text(path, self._latest_code)
        self._write_annotations(path)
        self._saved_source_hash = hashlib.sha256(self._latest_code.encode("utf-8")).hexdigest()
        self._update_export_enabled()
        self._show_status(f"已保存 → {path}", 5000)

    def _annotations_for_export(self) -> Dict[str, object]:
        """Build replica annotations from the live editor source (post-edit).

        Marker line numbers come from ``agent.parse_markers``; ids are reused
        where fingerprints match the anchors, else freshly minted. See
        build_annotations_from_source.
        """
        return build_annotations_from_source(self._latest_code, self._marker_anchors)

    def _write_annotations(self, source_path: str | Path) -> Path:
        annotations_path = Path(source_path).with_name("replica_annotations.json")
        annotations_path.write_text(
            json.dumps(self._annotations_for_export(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return annotations_path

    def _on_export_replica(self) -> None:
        """Launch the product pipeline orchestrator as a child QProcess.

        The GUI only drives the event protocol: it streams JSONL events to the
        status bar and forwards cancel/continue commands through stdin. Storage
        state stays out of the GUI (scripted / interactive auth only).
        """
        if not self.export_replica_btn.isEnabled():
            return
        operation = self.replica_operation_combo.currentData()
        action_name = (
            "生成离线复刻"
            if operation == "capture-build"
            else "生成 Adapter + 离线复刻"
        )
        try:
            interpreter = replica_python_executable()
        except RuntimeError as error:
            QMessageBox.critical(self, f"无法{action_name}", str(error))
            self._show_status("已中止：缺少复制子进程解释器", 5000)
            return
        errors = export_preflight_errors(self._latest_code)
        if errors:
            QMessageBox.warning(self, f"无法{action_name}", "\n".join(errors))
            return
        recording_path = Path(self.output_input.text().strip() or self.DEFAULT_OUTPUT).resolve()
        # persist the source + annotations (run copies are made by the orchestrator)
        write_source_text(recording_path, self._latest_code)
        annotations_path = self._write_annotations(recording_path)
        hospital = recording_path.parent.name
        output_root = recording_path.parent.parent

        # reset per-run stream buffer / terminal state
        self._pipeline_buffers = {"stdout": "", "stderr": ""}
        self._last_pipeline_event = None
        self._final_pipeline_report = None
        self._final_pipeline_status = None
        self._hospital = hospital
        self._output_root = output_root
        self._pipeline_cancel_requested = False
        self._marker_tracker = MarkerTracker()
        self._series_tracker = SeriesTracker()
        self.series_status_label.setText("")

        process = QProcess(self)
        process.setProgram(interpreter)
        process.setArguments([
            str(PROJECT_ROOT / "pipeline_orchestrator.py"),
            "--script", str(recording_path),
            "--annotations", str(annotations_path),
            "--hospital", hospital,
            "--output-root", str(output_root),
            "--auth-mode", str(self.replica_auth_mode.currentData()),
            "--operation", str(operation),
            *self.series_config_args(),
        ])
        process.readyReadStandardOutput.connect(lambda: self._on_export_output("stdout"))
        process.readyReadStandardError.connect(lambda: self._on_export_output("stderr"))
        process.finished.connect(self._on_export_finished)
        process.errorOccurred.connect(self._on_export_error)
        self._export_process = process
        self.export_replica_btn.setEnabled(False)
        self.cancel_export_btn.setEnabled(True)
        self.replica_auth_mode.setEnabled(False)
        self.replica_operation_combo.setEnabled(False)
        process.start()
        if self._export_process is not process:
            return
        self._refresh_annotation_panel()
        if operation == "capture-build":
            self._show_status("正在生成离线复刻…", 0)
        else:
            self._show_status("正在生成 Adapter + 离线复刻…", 0)

    def _on_export_error(self, error: QProcess.ProcessError) -> None:
        """Restore export controls when the child process cannot be started."""
        if error != QProcess.ProcessError.FailedToStart:
            return
        detail = (
            self._export_process.errorString()
            if self._export_process is not None
            else "子进程无法启动"
        )
        self._export_process = None
        self.cancel_export_btn.setEnabled(False)
        self.continue_auth_btn.setEnabled(False)
        self.replica_auth_mode.setEnabled(True)
        self.replica_operation_combo.setEnabled(True)
        self._update_export_enabled()
        self._refresh_annotation_panel()
        self._show_status(f"离线复刻启动失败：{detail}", 5000)

    def _on_export_output(self, stream: str) -> None:
        if self._export_process is None:
            return
        if stream == "stdout":
            chunk = bytes(self._export_process.readAllStandardOutput())
        else:
            chunk = bytes(self._export_process.readAllStandardError())
        self._consume_pipeline_chunk(stream, chunk)

    def _consume_pipeline_chunk(self, stream: str, chunk: bytes) -> None:
        """Decode a stream chunk, buffer incomplete JSONL lines, and update GUI.

        stdout carries the JSON event protocol; stderr is redacted diagnostics
        surfaced only as status text. The GUI is updated only from complete,
        parseable stdout lines.
        """
        self._pipeline_buffers[stream] += chunk.decode("utf-8", errors="replace")
        data = self._pipeline_buffers[stream]
        lines = data.split("\n")
        self._pipeline_buffers[stream] = lines.pop()  # retain incomplete fragment
        for line in lines:
            line = line.strip()
            if not line:
                continue
            if stream == "stdout":
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    self._handle_pipeline_event(event)
            else:
                self._show_status(f"离线复刻诊断：{line}", 5000)

    def _handle_pipeline_event(self, event: dict) -> None:
        self._last_pipeline_event = event
        kind = event.get("event")

        if kind == "ready":
            # protocol handshake — pipeline is live and accepting commands
            self._show_status("已连接离线复刻管线（ready）", 5000)
            return

        if kind == "auth_required":
            self.continue_auth_btn.setEnabled(True)
            self._show_status("需要手动登录：完成后点「登录完成，继续」", 0)
            return

        if kind == "auth_completed":
            self.continue_auth_btn.setEnabled(False)
            self._show_status("登录完成，继续捕获…", 5000)
            return

        if kind == "completed" or kind == "pipeline_finished":
            run_id = str(event.get("run_id") or "")
            status = event.get("status")
            self._final_pipeline_status = status if isinstance(status, str) else None
            if self._output_root is not None and run_id:
                self._final_pipeline_report = (
                    self._output_root / self._hospital / "runs" / run_id / "pipeline_report.json"
                )
            # NOT final: _on_export_finished is the authoritative terminal message.
            self._show_status("离线复刻处理完成，正在校验最终报告…", 5000)
            return

        if kind == "fatal":
            detail = event.get("error_category") or event.get("stage") or "未知错误"
            self._show_status(f"离线复刻失败：{detail}", 5000)
            return

        if kind == "stage_started":
            self._show_status(f"阶段：{event.get('stage')}", 5000)
            return

        if kind == "stage_finished":
            return

        if kind == "marker_result":
            # D3: upsert per-marker outcome (latest status wins; clears summary overlay)
            try:
                self._marker_tracker.upsert(event)
            except ValueError:
                pass  # malformed marker_result — ignore rather than break the stream
            return

        if kind == "summary":
            # D3: authoritative counts overlay (scope=markers)
            if event.get("scope") == "markers":
                self._marker_tracker.overwrite(event)
            return

        if kind == "progress":
            # current-item / item-total overlay
            stage = event.get("stage", "")
            self._show_status(f"离线复刻 {stage}…", 5000)
            return

        if kind in SERIES_EVENT_NAMES:
            # Phase 8: series expansion progress (discovered/captured/partial/failed).
            self._on_series_event(event)
            return

        # fall back for any other event kinds
        self._show_status(f"离线复刻：{kind}", 5000)

    def _marker_counts(self) -> Dict[str, int]:
        """Current D3 marker counts: from the summary overlay or latest upserts."""
        return self._marker_tracker.counts()

    def _on_export_finished(self, exit_code: int, _exit_status) -> None:
        terminal_ok = False
        terminal_status = None
        if self._final_pipeline_report is not None:
            try:
                report = json.loads(
                    self._final_pipeline_report.read_text(encoding="utf-8")
                )
            except (OSError, ValueError):
                report = None
            if isinstance(report, dict):
                terminal_status = report.get("status")
                terminal_ok = terminal_status in ("success", "partial")

        self._export_process = None
        self.cancel_export_btn.setEnabled(False)
        self.continue_auth_btn.setEnabled(False)
        self.replica_auth_mode.setEnabled(True)
        self.replica_operation_combo.setEnabled(True)
        self._update_export_enabled()
        self._refresh_annotation_panel()

        if exit_code == 0 and terminal_ok:
            self._show_status(
                f"离线复刻完成（{terminal_status or self._final_pipeline_status or 'success'}）", 5000
            )
        elif self._final_pipeline_report is None:
            # exit 0 but no final validation report produced → not a success
            self._show_status("未产生最终验证报告", 5000)
        elif not terminal_ok:
            self._show_status(
                f"离线复刻报告状态异常（{terminal_status or 'unknown'}）", 5000
            )
        else:
            self._show_status(f"离线复刻失败（{exit_code}）", 5000)

    def _on_cancel_export(self) -> None:
        """Gracefully cancel an in-flight pipeline run.

        Sends a ``cancel`` JSONL command over stdin, then starts a 5s timer to
        ``terminate()`` and another 2s later ``kill()``. Keeps Qt responsive.
        """
        self._pipeline_cancel_requested = True
        if self._export_process is None:
            return
        try:
            self._export_process.write(b'{"command":"cancel"}\n')
        except Exception:  # noqa: BLE001 - best-effort command delivery
            pass
        self._show_status("正在取消离线复刻…", 0)
        if self._cancel_timer is None:
            self._cancel_timer = QTimer(self)
            self._cancel_timer.setSingleShot(True)
            self._cancel_timer.timeout.connect(self._terminate_export)
            self._kill_timer = QTimer(self)
            self._kill_timer.setSingleShot(True)
            self._kill_timer.timeout.connect(self._kill_export)
        self._cancel_timer.start(5000)
        self._kill_timer.start(7000)

    def _terminate_export(self) -> None:
        process = self._export_process
        if process is None:
            return
        try:
            if process.state() == QProcess.ProcessState.Running:
                process.terminate()
        except Exception:  # noqa: BLE001 - best-effort terminate
            pass

    def _kill_export(self) -> None:
        process = self._export_process
        if process is None:
            return
        try:
            process.kill()
        except Exception:  # noqa: BLE001 - best-effort kill
            pass

    def _on_continue_auth(self) -> None:
        """Release an interactive live capture only after the user finishes login."""
        if self._export_process is None or not self.continue_auth_btn.isEnabled():
            return
        self._export_process.write(b'{"command":"continue_after_auth"}\n')
        self.continue_auth_btn.setEnabled(False)
        self._show_status("已发送登录完成指令，继续捕获…", 5000)

    def _update_export_enabled(self) -> None:
        has_marker = any(item["type"] == "marker" for item in self._display_items)
        is_saved = self._saved_source_hash == hashlib.sha256(self._latest_code.encode("utf-8")).hexdigest()
        self.export_replica_btn.setEnabled(bool(self._latest_code and has_marker and is_saved and self._manager is None and self._export_process is None))

    def _update_replica_operation_text(self) -> None:
        if self.replica_operation_combo.currentData() == "capture-build":
            self.export_replica_btn.setText("⚙️ 生成离线复刻")
        else:
            self.export_replica_btn.setText("⚙️ 生成 Adapter + 离线复刻")

    # ---------- Phase 8: series-expansion config + progress display ----------
    def _sync_series_controls(self) -> None:
        """Enable budget controls only when all-series discovery is on.

        The MVP capture mode combo stays enabled but only offers the implemented
        ``first_stable_frame`` mode (no un-implemented all-frames option).
        """
        enabled = self.expand_all_series_chk.isChecked()
        for control in (
            self.max_series_spin,
            self.per_series_timeout_spin,
            self.total_series_timeout_spin,
        ):
            control.setEnabled(enabled)
        if not enabled:
            self.series_status_label.setText("")
            self._series_tracker = SeriesTracker()

    def series_config_args(self) -> list[str]:
        """Return the CLI args for series expansion (empty when disabled).

        Deliberately mirrors the orchestrator's ``--expand-all-series`` / budget
        flags so the GUI and the child pipeline agree on what was requested.
        """
        if not self.expand_all_series_chk.isChecked():
            return []
        return [
            "--expand-all-series",
            "--max-series", str(self.max_series_spin.value()),
            "--per-series-timeout", str(self.per_series_timeout_spin.value()),
            "--total-series-timeout", str(self.total_series_timeout_spin.value()),
            "--viewer-capture-mode", str(self.viewer_capture_mode_combo.currentData()),
        ]

    def _on_series_event(self, event: dict) -> None:
        """Track a ``series_*`` event and show discovered/captured/partial/failed.

        Forwarded child events carry the real fields under ``payload`` (child's
        raw JSON); orchestrator-origin events carry them at top level. ``note``
        tolerates both since it reads ``event['event']`` for the kind.
        """
        child = event.get("payload") if isinstance(event.get("payload"), dict) else event
        self._series_tracker.note(child)
        counts = self._series_tracker.counts()
        coverage = self._series_tracker.coverage()
        label = (
            f"序列: 发现 {counts['discovered']} | "
            f"成功 {counts['captured']} | 部分 {counts['partial']} | "
            f"失败 {counts['failed']} | {coverage['status']}"
        )
        self.series_status_label.setText(label)
        self._show_status(f"离线复刻序列探索：{label}", 5000)

    def _on_clear(self) -> None:
        self._display_items.clear()
        self._marker_anchors.clear()
        self._panel_initialized = False
        self._rebuild_display()
        self._update_export_enabled()
        self._refresh_annotation_panel()
        self._show_status("已清空", 2000)

    def _on_text_changed(self) -> None:
        """Synchronize live source and debounce annotation parsing."""
        self._latest_code = self.code_view.toPlainText()
        self._update_export_enabled()
        self._annotation_refresh_timer.start()

    def _set_editor_source(self, source: str) -> None:
        """Atomically synchronize the editor and both line/marker data models."""
        display_items, anchors = rebuild_display_state_from_source(
            source,
            self._marker_anchors,
        )
        self._display_items = display_items
        self._marker_anchors = anchors
        self.code_view.setPlainText(source)
        self._latest_code = source
        self._update_export_enabled()

    def _refresh_annotation_panel(self) -> None:
        editable = self._manager is None and self._export_process is None
        self.annotation_panel.set_editable(editable)
        if not self._latest_code:
            self.annotation_panel.set_plan("", parse_action_plan(""))
            return
        try:
            annotations = self._annotations_for_export()["markers"]
            plan = parse_action_plan(self._latest_code, annotations)
        except (SyntaxError, ValueError) as error:
            self.annotation_panel.set_parse_error(str(error))
            return
        self.annotation_panel.set_plan(self._latest_code, plan)

    def _select_source_span(self, span: SourceSpan) -> None:
        start, end = source_span_offsets(self._latest_code, span)
        cursor = self.code_view.textCursor()
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        self.code_view.setTextCursor(cursor)
        self.code_view.ensureCursorVisible()

    def _apply_locator_edit(self, action_id: str, expression: str) -> None:
        original = self._latest_code
        try:
            annotations = self._annotations_for_export()["markers"]
            updated = replace_action_locator(
                original,
                action_id,
                expression,
                annotations,
            )
        except (LocatorEditError, SyntaxError, ValueError) as error:
            self.annotation_panel.error_label.setText(str(error))
            self._show_status("Locator 修改未应用", 5000)
            return
        self._set_editor_source(updated)
        self._refresh_annotation_panel()
        self._show_status("Locator 已更新，请保存后再导出", 5000)

    # ---------- worker 线程 → GUI 线程 ----------
    def _on_update_from_worker(self, code: str) -> None:
        self._emitter.code_ready.emit(code)

    def _on_code_ready(self, code: str) -> None:
        """codegen 文件更新 → 全量替换 codegen 条目，保留 marker。

        playwright codegen 在文件末尾有固定的收尾段落
        （# ---- / context.close / browser.close / with sync_playwright），
        新录制动作被插入到收尾段落**之前**而非末尾。
        因此不能使用 compute_codegen_appendix 取末尾 delta，
        必须每次用全量 codegen 内容替换所有 codegen 条目，
        然后将 marker 按【codegen 序列偏移 + 指纹】重新定位（见 relocate_markers）。
        """
        normalized = normalize_ftimage_codegen(code)
        new_codegen_lines = preserve_entry_url(normalized, self._entry_url).splitlines()

        if not self._panel_initialized:
            # 首次：用全部 codegen 内容初始化
            self._display_items = [
                {"type": "codegen", "text": ln} for ln in new_codegen_lines
            ]
            self._panel_initialized = True
            self._rebuild_display()
            return

        # 后续推送：全量替换 codegen 条目，按偏移+指纹重定位 marker
        self._display_items = relocate_markers(new_codegen_lines, self._marker_anchors)
        self._rebuild_display()

    def _rebuild_display(self) -> None:
        """从 _display_items 重建面板显示。"""
        text = "\n".join(item["text"] for item in self._display_items)
        self.code_view.setPlainText(text)
        # _on_text_changed 会同步 _latest_code

    # ---------- 标记插入 ----------
    def _codegen_idx_for_panel_line(self, line_idx: int) -> Optional[int]:
        """把面板行号映射到【纯 codegen 序列】中的偏移。

        - 锚点行是 codegen 项 → 返回它在 codegen 序列中的偏移
        - 锚点行是 marker 项 → 返回它前一个 codegen 项的偏移（marker 跟随其前的 codegen 行）
        - 越界或无前置 codegen 项 → 返回 None
        """
        if not (0 <= line_idx < len(self._display_items)):
            return None
        codegen_count = 0
        last_codegen_idx = None
        for i, item in enumerate(self._display_items):
            if i == line_idx:
                if item["type"] == "codegen":
                    return codegen_count
                return last_codegen_idx  # marker 行 → 锚定前一个 codegen 项
            if item["type"] == "codegen":
                last_codegen_idx = codegen_count
                codegen_count += 1
        return last_codegen_idx

    def _insert_marker(self, marker: Marker) -> None:
        """在光标所在行后面插入 marker（通过右键菜单触发）。

        使用数据模型 + QTextCursor 精确插入，不做文本手术。
        锚点记录为 codegen 序列偏移 + 指纹，跨推送稳定（见 relocate_markers）。
        """
        rendered = render(marker)
        cursor = self.code_view.textCursor()
        line_idx = cursor.blockNumber()

        # 获取锚点行缩进
        if 0 <= line_idx < len(self._display_items):
            indent = detect_indent(self._display_items[line_idx]["text"])
        else:
            indent = ""

        # 准备 marker 行
        marker_lines = rendered.split("\n")
        while marker_lines and marker_lines[-1] == "":
            marker_lines.pop()
        marker_id = str(uuid.uuid4())
        marker_items = [
            {"type": "marker", "text": indent + ln, "marker_id": marker_id}
            for ln in marker_lines
        ]

        # 插入到 _display_items（锚点行后）
        insert_pos = line_idx + 1
        if insert_pos > len(self._display_items):
            insert_pos = len(self._display_items)
        self._display_items[insert_pos:insert_pos] = marker_items

        # 保存锚定信息：codegen 序列偏移 + 指纹（用于推送后稳定重定位）
        codegen_idx = self._codegen_idx_for_panel_line(line_idx)
        if codegen_idx is not None:
            codegen_text = self._codegen_line_text(codegen_idx)
            fingerprint = _fingerprint(codegen_text) if codegen_text is not None else ""
        else:
            fingerprint = ""
        self._marker_anchors.append({
            "marker_id": marker_id,
            "codegen_idx": codegen_idx if codegen_idx is not None else 0,
            "fingerprint": fingerprint,
            "items": marker_items,
        })

        # 使用 QTextCursor 精确插入（避免 setPlainText 全量替换）
        cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock)
        insert_text = "\n" + "\n".join(item["text"] for item in marker_items)
        cursor.insertText(insert_text)

        self._show_status(f"已插入：{marker.label}", 3000)
        self._update_export_enabled()

    def _codegen_line_text(self, codegen_idx: int) -> Optional[str]:
        """取纯 codegen 序列中第 codegen_idx 行的文本。"""
        count = 0
        for item in self._display_items:
            if item["type"] == "codegen":
                if count == codegen_idx:
                    return item["text"]
                count += 1
        return None

    def _delete_current_line(self) -> None:
        """删除光标所在行（从 _display_items 和数据面板同步删除）。

        若删除的是 marker 行，同步从 _marker_anchors 移除对应条目，
        避免下次 codegen 推送时被删的 marker 复活。
        """
        cursor = self.code_view.textCursor()
        line_idx = cursor.blockNumber()
        if not (0 <= line_idx < len(self._display_items)):
            return
        deleted_item = self._display_items[line_idx]

        # 删 marker 任意一行时，完整删除同一 anchor 的多行 marker。
        if deleted_item["type"] == "marker":
            marker_id = deleted_item.get("marker_id")
            self._display_items = [
                item for item in self._display_items
                if item.get("marker_id") != marker_id
            ]
            self._marker_anchors = [
                a for a in self._marker_anchors
                if a.get("marker_id") != marker_id
            ]
            self._rebuild_display()
            self._show_status("已删除标记", 2000)
            self._update_export_enabled()
            return

        del self._display_items[line_idx]

        # 选择整行然后删除
        cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock)
        cursor.movePosition(
            QTextCursor.MoveOperation.EndOfBlock,
            QTextCursor.MoveMode.KeepAnchor,
        )
        cursor.removeSelectedText()
        # 删除剩余的换行符
        cursor.deleteChar()
        self._show_status("已删除当前行", 2000)

    def _show_status(self, message: str, timeout_ms: int) -> None:
        self.statusBar().showMessage(message, timeout_ms)

    # ---------- 关闭 ----------
    def closeEvent(self, event) -> None:  # type: ignore[override]
        self._pipeline_cancel_requested = True
        if self._export_process is not None:
            self._on_cancel_export()
        if self._manager is not None:
            try:
                self._manager.stop()
            except Exception:
                pass
        super().closeEvent(event)


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Codegen Marker Tool")
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
