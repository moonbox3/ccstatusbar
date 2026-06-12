"""Tests for slice 06: render budget, NO_COLOR, threshold colors."""

import os
import time
import unittest
from unittest import mock

import statusline


# ---------- threshold colors ----------

class ColorEnabledTestCase(unittest.TestCase):
    def setUp(self):
        self.env_patcher = mock.patch.dict(os.environ, {"NO_COLOR": ""})
        self.env_patcher.start()
        self.addCleanup(self.env_patcher.stop)


class ThresholdColorTests(unittest.TestCase):
    def test_under_70_is_green(self):
        for pct in (0, 1, 50, 69):
            self.assertEqual(statusline._threshold_color(pct),
                             statusline.COLOR_GREEN, f"pct={pct}")

    def test_70_to_89_is_yellow(self):
        for pct in (70, 75, 89):
            self.assertEqual(statusline._threshold_color(pct),
                             statusline.COLOR_YELLOW, f"pct={pct}")

    def test_90_and_above_is_red(self):
        for pct in (90, 95, 100, 200):
            self.assertEqual(statusline._threshold_color(pct),
                             statusline.COLOR_RED, f"pct={pct}")


# ---------- ctx segment colors ----------

class CtxSegmentColorTests(ColorEnabledTestCase):
    def _seg(self, used, model_id="claude-opus-4-7", colorize=True):
        info = {
            "input_tokens": used, "cache_read_tokens": 0,
            "cache_creation_tokens": 0, "model_id": model_id,
        }
        return statusline.render_ctx_segment(info, colorize=colorize)

    def test_uncolored_default_matches_legacy_format(self):
        info = {"input_tokens": 34_000, "cache_read_tokens": 0,
                "cache_creation_tokens": 0, "model_id": "claude-opus-4-7"}
        self.assertEqual(statusline.render_ctx_segment(info),
                         "ctx:17%(34k/200k)")

    def test_green_at_69(self):
        seg = self._seg(138_000)  # 138/200 = 69%
        self.assertIn(statusline.COLOR_GREEN, seg)
        self.assertNotIn(statusline.COLOR_YELLOW, seg)
        self.assertNotIn(statusline.COLOR_RED, seg)

    def test_yellow_at_70(self):
        seg = self._seg(140_000)  # 70%
        self.assertIn(statusline.COLOR_YELLOW, seg)
        self.assertNotIn(statusline.COLOR_RED, seg)

    def test_yellow_at_89(self):
        seg = self._seg(178_000)  # 89%
        self.assertIn(statusline.COLOR_YELLOW, seg)
        self.assertNotIn(statusline.COLOR_RED, seg)

    def test_red_at_90(self):
        seg = self._seg(180_000)  # 90%
        self.assertIn(statusline.COLOR_RED, seg)
        self.assertNotIn(statusline.COLOR_YELLOW, seg)

    def test_label_and_tail_dim(self):
        seg = self._seg(34_000)
        self.assertIn(statusline.COLOR_DIM, seg)
        # Reset codes appear after each colored chunk.
        self.assertIn(statusline.COLOR_RESET, seg)


# ---------- rate-limit colors ----------

class RateLimitColorTests(ColorEnabledTestCase):
    def _usage(self, fh_pct, wk_pct, stale=False):
        return {
            "data": {
                "five_hour_pct": fh_pct,
                "five_hour_resets_at": "2026-05-07T05:00:00+00:00",
                "weekly_pct": wk_pct,
                "weekly_resets_at": "2026-05-08T00:00:00+00:00",
            },
            "stale": stale,
        }

    def test_uncolored_format_unchanged(self):
        import datetime as dt
        now = dt.datetime(2026, 5, 7, 4, 0, 0, tzinfo=dt.timezone.utc)
        segs = statusline.render_rate_limit_segments(
            self._usage(50, 25), now=now)
        self.assertEqual(segs[0], "5h:50%(1h0m)")
        self.assertEqual(segs[1], "wk:25%(0d20h)")

    def test_color_thresholds(self):
        import datetime as dt
        now = dt.datetime(2026, 5, 7, 4, 0, 0, tzinfo=dt.timezone.utc)
        segs = statusline.render_rate_limit_segments(
            self._usage(50, 95), now=now, colorize=True)
        self.assertIn(statusline.COLOR_GREEN, segs[0])  # 50 → green
        self.assertIn(statusline.COLOR_RED, segs[1])    # 95 → red


# ---------- NO_COLOR suppression ----------

class NoColorTests(unittest.TestCase):
    def test_no_color_env_suppresses_all_escapes(self):
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
            with mock.patch.object(statusline, "get_access_token",
                                   return_value=(None, "auth")), \
                 mock.patch.object(statusline, "get_git_status",
                                   return_value={"in_repo": False}), \
                 mock.patch.object(statusline, "parse_transcript",
                                   return_value=None):
                line = statusline.render({
                    "model": {"display_name": "Opus"},
                    "workspace": {"current_dir": "/tmp/xyz"},
                })
        self.assertNotIn("\x1b[", line)
        self.assertIn("[API auth]", line)
        self.assertIn("Opus", line)

    def test_segment_funcs_no_color_when_env_set(self):
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
            seg = statusline.render_ctx_segment(
                {"input_tokens": 180_000, "cache_read_tokens": 0,
                 "cache_creation_tokens": 0, "model_id": "x"},
                colorize=True)
        self.assertNotIn("\x1b[", seg)


# ---------- render budget / deadline ----------

class RenderBudgetTests(unittest.TestCase):
    def test_deadline_falls_back_to_disk_cache(self):
        cache_data = {
            "last_success": {
                "ts": time.time(),
                "data": {
                    "five_hour_pct": 11,
                    "five_hour_resets_at": "2099-01-01T00:00:00+00:00",
                    "weekly_pct": 22,
                    "weekly_resets_at": "2099-01-02T00:00:00+00:00",
                },
            },
        }

        def slow_token(*a, **kw):
            time.sleep(5)
            return ("tok", None)

        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}), \
             mock.patch.object(statusline, "get_access_token",
                               side_effect=slow_token), \
             mock.patch.object(statusline, "cache_read",
                               return_value=cache_data), \
             mock.patch.object(statusline, "get_git_status",
                               return_value={"in_repo": False}), \
             mock.patch.object(statusline, "parse_transcript",
                               return_value=None):
            start = time.monotonic()
            deadline = start + 0.2  # tight budget
            line = statusline.render({
                "model": {"display_name": "Opus"},
                "workspace": {"current_dir": "/tmp/xyz"},
            }, deadline=deadline)
            elapsed = time.monotonic() - start
        self.assertLess(elapsed, 1.0,
                        f"render took {elapsed:.3f}s, exceeded budget")
        # Cache fallback renders the rate-limit segments with stale marker.
        self.assertIn("5h:", line)
        self.assertIn("*", line)

    def test_deadline_with_no_cache_returns_no_segments(self):
        def slow_token(*a, **kw):
            time.sleep(5)
            return ("tok", None)

        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}), \
             mock.patch.object(statusline, "get_access_token",
                               side_effect=slow_token), \
             mock.patch.object(statusline, "cache_read",
                               return_value={}), \
             mock.patch.object(statusline, "get_git_status",
                               return_value={"in_repo": False}), \
             mock.patch.object(statusline, "parse_transcript",
                               return_value=None):
            start = time.monotonic()
            deadline = start + 0.2
            line = statusline.render({
                "model": {"display_name": "Opus"},
                "workspace": {"current_dir": "/tmp/xyz"},
            }, deadline=deadline)
            elapsed = time.monotonic() - start
        self.assertLess(elapsed, 1.0)
        self.assertNotIn("5h:", line)
        self.assertNotIn("wk:", line)
        self.assertIn("Opus", line)

    def test_fast_path_completes_normally(self):
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}), \
             mock.patch.object(statusline, "get_access_token",
                               return_value=(None, None)), \
             mock.patch.object(statusline, "get_git_status",
                               return_value={"in_repo": False}), \
             mock.patch.object(statusline, "parse_transcript",
                               return_value=None):
            line = statusline.render({
                "model": {"display_name": "Opus"},
                "workspace": {"current_dir": "/tmp/xyz"},
            })
        self.assertIn("xyz", line)
        self.assertIn("Opus", line)
        self.assertNotIn("[API auth]", line)


# ---------- full composition ----------

class FullCompositionTests(unittest.TestCase):
    def test_all_segments_present_in_order(self):
        usage = {
            "data": {
                "five_hour_pct": 13,
                "five_hour_resets_at": "2099-01-01T00:00:00+00:00",
                "weekly_pct": 25,
                "weekly_resets_at": "2099-01-02T00:00:00+00:00",
            },
            "stale": False,
        }
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}), \
             mock.patch.object(statusline, "get_access_token",
                               return_value=("tok", None)), \
             mock.patch.object(statusline, "get_usage",
                               return_value=usage), \
             mock.patch.object(statusline, "get_git_status",
                               return_value={
                                   "in_repo": True, "branch": "main",
                                   "staged": 0, "unstaged": 1, "untracked": 2,
                                   "ahead": 0, "behind": 0,
                               }), \
             mock.patch.object(statusline, "parse_transcript",
                               return_value={
                                   "input_tokens": 34_000,
                                   "cache_read_tokens": 0,
                                   "cache_creation_tokens": 0,
                                   "model_id": "claude-opus-4-7",
                               }):
            line = statusline.render({
                "model": {"display_name": "Opus 4.7"},
                "workspace": {"current_dir": "/tmp/xyz"},
            })
        # Expected order: cwd, git, ctx, 5h, wk, model.
        positions = [line.index(s) for s in
                     ["xyz", "main", "ctx:", "5h:", "wk:", "Opus 4.7"]]
        self.assertEqual(positions, sorted(positions))


if __name__ == "__main__":
    unittest.main()
