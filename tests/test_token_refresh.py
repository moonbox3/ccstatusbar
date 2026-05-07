"""Tests for OAuth token refresh + write-back (issue 05)."""

import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import statusline


class FakeResponse:
    def __init__(self, body):
        self._body = body if isinstance(body, bytes) else body.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


class RefreshOAuthTokenTests(unittest.TestCase):
    def test_ssrf_guard_rejects_other_url(self):
        called = []

        def opener(req, timeout=None):
            called.append(req)
            return FakeResponse(b"{}")

        out = statusline._refresh_oauth_token(
            "rt", url="https://evil.example.com/oauth/token",
            urlopen=opener,
        )
        self.assertIsNone(out)
        self.assertEqual(called, [])

    def test_allows_documented_url(self):
        body = json.dumps({
            "access_token": "AT2",
            "refresh_token": "RT2",
            "expires_at": 9999999999999,
        })

        captured = {}

        def opener(req, timeout=None):
            captured["url"] = req.full_url
            captured["data"] = req.data
            return FakeResponse(body)

        out = statusline._refresh_oauth_token(
            "RT1", urlopen=opener, client_id="cid-x")
        self.assertEqual(captured["url"], statusline.OAUTH_TOKEN_URL)
        sent = json.loads(captured["data"].decode("utf-8"))
        self.assertEqual(sent["grant_type"], "refresh_token")
        self.assertEqual(sent["refresh_token"], "RT1")
        self.assertEqual(sent["client_id"], "cid-x")
        self.assertEqual(out["accessToken"], "AT2")
        self.assertEqual(out["refreshToken"], "RT2")
        self.assertEqual(out["expiresAt"], 9999999999999)

    def test_default_client_id_used(self):
        captured = {}

        def opener(req, timeout=None):
            captured["data"] = req.data
            return FakeResponse(json.dumps({"access_token": "A"}))

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CLAUDE_CODE_OAUTH_CLIENT_ID", None)
            statusline._refresh_oauth_token("rt", urlopen=opener)
        sent = json.loads(captured["data"].decode("utf-8"))
        self.assertEqual(sent["client_id"], statusline.OAUTH_CLIENT_ID_DEFAULT)

    def test_env_override_client_id(self):
        captured = {}

        def opener(req, timeout=None):
            captured["data"] = req.data
            return FakeResponse(json.dumps({"access_token": "A"}))

        with mock.patch.dict(os.environ,
                             {"CLAUDE_CODE_OAUTH_CLIENT_ID": "env-cid"}):
            statusline._refresh_oauth_token("rt", urlopen=opener)
        sent = json.loads(captured["data"].decode("utf-8"))
        self.assertEqual(sent["client_id"], "env-cid")

    def test_expires_in_converted(self):
        body = json.dumps({"access_token": "A", "expires_in": 3600})

        def opener(req, timeout=None):
            return FakeResponse(body)

        out = statusline._refresh_oauth_token("rt", urlopen=opener)
        self.assertIsNotNone(out["expiresAt"])
        self.assertGreater(out["expiresAt"], 0)

    def test_refresh_token_preserved_when_omitted(self):
        body = json.dumps({"access_token": "A2"})

        def opener(req, timeout=None):
            return FakeResponse(body)

        out = statusline._refresh_oauth_token("RT_OLD", urlopen=opener)
        self.assertEqual(out["refreshToken"], "RT_OLD")

    def test_malformed_response_returns_none(self):
        def opener(req, timeout=None):
            return FakeResponse(b"not-json")

        out = statusline._refresh_oauth_token("rt", urlopen=opener)
        self.assertIsNone(out)

    def test_missing_access_token_returns_none(self):
        def opener(req, timeout=None):
            return FakeResponse(json.dumps({"refresh_token": "x"}))

        out = statusline._refresh_oauth_token("rt", urlopen=opener)
        self.assertIsNone(out)

    def test_network_error_returns_none(self):
        def opener(req, timeout=None):
            raise OSError("connection refused")

        out = statusline._refresh_oauth_token("rt", urlopen=opener)
        self.assertIsNone(out)

    def test_empty_refresh_token_rejected(self):
        out = statusline._refresh_oauth_token("", urlopen=lambda *a, **k: None)
        self.assertIsNone(out)


class MergeOAuthTests(unittest.TestCase):
    def test_merges_into_nested_shape(self):
        creds = {"claudeAiOauth": {"accessToken": "old", "extra": "keep"}}
        merged = statusline._merge_oauth(
            creds, {"accessToken": "new", "expiresAt": 123})
        self.assertEqual(merged["claudeAiOauth"]["accessToken"], "new")
        self.assertEqual(merged["claudeAiOauth"]["expiresAt"], 123)
        self.assertEqual(merged["claudeAiOauth"]["extra"], "keep")

    def test_merges_into_flat_shape(self):
        creds = {"accessToken": "old", "other": 1}
        merged = statusline._merge_oauth(creds, {"accessToken": "new"})
        self.assertEqual(merged["accessToken"], "new")
        self.assertEqual(merged["other"], 1)

    def test_skips_none_values(self):
        creds = {"claudeAiOauth": {"expiresAt": 99}}
        merged = statusline._merge_oauth(
            creds, {"accessToken": "n", "expiresAt": None})
        self.assertEqual(merged["claudeAiOauth"]["expiresAt"], 99)


class WriteFileCredentialsTests(unittest.TestCase):
    def test_atomic_write_preserves_mode(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            with mock.patch.object(Path, "home", lambda: home):
                target = home / ".claude" / ".credentials.json"
                target.parent.mkdir(parents=True)
                target.write_text(json.dumps({"old": True}))
                os.chmod(target, 0o600)

                ok = statusline._write_file_credentials({"new": True})
                self.assertTrue(ok)

                self.assertEqual(json.loads(target.read_text()), {"new": True})
                mode = stat.S_IMODE(target.stat().st_mode)
                self.assertEqual(mode, 0o600)

    def test_creates_with_default_0600_when_missing(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            with mock.patch.object(Path, "home", lambda: home):
                ok = statusline._write_file_credentials({"x": 1})
                self.assertTrue(ok)
                target = home / ".claude" / ".credentials.json"
                mode = stat.S_IMODE(target.stat().st_mode)
                self.assertEqual(mode, 0o600)


class GetAccessTokenRefreshTests(unittest.TestCase):
    """Integration of get_access_token with refresh + write-back."""

    def _load(self, creds, source):
        def fn():
            return creds, source
        return fn

    def test_non_expired_token_skips_refresh(self):
        refresh_calls = []

        def refresh(rt):
            refresh_calls.append(rt)
            return None

        write_calls = []
        creds = {"claudeAiOauth": {
            "accessToken": "AT", "refreshToken": "RT",
            "expiresAt": 10_000_000,
        }}
        token, err = statusline.get_access_token(
            now_ms=1_000_000,
            refresh_fn=refresh,
            write_fn=lambda c, s: write_calls.append((c, s)),
            load_fn=self._load(creds, "file"),
        )
        self.assertEqual(token, "AT")
        self.assertIsNone(err)
        self.assertEqual(refresh_calls, [])
        self.assertEqual(write_calls, [])

    def test_expired_token_refreshes_and_writes_back(self):
        creds = {"claudeAiOauth": {
            "accessToken": "OLD", "refreshToken": "RT_OLD",
            "expiresAt": 1_000_000,
        }}

        def refresh(rt):
            self.assertEqual(rt, "RT_OLD")
            return {"accessToken": "NEW", "refreshToken": "RT_NEW",
                    "expiresAt": 9_000_000}

        write_calls = []
        token, err = statusline.get_access_token(
            now_ms=2_000_000,
            refresh_fn=refresh,
            write_fn=lambda c, s: write_calls.append((c, s)),
            load_fn=self._load(creds, "keychain"),
        )
        self.assertEqual(token, "NEW")
        self.assertIsNone(err)
        self.assertEqual(len(write_calls), 1)
        written, source = write_calls[0]
        self.assertEqual(source, "keychain")
        self.assertEqual(written["claudeAiOauth"]["accessToken"], "NEW")
        self.assertEqual(written["claudeAiOauth"]["refreshToken"], "RT_NEW")

    def test_expiry_buffer_triggers_refresh(self):
        # token "valid" by raw expiresAt but within 60s buffer → refresh.
        creds = {"claudeAiOauth": {
            "accessToken": "OLD", "refreshToken": "RT",
            "expiresAt": 1_000_000,
        }}
        called = []

        def refresh(rt):
            called.append(rt)
            return {"accessToken": "NEW"}

        token, err = statusline.get_access_token(
            now_ms=1_000_000 - 30_000,  # 30s before expiry, within 60s buffer
            refresh_fn=refresh, write_fn=lambda *a: True,
            load_fn=self._load(creds, "file"),
        )
        self.assertEqual(token, "NEW")
        self.assertEqual(called, ["RT"])

    def test_refresh_failure_returns_auth_error(self):
        creds = {"claudeAiOauth": {
            "accessToken": "OLD", "refreshToken": "RT",
            "expiresAt": 1_000_000,
        }}
        token, err = statusline.get_access_token(
            now_ms=2_000_000,
            refresh_fn=lambda rt: None,
            write_fn=lambda *a: True,
            load_fn=self._load(creds, "file"),
        )
        self.assertIsNone(token)
        self.assertEqual(err, "auth")

    def test_no_refresh_token_returns_auth_error(self):
        creds = {"claudeAiOauth": {
            "accessToken": "OLD", "expiresAt": 1_000_000,
        }}
        token, err = statusline.get_access_token(
            now_ms=2_000_000,
            load_fn=self._load(creds, "file"),
        )
        self.assertIsNone(token)
        self.assertEqual(err, "auth")

    def test_no_creds_silent(self):
        token, err = statusline.get_access_token(
            load_fn=self._load(None, None))
        self.assertIsNone(token)
        self.assertIsNone(err)

    def test_garbage_expires_at_returns_auth(self):
        creds = {"claudeAiOauth": {
            "accessToken": "X", "expiresAt": "not-a-number",
        }}
        token, err = statusline.get_access_token(
            load_fn=self._load(creds, "file"))
        self.assertIsNone(token)
        self.assertEqual(err, "auth")


class RenderAuthMarkerTests(unittest.TestCase):
    def test_render_emits_api_auth_on_auth_error(self):
        with mock.patch.object(statusline, "get_access_token",
                               return_value=(None, "auth")), \
             mock.patch.object(statusline, "get_git_status",
                               return_value={"in_repo": False}), \
             mock.patch.object(statusline, "parse_transcript",
                               return_value=None):
            payload = {"model": {"display_name": "Opus"},
                       "workspace": {"current_dir": "/tmp/xyz"}}
            line = statusline.render(payload)
        self.assertIn("[API auth]", line)
        # Marker sits before model
        self.assertLess(line.index("[API auth]"), line.index("Opus"))

    def test_render_omits_marker_for_apikey_user(self):
        with mock.patch.object(statusline, "get_access_token",
                               return_value=(None, None)), \
             mock.patch.object(statusline, "get_git_status",
                               return_value={"in_repo": False}), \
             mock.patch.object(statusline, "parse_transcript",
                               return_value=None):
            payload = {"model": {"display_name": "Opus"},
                       "workspace": {"current_dir": "/tmp/xyz"}}
            line = statusline.render(payload)
        self.assertNotIn("[API auth]", line)


if __name__ == "__main__":
    unittest.main()
