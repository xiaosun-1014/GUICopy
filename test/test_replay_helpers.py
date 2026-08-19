"""Regression tests for query-secret scrubbing and the shared sensitivity key.

Critical-1 coverage: the post-capture scrubber and the privacy scanner must
agree on what counts as a credential query key, and the scrubber must strip
real EMR/Dapeng identity fields (sessionId / tokenType / username / studyUid /
uniqueid / vna_address / webviewer_address), not just the small set that used
to fit in ``KNOWN_QUERY_SECRET_KEYS``.

All text fixtures are read/written as UTF-8; byte-oriented greps are never used
because UTF-8 content cannot be reliably matched byte-wise on Windows.
"""

from __future__ import annotations

import io
import unittest
from pathlib import Path

from replay_helpers import (
    _is_sensitive_query_key,
    _normalize_query_key,
    scan_text_for_secrets,
    strip_known_query_secrets,
)


class NormalizeKeyTests(unittest.TestCase):
    def test_lowercase_and_separator_strip(self):
        self.assertEqual(_normalize_query_key("sessionId"), "sessionid")
        self.assertEqual(_normalize_query_key("SESSION-ID"), "sessionid")
        self.assertEqual(_normalize_query_key("vna_address"), "vnaaddress")
        self.assertEqual(_normalize_query_key("access-token_2"), "accesstoken2")
        self.assertEqual(_normalize_query_key("plain"), "plain")


class SensitiveKeyClassificationTests(unittest.TestCase):
    def test_exact_set_members_are_sensitive(self):
        for key in (
            "code", "key", "sig", "token", "password", "tokentype",
            "username", "userid", "uniqueid", "studyuid", "study_uid",
            "patientid", "locationcode", "vnaaddress", "vna_address",
            "webvieweraddress", "webviewer_address",
        ):
            self.assertTrue(_is_sensitive_query_key(key), key)

    def test_family_substring_is_sensitive_regardless_of_case_variant(self):
        # sessionId (camelCase) -> sessionid; must match via the ``session``
        # family even though it is not an exact set member.
        self.assertTrue(_is_sensitive_query_key("sessionId"))
        self.assertTrue(_is_sensitive_query_key("SESSIONID"))
        self.assertTrue(_is_sensitive_query_key("tokenType"))
        self.assertTrue(_is_sensitive_query_key("authToken"))
        self.assertTrue(_is_sensitive_query_key("X-Api-Key"))
        self.assertTrue(_is_sensitive_query_key("refreshToken"))
        self.assertTrue(_is_sensitive_query_key("access_token"))
        self.assertTrue(_is_sensitive_query_key("id_token"))
        self.assertTrue(_is_sensitive_query_key("signature"))
        self.assertTrue(_is_sensitive_query_key("signatureid"))
        self.assertTrue(_is_sensitive_query_key("authorization"))

    def test_business_fields_containing_family_substrings_are_not_sensitive(self):
        # Family matching must be prefix-anchored with a bounded indicator
        # vocabulary, or ordinary fields that merely *contain* ``auth`` /
        # ``token`` / ``session`` / ``cookie`` / ``signature`` would be stripped
        # from otherwise-safe URLs (authorname, tokenamount, sessionstart …).
        for key in (
            "authorname", "sessionstart", "sessiondata", "sessionstate",
            "tokenamount", "tokendate", "stoken", "tabletoken",
            "tokenizername", "cookiecount", "signaturename", "signaturecount",
            "keyname", "keyboard", "keyid", "screenid", "role",
            "appendtags", "syscode", "appcode", "piterminal",
        ):
            self.assertFalse(_is_sensitive_query_key(key), key)

    def test_plain_fields_are_never_sensitive(self):
        for key in (
            "screenid", "role", "appendtags", "businesstype", "syscode",
            "appcode", "piterminal", "study", "frame", "page", "view",
            "windowid", "dataset",
        ):
            self.assertFalse(_is_sensitive_query_key(key), key)


class StripQuerySecretsRegressionTests(unittest.TestCase):
    """Real Dapeng-style URLs with the exact keys observed in production."""

    _DAPENG_URL = (
        "https://zscloud.zs-hospital.sh.cn/viewer/2d/zh-cn/Dapeng/Viewer/Index"
        "?screenid=2&sessionId=WNSqqaouwkA%2BCrDZPX4vR1kVfk4eIkyrcLxWBozBRCg"
        "&studyUid=1.2.826.0.1.3680043.8.1055.1.2011110111111"
        "&tokentype=bearer&username=jincheng&uniqueid=abcdef0123456789abcdef0123456789"
        "&vna_address=http://pacs.internal:19500&webviewer_address=https://viewer.internal"
        "&role=doctor&syscode=003&appcode=001"
    )

    def test_dapeng_identity_and_session_fields_are_stripped(self):
        cleaned = strip_known_query_secrets(self._DAPENG_URL)
        self.assertNotIn("sessionId=", cleaned)
        self.assertNotIn("studyUid=", cleaned)
        self.assertNotIn("tokentype=", cleaned)
        self.assertNotIn("username=", cleaned)
        self.assertNotIn("uniqueid=", cleaned)
        self.assertNotIn("vna_address=", cleaned)
        self.assertNotIn("webviewer_address=", cleaned)
        # Non-sensitive EMR query params are preserved verbatim.
        self.assertIn("screenid=2", cleaned)
        self.assertIn("role=doctor", cleaned)
        self.assertIn("syscode=003", cleaned)
        self.assertIn("appcode=001", cleaned)

    def test_camel_case_session_id_is_stripped(self):
        cleaned = strip_known_query_secrets(
            "https://host/viewer?sessionId=WNSqREALTOKEN&mode=view"
        )
        self.assertNotIn("sessionId=", cleaned)
        self.assertIn("mode=view", cleaned)

    def test_token_type_variant_is_stripped(self):
        cleaned = strip_known_query_secrets(
            "https://host/viewer?tokenType=bearer&type=jwt"
        )
        self.assertNotIn("tokenType=", cleaned)
        self.assertIn("type=jwt", cleaned)

    def test_fragment_query_is_stripped(self):
        # ``#/shared?code=...`` puts code in the fragment, not the query.
        cleaned = strip_known_query_secrets(
            "https://zscloud.zs-hospital.sh.cn/film/#/shared?code=xg06q2"
        )
        self.assertNotIn("code=xg06q2", cleaned)
        self.assertIn("/shared", cleaned)

    def test_nested_url_in_json_value_is_stripped_utf8(self):
        # JSON-embedded value with a nested full URL plus surrounding UTF-8 text;
        # read back as text, never byte-wise.
        payload = '{"label": "序列", "viewer": "https://host/viewer?sessionId=ABCTOKEN&ok=1"}'
        cleaned = strip_known_query_secrets(payload)
        self.assertNotIn("ABCTOKEN", cleaned)
        self.assertIn("ok=1", cleaned)
        self.assertIn("序列", cleaned)

    def test_safe_query_is_preserved(self):
        source = 'page.goto("https://viewer.example.test/open?study=demo&page=2")'
        cleaned = strip_known_query_secrets(source)
        self.assertEqual(cleaned.count("study=demo"), 1)
        self.assertIn("page=2", cleaned)

    def test_utf8_file_read_round_trip(self):
        # Simulate the post-capture scrubber's read-text/write-text path on a
        # UTF-8 file that also carries Chinese comments.
        import tempfile

        content = (
            "page.goto(\"https://host/film/#/shared?code=SECRET\")\n"
            "# 备注：影像查看\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "script.py"
            path.write_text(content, encoding="utf-8")
            text = path.read_text(encoding="utf-8")
            scrubbed = strip_known_query_secrets(text)
            path.write_text(scrubbed, encoding="utf-8", newline="\n")
            reread = path.read_text(encoding="utf-8")
        self.assertNotIn("SECRET", reread)
        self.assertIn("影像查看", reread)


class ScannerAgreementTests(unittest.TestCase):
    """The privacy scanner must flag the same keys the scrubber removes."""

    def test_scanner_flags_dapeng_style_urls(self):
        source = (
            "https://host/viewer?sessionId=WNSqTOKEN&tokentype=bearer"
            "&username=jincheng&studyUid=1.2.3"
        )
        rules = scan_text_for_secrets(source)
        self.assertIn("known_source_query", rules)
        self.assertTrue(any("session" in rule for rule in rules) or "known_source_query" in rules)

    def test_scanner_does_not_flag_plain_url(self):
        rules = scan_text_for_secrets("https://host/viewer?screenid=2&role=doctor")
        self.assertNotIn("known_source_query", rules)
        self.assertEqual(rules, [])


if __name__ == "__main__":
    unittest.main()
