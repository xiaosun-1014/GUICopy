from __future__ import annotations

import ast
import hashlib
import py_compile
from dataclasses import dataclass
from pathlib import Path

import agent


@dataclass(frozen=True)
class AdapterGenerationResult:
    status: str
    model: str
    marker_names: tuple[str, ...]
    output_path: Path
    output_sha256: str
    events: tuple[dict[str, object], ...]


def generate_completed_adapter(
    source_path: Path,
    output_path: Path,
    model: str | None = None,
    retry_count: int = 3,
) -> AdapterGenerationResult:
    source = source_path.read_text(encoding="utf-8")
    events: list[dict[str, object]] = []
    selected_model = model or agent.DEFAULT_MODEL
    completed = agent.process_script(
        source,
        max_retries=retry_count,
        model=selected_model,
        event_sink=events.append,
    )
    ast.parse(completed)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary.write_text(completed, encoding="utf-8", newline="\n")
    py_compile.compile(str(temporary), doraise=True)
    temporary.replace(output_path)
    digest = hashlib.sha256(completed.encode("utf-8")).hexdigest()
    return AdapterGenerationResult(
        "success",
        selected_model,
        tuple(marker["name"] for marker in agent.parse_markers(source)),
        output_path,
        digest,
        tuple(events),
    )


def main() -> None:
    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser(
        description="从 processed 脚本生成带事件追踪的 completed adapter"
    )
    parser.add_argument("--source", required=True, help="processed 脚本路径")
    parser.add_argument("--output", required=True, help="completed adapter 输出路径")
    parser.add_argument("--model", default=None, help="LLM 模型名（默认 agent.DEFAULT_MODEL）")
    parser.add_argument("--retry", type=int, default=3, help="每个 marker 的 LLM 重试次数")
    args = parser.parse_args()

    source_path = Path(args.source)
    output_path = Path(args.output)

    try:
        result = generate_completed_adapter(
            source_path,
            output_path,
            model=args.model,
            retry_count=args.retry,
        )
    except Exception as e:  # noqa: BLE001 - CLI 顶层透出诊断到 stderr
        print(f"adapter generation failed: {e}", file=sys.stderr)
        print(json.dumps(
            {"event": "adapter_generated", "status": "failed", "reason": str(e)}
        ), file=sys.stdout)
        sys.exit(1)

    for event in result.events:
        print(json.dumps(event, ensure_ascii=False), file=sys.stdout)

    print(json.dumps({
        "event": "adapter_generated",
        "status": "success",
        "output": output_path.name,
        "output_sha256": result.output_sha256,
    }), file=sys.stdout)


if __name__ == "__main__":
    main()
