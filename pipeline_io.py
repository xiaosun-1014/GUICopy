from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from replay_helpers import redact_url


@dataclass(frozen=True)
class RunLayout:
    root: Path
    source_dir: Path
    adapter_dir: Path
    capture_dir: Path
    replica_dir: Path
    validation_dir: Path
    logs_dir: Path
    state_path: Path
    events_path: Path
    report_json: Path
    report_html: Path


def create_run_layout(
    output_root: Path, hospital: str, run_id: str
) -> RunLayout:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", hospital):
        raise ValueError("hospital name contains unsafe path characters")
    if not re.fullmatch(r"[A-Za-z0-9T_-]+", run_id):
        raise ValueError("run id contains unsafe path characters")
    root = output_root / hospital / "runs" / run_id
    directories = {
        "source_dir": root / "source",
        "adapter_dir": root / "adapter",
        "capture_dir": root / "capture",
        "replica_dir": root / "replica",
        "validation_dir": root / "validation",
        "logs_dir": root / "logs",
    }
    for directory in directories.values():
        directory.mkdir(parents=True, exist_ok=True)
    return RunLayout(
        root=root,
        **directories,
        state_path=root / "pipeline_state.json",
        events_path=root / "pipeline_events.jsonl",
        report_json=root / "pipeline_report.json",
        report_html=root / "pipeline_report.html",
    )


SECRET_KEYS = {
    "authorization", "cookie", "password", "secret",
    "storage_state", "access_token", "refresh_token",
}


def redact_payload(value):
    if isinstance(value, dict):
        return {
            key: ("REDACTED" if key.lower() in SECRET_KEYS else redact_payload(item))
            for key, item in value.items()
            if key.lower() != "password"
        }
    if isinstance(value, list):
        return [redact_payload(item) for item in value]
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        return redact_url(value)
    return value


class PipelineStore:
    def __init__(self, layout: RunLayout):
        self._layout = layout
        self._lock = threading.Lock()

    def emit(self, event: dict[str, Any]) -> None:
        line = json.dumps(
            redact_payload(event), ensure_ascii=False
        ) + "\n"
        with self._lock:
            with self._layout.events_path.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()

    def write_state(self, state) -> None:
        temporary = self._layout.state_path.with_suffix(
            self._layout.state_path.suffix + ".tmp"
        )
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(state.to_dict(), handle, ensure_ascii=False, indent=2)
            handle.flush()
        os.replace(temporary, self._layout.state_path)
