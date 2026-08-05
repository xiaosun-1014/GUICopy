from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class PipelineStage(str, Enum):
    DRAFT = "draft"
    PREFLIGHT = "preflight"
    ADAPTER = "generating_adapter"
    LIVE_CAPTURE = "capturing_live"
    REPLICA_BUILD = "building_replica"
    REPLICA_VALIDATION = "validating_replica"
    ADAPTER_VALIDATION = "validating_adapter"
    REPORT = "report"


class PipelineStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL = {
    PipelineStatus.SUCCESS,
    PipelineStatus.PARTIAL,
    PipelineStatus.FAILED,
    PipelineStatus.CANCELLED,
}

ORDER = [
    PipelineStage.DRAFT,
    PipelineStage.PREFLIGHT,
    PipelineStage.ADAPTER,
    PipelineStage.LIVE_CAPTURE,
    PipelineStage.REPLICA_BUILD,
    PipelineStage.REPLICA_VALIDATION,
    PipelineStage.ADAPTER_VALIDATION,
    PipelineStage.REPORT,
]


@dataclass(frozen=True)
class PipelineConfig:
    hospital: str
    source_script: Path
    annotations_path: Path
    output_root: Path
    auth_mode: str = "scripted"
    storage_state: Path | None = None
    model: str | None = None
    retry_count: int = 3
    capture_timeout_s: int = 900
    auth_timeout_s: int = 300
    process_exit_grace_s: int = 5


@dataclass(frozen=True)
class StageResult:
    stage: PipelineStage
    status: PipelineStatus
    error_category: str | None = None
    message: str = ""
    artifacts: dict[str, str] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PipelineState:
    run_id: str
    stage: PipelineStage
    status: PipelineStatus
    error_category: str | None = None

    @classmethod
    def new(cls, run_id: str) -> "PipelineState":
        return cls(run_id, PipelineStage.DRAFT, PipelineStatus.PENDING)

    def transition(
        self, stage: PipelineStage, status: PipelineStatus
    ) -> "PipelineState":
        if self.status in TERMINAL:
            raise ValueError("terminal pipeline state cannot transition")
        current = ORDER.index(self.stage)
        target = ORDER.index(stage)
        if target not in {current, current + 1}:
            raise ValueError(
                f"invalid pipeline transition: {self.stage.value} -> {stage.value}"
            )
        return PipelineState(self.run_id, stage, status)

    def finish(
        self, status: PipelineStatus, error_category: str | None = None
    ) -> "PipelineState":
        if status not in TERMINAL:
            raise ValueError("finish requires terminal status")
        return PipelineState(self.run_id, self.stage, status, error_category)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
