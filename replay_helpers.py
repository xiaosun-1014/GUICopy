"""Manifest, redaction, and localhost helpers for offline replica replay."""

from __future__ import annotations

import hashlib
import json
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from replica_models import ReplicaFlow


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def redact_url(url: str) -> str:
    parsed = urlsplit(url)
    redacted_query = urlencode([(key, "REDACTED") for key, _ in parse_qsl(parsed.query, keep_blank_values=True)])
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, redacted_query, ""))


def write_manifest(path: str | Path, flow: ReplicaFlow) -> None:
    Path(path).write_text(json.dumps(flow.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")


def read_manifest(
    path: str | Path,
    flow_root: str | Path,
    verify_source_hash: bool = False,
) -> ReplicaFlow:
    flow = ReplicaFlow.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
    if verify_source_hash:
        source = Path(flow_root) / flow.source_script_relpath
        if sha256_file(source) != flow.source_script_sha256:
            raise ValueError("source script hash does not match manifest")
    return flow


class ReplicaServer:
    """Serve generated replica files exclusively from a loopback HTTP endpoint."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        if self._server is None:
            raise RuntimeError("ReplicaServer is not running")
        return f"http://127.0.0.1:{self._server.server_port}/index.html"

    def __enter__(self) -> "ReplicaServer":
        handler = partial(SimpleHTTPRequestHandler, directory=str(self.root))
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
        if self._thread:
            self._thread.join(timeout=3)
