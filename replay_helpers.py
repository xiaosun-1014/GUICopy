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


# Exact, unambiguous query keys that carry credential material. Any ``key``
# whose normalized form contains a CREDENTIAL_FAMILY token (e.g. ``sessionId``,
# ``tokenType``) is also treated as sensitive; this explicit set only catches
# keys that would otherwise be missed by family matching (``code``/``key`` are
# too generic to family-match safely, while ``username`` etc. are EMR identity
# fields with no generic family token).
KNOWN_QUERY_SECRET_KEYS = {
    # family-matched credentials (kept here for explicit documentation / tests)
    "token", "session", "auth", "refresh_token", "access_token", "authtoken",
    "id_token", "password", "passwd", "cookie", "sig", "apikey", "api_key",
    # exact-match identity / route keys (safe to remove; not generic words)
    "code", "key", "secret", "tokentype", "tokenid", "sessionid",
    "refreshtoken", "accesstoken", "idtoken", "signatureid",
    "signature", "sig",
    "username", "userid", "user_id", "uniqueid", "unique_id",
    "studyuid", "study_uid", "patientid", "patient_id",
    "locationcode", "location_code", "vnaaddress", "vna_address",
    "webvieweraddress", "webviewer_address", "authorization",
}

# Substring families whose presence marks a normalized key as credential
# material. ``tokenid`` / ``sessionid`` etc. match via their family token, so
# they need no explicit entry; this is deliberately separate from the exact set
# so a generic word like ``key`` never family-matches ordinary keys.
#
# Family matching is deliberately *prefix-anchored with a bounded suffix
# vocabulary*: a family token only fires when the normalized key **starts with**
# the token and the remainder is a known credential indicator ('id', 'value',
# 'type', 'name', 'key', 'expiry', 'expires', 'ttl', 'nonce', 'secret', or
# empty). This prevents ordinary business fields that merely *contain* a family
# substring ('authorname' contains 'auth', 'tokenamount' contains 'token',
# 'sessionname' contains 'session', 'cookiecount' contains 'cookie') from being
# misclassified as credentials and stripped from otherwise-safe URLs.
_FAMILY_TOKENS = ("token", "session", "auth", "secret", "password",
                  "passwd", "credential", "cookie")
_FAMILY_INDICATORS = (
    "", "id", "value", "val", "type", "name", "key", "expiry",
    "expires", "ttl", "nonce", "secret", "token", "header", "bearer",
)


def _family_hits(normalized: str) -> bool:
    for token in _FAMILY_TOKENS:
        # Token must anchor the key as a prefix; the remainder is then checked
        # against a bounded indicator vocabulary. This excludes ordinary fields
        # that merely contain the family later in the word (``authorname``
        # contains ``auth``, ``tokenamount`` contains ``token``).
        if not normalized.startswith(token):
            continue
        suffix = normalized[len(token):]
        if suffix == "" or suffix in _FAMILY_INDICATORS:
            return True
    return False

_TEXT_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)


def _normalize_query_key(key: str) -> str:
    """Lowercase and strip non-alphanumeric separators from a query key.

    ``sessionId`` -> ``sessionid``; ``vna_address`` -> ``vnaaddress``;
    ``access-token`` -> ``accesstoken``. Normalization lets a single family
    check cover size/case and separator variants.
    """
    return re.sub(r"[^a-z0-9]", "", key.lower())


def _is_sensitive_query_key(key: str) -> bool:
    """Return True when a URL query key carries credential material.

    The single, shared classification used by both the post-capture scrubber
    and the privacy scanner so the two can never drift apart. A key is
    sensitive when either:
    * its exact normalized form is in :data:`KNOWN_QUERY_SECRET_KEYS`, or
    * its normalized form contains a credential family token
      (``token``/``session``/``auth``/…).

    Example coverage: ``sessionId``, ``tokenType``, ``authToken``, ``sig``,
    ``code``, ``username``, ``uniqueid``, ``vna_address``, ``webvieweraddress``.
    """
    normalized = _normalize_query_key(key)
    if normalized in KNOWN_QUERY_SECRET_KEYS:
        return True
    if "apikey" in normalized or "apikey" == normalized:
        return True
    return _family_hits(normalized)


def strip_known_query_secrets(text: str) -> str:
    """Remove credential-bearing query pairs from URLs embedded in text.

    This is intended for persisted post-capture artifacts. Unlike
    :func:`redact_url`, it removes the sensitive key as well as its value so a
    later privacy scan cannot mistake a safe placeholder for a live secret.
    Non-secret query parameters are preserved.
    """
    def replace(match: re.Match[str]) -> str:
        candidate = match.group(0)
        parsed = urlsplit(candidate)
        query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
        fragment_path, separator, fragment_query = parsed.fragment.partition("?")
        fragment_pairs = parse_qsl(fragment_query, keep_blank_values=True) if separator else []
        if not any(
            _is_sensitive_query_key(key)
            for key, _ in (*query_pairs, *fragment_pairs)
        ):
            return candidate
        safe_query = urlencode([
            (key, value)
            for key, value in query_pairs
            if not _is_sensitive_query_key(key)
        ])
        safe_fragment_query = urlencode([
            (key, value)
            for key, value in fragment_pairs
            if not _is_sensitive_query_key(key)
        ])
        safe_fragment = fragment_path
        if separator and safe_fragment_query:
            safe_fragment += "?" + safe_fragment_query
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, safe_query, safe_fragment))

    return _TEXT_URL_RE.sub(replace, text)


def _known_source_query_value(sample: str) -> str | None:
    """Return the first sensitive query key found in a URL query string.

    Used by :func:`scan_text_for_secrets` so a captured URL like
    ``https://app.test/?access_token=abc`` is recognized without embedding the
    actual value anywhere downstream. Shares :func:`_is_sensitive_query_key`
    with the post-capture scrubber so scanner and scrubber can never drift.
    """
    marker = "?"
    if marker not in sample:
        return None
    for key, _ in parse_qsl(sample.split(marker, 1)[1], keep_blank_values=True):
        if _is_sensitive_query_key(key):
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
