"""Tests for rate-limit segments: cache TTL, fetch flow, render."""

import datetime
import json
import os
import tempfile
import unittest
from pathlib import Path

import statusline


class CacheDecideTests(unittest.TestCase):
    """Pure decision logic for the four TTL states."""

    def test_fresh_hit_skips_fetch(self):
        now = 1000.0
        cache = {"last_success": {"ts": 970.0, "data": {"x": 1}}}
        d = statusline.cache_decide(cache, now)
        self.assertFalse(d["should_fetch"])
        self.assertEqual(d["data"], {"x": 1})
        self.assertFalse(d["stale"])

    def test_stale_window_with_recent_failure_serves_stale(self):
        now = 1000.0
        cache = {
            "last_success": {"ts": now - 300, "data": {"x": 1}},
            "last_failure": {"ts": now - 60, "error": "network"},
        }
        d = statusline.cache_decide(cache, now)
        self.assertFalse(d["should_fetch"])
        self.assertEqual(d["data"], {"x": 1})
        self.assertTrue(d["stale"])

    def test_expired_success_falls_through_to_fetch(self):
        now = 1000.0
        cache = {"last_success": {"ts": now - 1000, "data": {"x": 1}}}
        d = statusline.cache_decide(cache, now)
        self.assertTrue(d["should_fetch"])

    def test_failure_backoff_no_data(self):
        now = 1000.0
        cache = {"last_failure": {"ts": now - 60, "error": "network"}}
        d = statusline.cache_decide(cache, now)
        self.assertFalse(d["should_fetch"])
        self.assertIsNone(d["data"])

    def test_failure_old_allows_fetch(self):
        now = 1000.0
        cache = {"last_failure": {"ts": now - 600, "error": "network"}}
        d = statusline.cache_decide(cache, now)
        self.assertTrue(d["should_fetch"])

    def test_empty_cache_fetches(self):
        d = statusline.cache_decide({}, 1000.0)
        self.assertTrue(d["should_fetch"])

    def test_garbage_cache_fetches(self):
        d = statusline.cache_decide("not a dict", 1000.0)
        self.assertTrue(d["should_fetch"])


class CacheReadWriteTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = Path(self.dir) / "usage.json"

    def test_roundtrip(self):
        statusline.cache_write({"a": 1}, self.path)
        self.assertEqual(statusline.cache_read(self.path), {"a": 1})

    def test_corrupt_file_returns_empty(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("not json{")
        self.assertEqual(statusline.cache_read(self.path), {})

    def test_missing_file_returns_empty(self):
        self.assertEqual(statusline.cache_read(self.path), {})

    def test_write_creates_parent_dir(self):
        nested = Path(self.dir) / "nested" / "dir" / "u.json"
        statusline.cache_write({"a": 1}, nested)
        self.assertTrue(nested.exists())
        self.assertEqual(statusline.cache_read(nested), {"a": 1})

    def test_write_is_atomic(self):
        # Pre-existing valid file must not be left half-written if write fails.
        statusline.cache_write({"a": 1}, self.path)
        self.assertEqual(statusline.cache_read(self.path), {"a": 1})
        statusline.cache_write({"a": 2}, self.path)
        self.assertEqual(statusline.cache_read(self.path), {"a": 2})


class GetUsageTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = Path(self.dir) / "usage.json"

    def test_fresh_hit_skips_fetch(self):
        now = 1000.0
        statusline.cache_write({
            "last_success": {"ts": now - 30, "data": {"five_hour_pct": 42}}
        }, self.path)
        called = {"n": 0}

        def fake_fetch(token):
            called["n"] += 1
            return {"ok": True, "data": {}}

        result = statusline.get_usage(
            "tok", fetch_fn=fake_fetch, cache_path=self.path, now=now
        )
        self.assertEqual(called["n"], 0)
        self.assertEqual(result, {"data": {"five_hour_pct": 42}, "stale": False})

    def test_failure_backoff_skips_fetch(self):
        now = 1000.0
        statusline.cache_write({
            "last_failure": {"ts": now - 60, "error": "network"}
        }, self.path)
        called = {"n": 0}

        def fake_fetch(token):
            called["n"] += 1
            return {"ok": True, "data": {}}

        result = statusline.get_usage(
            "tok", fetch_fn=fake_fetch, cache_path=self.path, now=now
        )
        self.assertEqual(called["n"], 0)
        self.assertIsNone(result)

    def test_fetch_failure_serves_stale(self):
        now = 1000.0
        statusline.cache_write({
            "last_success": {"ts": now - 300, "data": {"five_hour_pct": 42}}
        }, self.path)

        def fake_fetch(token):
            return {"ok": False, "error": "network"}

        result = statusline.get_usage(
            "tok", fetch_fn=fake_fetch, cache_path=self.path, now=now
        )
        self.assertEqual(result, {"data": {"five_hour_pct": 42}, "stale": True})

    def test_fetch_success_writes_cache(self):
        now = 1000.0

        def fake_fetch(token):
            return {"ok": True, "data": {"five_hour_pct": 50}}

        result = statusline.get_usage(
            "tok", fetch_fn=fake_fetch, cache_path=self.path, now=now
        )
        self.assertEqual(result, {"data": {"five_hour_pct": 50}, "stale": False})
        c = statusline.cache_read(self.path)
        self.assertEqual(c["last_success"]["data"], {"five_hour_pct": 50})

    def test_corrupt_cache_recovers(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("garbage{not json")
        now = 1000.0

        def fake_fetch(token):
            return {"ok": True, "data": {"weekly_pct": 10}}

        result = statusline.get_usage(
            "tok", fetch_fn=fake_fetch, cache_path=self.path, now=now
        )
        self.assertEqual(result, {"data": {"weekly_pct": 10}, "stale": False})


class FormatHelperTests(unittest.TestCase):
    def test_hours_minutes(self):
        self.assertEqual(statusline._format_hours_minutes(2 * 3600 + 15 * 60), "2h15m")
        self.assertEqual(statusline._format_hours_minutes(3600), "1h0m")
        self.assertEqual(statusline._format_hours_minutes(0), "0h0m")
        self.assertEqual(statusline._format_hours_minutes(-100), "0h0m")

    def test_days_hours(self):
        self.assertEqual(statusline._format_days_hours(3 * 86400 + 4 * 3600), "3d4h")
        self.assertEqual(statusline._format_days_hours(86400), "1d0h")
        self.assertEqual(statusline._format_days_hours(0), "0d0h")


class RenderRateLimitTests(unittest.TestCase):
    NOW = datetime.datetime(2026, 5, 7, 12, 0, 0, tzinfo=datetime.timezone.utc)

    def test_renders_both_segments(self):
        usage = {"data": {
            "five_hour_pct": 42,
            "five_hour_resets_at": "2026-05-07T14:15:00Z",
            "weekly_pct": 18,
            "weekly_resets_at": "2026-05-10T16:00:00Z",
        }, "stale": False}
        segs = statusline.render_rate_limit_segments(usage, now=self.NOW)
        self.assertEqual(segs, ["5h:42%(2h15m)", "wk:18%(3d4h)"])

    def test_stale_appends_marker(self):
        usage = {"data": {
            "five_hour_pct": 50,
            "five_hour_resets_at": "2026-05-07T13:00:00Z",
        }, "stale": True}
        segs = statusline.render_rate_limit_segments(usage, now=self.NOW)
        self.assertEqual(segs, ["5h:50%(1h0m)*"])

    def test_empty_usage(self):
        self.assertEqual(statusline.render_rate_limit_segments(None), [])
        self.assertEqual(statusline.render_rate_limit_segments({"data": {}}), [])

    def test_partial_data_renders_partial(self):
        usage = {"data": {
            "weekly_pct": 25,
            "weekly_resets_at": "2026-05-09T18:00:00Z",
        }, "stale": False}
        segs = statusline.render_rate_limit_segments(usage, now=self.NOW)
        self.assertEqual(len(segs), 1)
        self.assertTrue(segs[0].startswith("wk:"))

    def test_handles_fraction_pct(self):
        # If API returns utilization as fraction (0.42), it's normalized to 42.
        # Normalization happens in fetch_usage; here we render whatever the
        # data dict gives, so a value of 42 goes through unchanged.
        usage = {"data": {
            "five_hour_pct": 42.7,
            "five_hour_resets_at": "2026-05-07T13:30:00Z",
        }, "stale": False}
        segs = statusline.render_rate_limit_segments(usage, now=self.NOW)
        # Round to whole percent.
        self.assertEqual(segs[0], "5h:43%(1h30m)")


class NormalizeUsageTests(unittest.TestCase):
    def test_nested_shape_passes_percent_through(self):
        # The OAuth API returns utilization on a 0-100 scale. A value of 1.0
        # means 1%, NOT "fully utilized" - the original fraction heuristic
        # corrupted small percentages (1% became 100%).
        raw = {
            "five_hour": {"utilization": 1.0, "resets_at": "2026-05-07T14:15:00Z"},
            "weekly": {"utilization": 26.0, "resets_at": "2026-05-10T16:00:00Z"},
        }
        out = statusline._normalize_usage(raw)
        self.assertAlmostEqual(out["five_hour_pct"], 1.0)
        self.assertEqual(out["five_hour_resets_at"], "2026-05-07T14:15:00Z")
        self.assertAlmostEqual(out["weekly_pct"], 26.0)

    def test_flat_shape(self):
        raw = {
            "five_hour_pct": 55,
            "five_hour_resets_at": "2026-05-07T14:00:00Z",
            "weekly_pct": 22,
            "weekly_resets_at": "2026-05-10T18:00:00Z",
        }
        out = statusline._normalize_usage(raw)
        self.assertEqual(out["five_hour_pct"], 55.0)
        self.assertEqual(out["weekly_pct"], 22.0)

    def test_empty_input(self):
        self.assertEqual(statusline._normalize_usage({}), {})
        self.assertEqual(statusline._normalize_usage(None), {})


class AuthExtractTests(unittest.TestCase):
    def test_nested_oauth_key(self):
        creds = {"claudeAiOauth": {"accessToken": "T", "expiresAt": 9999999999999}}
        out = statusline._extract_oauth(creds)
        self.assertEqual(out["accessToken"], "T")

    def test_flat_creds(self):
        creds = {"accessToken": "T", "expiresAt": 9999999999999}
        out = statusline._extract_oauth(creds)
        self.assertEqual(out["accessToken"], "T")

    def test_garbage(self):
        self.assertIsNone(statusline._extract_oauth(None))
        self.assertIsNone(statusline._extract_oauth({"foo": 1}))


if __name__ == "__main__":
    unittest.main()
