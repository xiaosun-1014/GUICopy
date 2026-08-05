"""事件协议纯逻辑模块 — orchestrator 侧生成/规范化、GUI 侧消费的核心规则。

本模块只含纯函数与纯类：无 I/O、无 Qt、无浏览器、无子进程、确定性可测。
供后续 orchestrator（pipeline.py）与实际 GUI 复用。

依据已批准规格（docs/superpowers/specs/2026-08-05-gui-orchestrator-event-protocol.md）：
- §4  payload 规范化转发 + 保留终态名不转发
- §5.4 marker_result 按 marker_id upsert（D3）
- §5.5 summary 权威计数覆盖（D3）
- §5.10.1 D4 终态规则（fatal 非终态、completed 唯一业务终态）
- §8  脱敏（seeded registry）
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Dict, Optional, Sequence

# 协议版本（恒为 1）
PROTOCOL_VERSION = 1

# 终态/错误保留名：child 事件名不得出现在顶层 `event`（§4 保留终态名不转发）。
# completed/failed/fatal 是被 live-capture 子进程（batch_capture_replicate.py）
# 自身会在 stdout 发出的名字，转发时必须改名，否则 GUI 会把子进程阶段完成误判为
# run 终态。
RESERVED_TERMINAL_NAMES = frozenset({"completed", "failed", "fatal"})

# orchestrator 级事件名全集：child 事件名不得与其中任何名字撞名（§4）。
ORCHESTRATOR_EVENT_NAMES = frozenset(
    {
        "stage_started",
        "stage_finished",
        "progress",
        "marker_result",
        "summary",
        "fatal",
        "completed",
        "ready",
        "log",
    }
)

# child 保留名的具体改名映射（其余保留名统一加 `capture_` 前缀）。
CHILD_TERMINAL_RENAMES = {
    "completed": "capture_completed",
    "failed": "capture_failed",
    "fatal": "capture_fatal",
}

# payload 转发时顶层 source 的前缀（§4，如 "subprocess:batch_capture_replicate"）。
SUBPROCESS_SOURCE_PREFIX = "subprocess:"

# 脱敏占位符（§8）。
REDACT_MARK = "[REDACTED]"


def _now_iso() -> str:
    """当前 UTC 时间的 ISO8601 字符串（毫秒级，与规格 §5 ts 一致）。"""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# ═══════════════════════════════════════════════════
# 1. 单行 JSONL 解析
# ═══════════════════════════════════════════════════


def parse_envelope(line: str) -> Optional[Dict]:
    """解析单行 JSON 事件。

    - 非法 JSON / 非 dict → 返回 None
    - 未知 `event` 种类 → 原样透传（前向兼容，§2）
    """
    if not isinstance(line, str):
        return None
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    return obj


# ═══════════════════════════════════════════════════
# 2. child 事件 payload 规范化转发（§4）
# ═══════════════════════════════════════════════════


def _renamed_child_name(name: str) -> str:
    """把会与 orchestrator 保留名撞名的 child 事件名改名。

    决策：采用「改名」而非「丢弃」（规格 §4 首选方案 `capture_completed` /
    `capture_failed`）。这样既保证顶层 `event` 不出现终态名（GUI 不会误判 run
    终态），又保留了子进程事件的详细信息可供 GUI「详细进度」面板展示。
    """
    if name in CHILD_TERMINAL_RENAMES:
        return CHILD_TERMINAL_RENAMES[name]
    if name in ORCHESTRATOR_EVENT_NAMES:
        return f"capture_{name}"
    return name


def normalize_child_event(child: dict, stage: str, run_id: str) -> Dict:
    """把 child 子进程产出的原始 JSON 对象规范化为统一 envelope（§4）。

    顶层 `event` 复制 child 事件名；若撞 orchestrator 保留名则改名
    （`completed`→`capture_completed` 等），保证终态名不出现在顶层 `event`。
    `payload` 原样承载 child 原始 JSON（child 是各自唯一事实源）。
    """
    name = child.get("event", "child_event")
    top_name = _renamed_child_name(name)
    return {
        "version": PROTOCOL_VERSION,
        "ts": _now_iso(),
        "run_id": run_id,
        "stage": stage,
        "event": top_name,
        "source": SUBPROCESS_SOURCE_PREFIX + str(name),
        "payload": child,
    }


# ═══════════════════════════════════════════════════
# 3. orchestrator 生成事件
# ═══════════════════════════════════════════════════


def ready_event(run_id: str) -> Dict:
    """启动握手事件（§5.11），orchestrator 首条输出。"""
    return {"event": "ready", "version": PROTOCOL_VERSION, "run_id": run_id}


# ═══════════════════════════════════════════════════
# 4. MarkerTracker — marker_result upsert + summary 覆盖（D3）
# ═══════════════════════════════════════════════════

MARKER_STATUSES = ("success", "partial", "failed", "skipped")


class MarkerTracker:
    """维护 marker_id → marker_result 映射并对计数做 upsert/覆盖（§5.4, §5.5）。

    - `upsert(marker_result)`：按 marker_id 覆盖明细；最新状态决定计数。
    - `counts()`：从最新 upsert 重算 success/partial/failed/skipped；若收到过
      `summary`（权威覆盖）则返回 summary 快照。
    - `overwrite(summary)`：用 summary 计数覆盖（不累加），供权威快照。
    """

    def __init__(self) -> None:
        self._results: Dict[str, dict] = {}
        self._override: Optional[Dict[str, int]] = None

    def upsert(self, marker_result: dict) -> None:
        """按 marker_id 覆盖存储该 marker 的最新明细（§5.4）。"""
        marker_id = marker_result.get("marker_id")
        if marker_id is None:
            raise ValueError("marker_result missing 'marker_id'")
        self._results[marker_id] = marker_result
        # 新的 marker_result 意味着回归「从最新明细重算」的语义（D3）。
        self._override = None

    def counts(self) -> Dict[str, int]:
        """当前统计算术：有 summary 覆盖则返回覆盖快照，否则从最新 upsert 重算。"""
        if self._override is not None:
            return dict(self._override)
        counts = {status: 0 for status in MARKER_STATUSES}
        for result in self._results.values():
            status = result.get("status")
            if status in counts:
                counts[status] += 1
        return counts

    def overwrite(self, summary: dict) -> None:
        """用 `summary` 计数覆盖当前统计（§5.5，权威快照，非累加）。"""
        self._override = {status: summary.get(status, 0) for status in MARKER_STATUSES}


# ═══════════════════════════════════════════════════
# 5. TerminalGuard — D4 终态唯一性校验
# ═══════════════════════════════════════════════════

# fatal 之后唯一还允许的业务事件（§5.10.1）。
_AFTER_FATAL_ALLOWED = frozenset({"summary", "completed"})


class TerminalGuard:
    """实施 D4 终态规则：最多一条 fatal、恰好一条 completed 且必为最末业务终态。

    - `note(event_kind)`：顺序递增校验，违规抛 `ValueError`。
      最多一条 `fatal`；不允许先 `completed` 再 `fatal`；不允许两个 `completed`；
      `fatal` 之后只允许 `summary` / `completed`。
    - `certify()`：最终状态校验——必须恰好一条 `completed`（即 fatal 之后必须
      且仅一次 completed），否则抛 `ValueError`。
    """

    def __init__(self) -> None:
        self._fatal_count = 0
        self._completed_count = 0

    @property
    def fatal_seen(self) -> bool:
        return self._fatal_count >= 1

    @property
    def completed_seen(self) -> bool:
        return self._completed_count >= 1

    def note(self, event_kind: str) -> None:
        """记录一条事件并按 D4 规则判定合法性与顺序，违规抛 `ValueError`。"""
        if event_kind == "fatal":
            if self._completed_count >= 1:
                raise ValueError("D4 violation: 'completed' emitted before 'fatal'")
            if self._fatal_count >= 1:
                raise ValueError("D4 violation: at most one 'fatal' allowed")
            self._fatal_count += 1
            return
        if event_kind == "completed":
            if self._completed_count >= 1:
                raise ValueError("D4 violation: at most one 'completed' allowed")
            self._completed_count += 1
            return
        # 其它业务事件
        if self._fatal_count >= 1 and event_kind not in _AFTER_FATAL_ALLOWED:
            raise ValueError(
                f"D4 violation: '{event_kind}' not allowed after 'fatal' "
                f"(only {'/' .join(sorted(_AFTER_FATAL_ALLOWED))})"
            )

    def certify(self) -> bool:
        """最终状态校验（D4）：必须恰好一条 `completed`，且 fatal 之后亦须恰好一次。"""
        if self._completed_count != 1:
            raise ValueError(
                f"D4 violation: expected exactly one 'completed', got {self._completed_count}"
            )
        # 若发出过 fatal，completed 必然已在（_completed_count == 1 已保证）。
        return True


# ═══════════════════════════════════════════════════
# 6. redact — seeded registry 脱敏（§8）
# ═══════════════════════════════════════════════════


def redact(text: str, registry: Sequence[str]) -> str:
    """把 text 中命中 registry 的脱敏值替换为 `[REDACTED]`。

    只替换 registry 中**明确命中**的值（seeded registry）；registry 外的文本不
    动（§12 承认无法自动识别任意患者文本，脱敏只对 seeded 值成立）。
    """
    for value in registry:
        if value:
            text = text.replace(value, REDACT_MARK)
    return text
