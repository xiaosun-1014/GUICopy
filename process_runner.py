"""Concurrency-safe managed subprocess with dual-stream reading and tree termination.

This module provides :class:`ManagedProcess`, a reusable wrapper around
``subprocess.Popen`` that:

- starts the child with stdin/stdout/stderr all connected to PIPE (text mode,
  line buffered);
- runs one daemon reader thread per output stream, pushing ``(stream, line)``
  tuples into a single queue (never a blocking end-of-process ``stderr.read()``
  that can deadlock when the stderr pipe fills up);
- parses JSON objects only from complete stdout lines and forwards dicts that
  carry an ``"event"`` key to a caller-supplied ``on_event`` callback;
- retains ordinary stdout and stderr text separately;
- exposes ``start()`` / ``send_command()`` / ``cancel()`` / ``wait()`` / ``run()``;
- terminates only the exact spawned process tree (never a name-based kill);
- returns a result with ``pid`` / ``returncode`` / ``stdout`` / ``stderr`` /
  ``timed_out`` / ``cancelled``.
"""

from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO


@dataclass
class ManagedProcessResult:
    """Outcome of running a :class:`ManagedProcess`."""

    args: Sequence[str]
    pid: int | None
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    cancelled: bool = False


def _child_env(explicit_env: Mapping[str, str] | None) -> dict[str, str] | None:
    """Build the child environment, forcing UTF-8 stdio regardless of host locale.

    Children emit UTF-8 JSON events on stdout (e.g. adapter marker events whose
    names are Chinese). On a Windows host without ``PYTHONIOENCODING`` set, the
    child's ``sys.stdout`` defaults to the ANSI code page (GBK), and printing a
    JSON string that already contains the UTF-8 replacement char ``\\ufffd``
    raises ``'gbk' codec can't encode character '\\ufffd'`` mid-stream. Force
    UTF-8 I/O (and UTF-8 mode) so no GBK console/locale can crash the child.
    """
    if explicit_env is None:
        env = dict(os.environ)
    else:
        env = dict(explicit_env)
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("PYTHONUTF8", "1")
    return env


@dataclass
class ManagedProcess:
    """Spawn and supervise one child process, reading both streams concurrently.

    Parameters
    ----------
    args:
        Command line (including the executable) to run.
    cwd:
        Working directory for the child.
    env:
        Environment for the child (defaults to the parent's environment).
    timeout_s:
        Maximum seconds to wait for the child. ``0`` means an immediate timeout
        (the deadline is ``now`` and the first wait yields nothing); a positive
        value sets the real deadline. There is no "disabled" sentinel — pass a
        large positive value if only a nominal bound is desired.
    on_event:
        Optional callback invoked for each parsed JSON dict on stdout that
        contains an ``"event"`` key.
    grace_period_s:
        Seconds to wait after the first termination attempt before issuing the
        forceful kill.
    """

    args: Sequence[str]
    cwd: str | Path | None = None
    env: Mapping[str, str] | None = None
    timeout_s: float = 900.0
    on_event: Callable[[dict[str, Any]], None] | None = None
    grace_period_s: float = 5.0

    _process: subprocess.Popen[str] | None = field(default=None, init=False, repr=False)
    _queue: queue.Queue[tuple[str, str | None]] = field(
        default_factory=queue.Queue, init=False, repr=False
    )
    _threads: list[threading.Thread] = field(default_factory=list, init=False, repr=False)
    _stdin: TextIO | None = field(default=None, init=False, repr=False)
    _timed_out: bool = field(default=False, init=False)
    _cancelled: bool = field(default=False, init=False)

    # ---- lifecycle ------------------------------------------------

    def start(self) -> "ManagedProcess":
        """Spawn the child and begin reader threads. Returns self."""
        if self._process is not None:
            raise RuntimeError("process already started")
        creationflags = (
            subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        )
        start_new_session = os.name != "nt"
        process = subprocess.Popen(
            list(self.args),
            cwd=self.cwd,
            env=_child_env(self.env),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
            start_new_session=start_new_session,
        )
        self._process = process
        self._stdin = process.stdin
        for stream_name, stream in (
            ("stdout", process.stdout),
            ("stderr", process.stderr),
        ):
            if stream is None:
                continue
            thread = threading.Thread(
                target=self._reader,
                args=(stream_name, stream),
                name=f"ManagedProcess-{stream_name}-{process.pid}",
                daemon=True,
            )
            thread.start()
            self._threads.append(thread)
        return self

    def run(self) -> ManagedProcessResult:
        """Start and wait, returning the result."""
        self.start()
        return self.wait()

    def send_command(self, payload: Any) -> None:
        """Write a JSON line command to the child's stdin and flush."""
        if self._stdin is None:
            raise RuntimeError("process stdin is not available")
        self._stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self._stdin.flush()

    def cancel(self) -> None:
        """Terminate the exact process tree and mark the run cancelled."""
        self._cancelled = True
        self._terminate_tree()

    def wait(self) -> ManagedProcessResult:
        """Block until the child exits, times out, or is cancelled."""
        if self._process is None:
            raise RuntimeError("process not started")
        process = self._process
        deadline = time.monotonic() + self.timeout_s
        stdout_lines: list[str] = []
        stderr_lines: list[str] = []
        ended = {"stdout": False, "stderr": False}
        timed_out = False
        while not (ended["stdout"] and ended["stderr"]):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            try:
                stream_name, line = self._queue.get(timeout=min(0.25, remaining))
            except queue.Empty:
                continue
            if line is None:
                ended[stream_name] = True
                continue
            if stream_name == "stdout":
                stdout_lines.append(line)
                self._maybe_emit(line)
            else:
                stderr_lines.append(line)
        if timed_out:
            self._timed_out = True
        if timed_out or self._cancelled:
            self._terminate_tree()
        self._join_readers()
        # Both streams reached EOF, so the (direct) child has exited. wait()
        # reaps it and yields the real return code; it cannot block because the
        # child already closed both stdout and stderr.
        returncode = process.wait()
        self._close_streams()
        return ManagedProcessResult(
            args=self.args,
            pid=process.pid,
            returncode=returncode,
            stdout="".join(stdout_lines),
            stderr="".join(stderr_lines),
            timed_out=self._timed_out or timed_out,
            cancelled=self._cancelled,
        )

    # ---- internals ------------------------------------------------

    def _reader(self, stream_name: str, stream: TextIO) -> None:
        for line in stream:
            self._queue.put((stream_name, line))
        self._queue.put((stream_name, None))

    def _maybe_emit(self, line: str) -> None:
        if self.on_event is None:
            return
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return
        if isinstance(event, dict) and "event" in event:
            self.on_event(event)

    def _terminate_tree(self) -> None:
        process = self._process
        if process is None or process.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                check=False,
                capture_output=True,
                text=True,
            )
        else:
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=self.grace_period_s)
        except subprocess.TimeoutExpired:
            # Forceful termination after the grace period.
            if os.name != "nt":
                os.killpg(process.pid, signal.SIGKILL)

    def _join_readers(self) -> None:
        for thread in self._threads:
            thread.join(timeout=max(1.0, self.grace_period_s))
        self._threads = []

    def _close_streams(self) -> None:
        process = self._process
        if process is None:
            return
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
