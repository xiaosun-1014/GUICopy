"""Developer-facing locator annotation widget."""

from __future__ import annotations

from dataclasses import replace

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from locator_risk import HIGH_RISK_LOCATORS, classify_locator_risk
from replica_models import ActionTarget
from rewrite_script import (
    ActionPlan,
    LocatorEditError,
    SourceSpan,
    parse_locator_expression,
    source_span_offsets,
)


ACTION_ID_ROLE = int(Qt.ItemDataRole.UserRole)
RISK_COLORS = {
    "stable_id": "#15803d",
    "aria": "#15803d",
    "stable_attribute": "#15803d",
    "text": "#2563eb",
    "ordinal": "#d97706",
    "structural": "#c2410c",
    "coordinate": "#b91c1c",
    "non_locator": "#6b7280",
}


class ReplicaAnnotationPanel(QWidget):
    source_jump_requested = pyqtSignal(object)
    locator_apply_requested = pyqtSignal(str, str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.source = ""
        self.plan: ActionPlan | None = None
        self.actions: dict[str, ActionTarget] = {}
        self.current_action_id: str | None = None
        self.editable = False

        root = QVBoxLayout(self)
        self.status_label = QLabel("没有可解析的复刻动作")
        self.high_risk_only = QCheckBox("只看高风险")
        self.high_risk_only.toggled.connect(self._populate_tree)
        root.addWidget(self.status_label)
        root.addWidget(self.high_risk_only)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Marker / Action", "行", "风险"])
        self.tree.currentItemChanged.connect(self._on_selection_changed)
        root.addWidget(self.tree, 1)

        self.action_label = QLabel("动作：—")
        self.frame_label = QLabel("iframe：—")
        self.expression_editor = QPlainTextEdit()
        self.expression_editor.setPlaceholderText(
            "选择一个 locator 动作后编辑完整 receiver"
        )
        self.expression_editor.textChanged.connect(self._preview)
        self.risk_label = QLabel("风险：—")
        self.error_label = QLabel("")
        self.error_label.setWordWrap(True)
        root.addWidget(self.action_label)
        root.addWidget(self.frame_label)
        root.addWidget(self.expression_editor)
        root.addWidget(self.risk_label)
        root.addWidget(self.error_label)

        buttons = QHBoxLayout()
        self.reset_button = QPushButton("恢复")
        self.apply_button = QPushButton("应用")
        self.reset_button.clicked.connect(self._reset_expression)
        self.apply_button.clicked.connect(self._emit_apply)
        buttons.addWidget(self.reset_button)
        buttons.addWidget(self.apply_button)
        root.addLayout(buttons)
        self.apply_shortcut = QShortcut(QKeySequence("Ctrl+Return"), self)
        self.apply_shortcut.activated.connect(self._emit_apply)
        self._clear_editor()

    def set_editable(self, editable: bool) -> None:
        self.editable = editable
        self._preview()

    def set_parse_error(self, message: str) -> None:
        self.status_label.setText(f"当前源码无法解析：{message}")
        self.tree.setEnabled(False)
        self.expression_editor.setReadOnly(True)
        self.apply_button.setEnabled(False)

    def set_plan(self, source: str, plan: ActionPlan) -> None:
        self.source = source
        self.plan = plan
        self.actions = {
            action.action_id: action
            for group in plan.marker_groups
            for action in group.actions
        }
        self.tree.setEnabled(True)
        self.status_label.setText("录制期间只读" if not self.editable else "可编辑")
        self._populate_tree()

    def _populate_tree(self) -> None:
        selected = self.current_action_id
        self.tree.clear()
        if self.plan is None:
            return
        for group in self.plan.marker_groups:
            visible_actions = [
                action for action in group.actions
                if (
                    not self.high_risk_only.isChecked()
                    or classify_locator_risk(action) in HIGH_RISK_LOCATORS
                )
            ]
            if not visible_actions:
                continue
            group_item = QTreeWidgetItem([group.marker_label, "", ""])
            self.tree.addTopLevelItem(group_item)
            for action in visible_actions:
                risk = classify_locator_risk(action)
                line = str(action.action_args.get("_source_line", ""))
                item = QTreeWidgetItem(
                    [f"{action.action_id}  {action.action_type}", line, risk]
                )
                item.setData(0, ACTION_ID_ROLE, action.action_id)
                item.setForeground(
                    2,
                    QBrush(QColor(RISK_COLORS[risk])),
                )
                group_item.addChild(item)
                if action.action_id == selected:
                    self.tree.setCurrentItem(item)
            group_item.setExpanded(True)

    def _on_selection_changed(self, current, _previous) -> None:
        action_id = current.data(0, ACTION_ID_ROLE) if current else None
        if not action_id:
            self.current_action_id = None
            self._clear_editor()
            return
        self.current_action_id = str(action_id)
        action = self.actions[self.current_action_id]
        self.action_label.setText(
            f"动作：{action.action_type} · 页面："
            f"{action.locator.page_var if action.locator else 'page'}"
        )
        if action.locator is None:
            self.frame_label.setText("iframe：—")
            self.expression_editor.setPlainText("")
            self.expression_editor.setReadOnly(True)
            self.risk_label.setText("风险：coordinate")
            self.error_label.setText(
                "coordinate 动作首版只读；请在左侧脚本中改写整条动作。"
            )
            self.apply_button.setEnabled(False)
            return
        frames = [hop.selector for hop in action.locator.frame_chain]
        self.frame_label.setText(
            "iframe：" + (" → ".join(frames) if frames else "无")
        )
        span = self.plan.locator_source_spans[action.action_id]
        start, end = source_span_offsets(self.source, span)
        self.expression_editor.blockSignals(True)
        self.expression_editor.setPlainText(self.source[start:end])
        self.expression_editor.blockSignals(False)
        self.expression_editor.setReadOnly(not self.editable)
        self.source_jump_requested.emit(span)
        self._preview()

    def _clear_editor(self) -> None:
        self.action_label.setText("动作：—")
        self.frame_label.setText("iframe：—")
        self.expression_editor.setPlainText("")
        self.expression_editor.setReadOnly(True)
        self.risk_label.setText("风险：—")
        self.error_label.setText("")
        self.apply_button.setEnabled(False)
        self.reset_button.setEnabled(False)

    def _preview(self) -> None:
        action = self.actions.get(self.current_action_id or "")
        if action is None or action.locator is None:
            return
        self.reset_button.setEnabled(self.editable)
        if not self.editable:
            self.expression_editor.setReadOnly(True)
            self.apply_button.setEnabled(False)
            return
        expression = self.expression_editor.toPlainText().strip()
        try:
            recipe = parse_locator_expression(expression)
            if recipe.page_var != action.locator.page_var:
                raise LocatorEditError("page variable cannot change")
        except LocatorEditError as error:
            self.error_label.setText(str(error))
            self.apply_button.setEnabled(False)
            return
        preview_target = replace(action, locator=recipe)
        self.risk_label.setText(
            f"风险：{classify_locator_risk(action)} → "
            f"{classify_locator_risk(preview_target)}"
        )
        frames = [hop.selector for hop in recipe.frame_chain]
        self.frame_label.setText(
            "iframe：" + (" → ".join(frames) if frames else "无")
        )
        self.error_label.setText("")
        self.apply_button.setEnabled(True)

    def _reset_expression(self) -> None:
        if self.current_action_id and self.plan:
            action = self.actions[self.current_action_id]
            if action.locator is not None:
                span = self.plan.locator_source_spans[action.action_id]
                start, end = source_span_offsets(self.source, span)
                self.expression_editor.setPlainText(self.source[start:end])

    def _emit_apply(self) -> None:
        if self.current_action_id and self.apply_button.isEnabled():
            self.locator_apply_requested.emit(
                self.current_action_id,
                self.expression_editor.toPlainText().strip(),
            )
