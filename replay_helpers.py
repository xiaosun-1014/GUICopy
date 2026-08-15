"""Manifest, redaction, and localhost helpers for offline replica replay."""

from __future__ import annotations

import hashlib
import json
import re
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


def series_key_slug(series_key: str, salt: str = "") -> str:
    """Deterministic, non-reversible public slug for a series key.

    The raw ``series_key`` may carry a real SeriesInstanceUID or
    patient-derived text, so it must never be written verbatim into served
    replica HTML (``data-replica-series-key`` / route maps), logs, reports, or
    manifest routes. This returns a short SHA-256 prefix that is stable across
    builds of the same replica and whose collisions are negligible for the
    bounded (<= max_series) set of keys in a single flow.
    """
    digest = hashlib.sha256(f"{salt}::{series_key}".encode("utf-8")).hexdigest()
    return digest[:12]


# Keys that carry high-confidence credential material in URL query strings.
KNOWN_QUERY_SECRET_KEYS = {
    "token", "access_token", "refresh_token", "auth", "authtoken",
    "apikey", "api_key", "key", "sig", "password", "passwd", "code",
    "id_token", "session", "cookie",
}


def _known_source_query_value(sample: str) -> str | None:
    """Return the first known secret key found in a URL query string.

    Used by :func:`scan_text_for_secrets` so a captured URL like
    ``https://app.test/?access_token=abc`` is recognized without embedding the
    actual value anywhere downstream.
    """
    marker = "?"
    if marker not in sample:
        return None
    for key, _ in parse_qsl(sample.split(marker, 1)[1], keep_blank_values=True):
        if key.lower() in KNOWN_QUERY_SECRET_KEYS:
            return key
    return None


SECRET_PATTERN_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("authorization_header", re.compile(r"(?i)\bauthorization\s*[:=]")),
    ("bearer_token", re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{8,}")),
    ("cookie_dump", re.compile(r"(?i)\b(set-cookie|cookie)\s*[:=]")),
    ("password_value", re.compile(r"(?i)\bpassword\s*[:=]")),
)


def scan_text_for_secrets(text: str) -> list[str]:
    """Return the rule names for every credential pattern found in ``text``.

    Always keyed on a fixed rule name, never on the matched secret itself, so
    downstream callers can report *which* rule fired without leaking the value.
    """
    matched: list[str] = []
    for name, pattern in SECRET_PATTERN_RULES:
        if pattern.search(text):
            matched.append(name)
    known_query = _known_source_query_value(text)
    if known_query is not None:
        matched.append("known_source_query")
    return matched



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
