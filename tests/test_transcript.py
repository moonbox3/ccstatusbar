"""Tests for the transcript parser and ctx-segment renderer (slice 03)."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from statusline import (
    context_limit,
    parse_transcript,
    render,
    render_ctx_segment,
)


def _write_jsonl(path: Path, records):
    with path.open("w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _assistant(model: str, usage: dict) -> dict:
    return {
        "type": "assistant",
        "message": {"model": model, "usage": usage},
    }


class ContextLimitTests(unittest.TestCase):
    def test_default_is_200k(self):
        self.assertEqual(context_limit("claude-opus-4-7"), 200_000)

    def test_unknown_model_defaults_to_200k(self):
        self.assertEqual(context_limit(""), 200_000)
        self.assertEqual(context_limit("some-future-model"), 200_000)

    def test_1m_suffix_detected(self):
        self.assertEqual(context_limit("claude-opus-4-7[1m]"), 1_000_000)

    def test_1m_suffix_case_insensitive(self):
        self.assertEqual(context_limit("Claude-Opus-4-7[1M]"), 1_000_000)


class ParseTranscriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "session.jsonl"

    def test_standard_200k_model(self):
        _write_jsonl(self.path, [
            _assistant("claude-opus-4-7", {
                "input_tokens": 100,
                "cache_creation_input_tokens": 20_000,
                "cache_read_input_tokens": 14_000,
            }),
        ])
        info = parse_transcript(str(self.path))
        self.assertEqual(info["input_tokens"], 100)
        self.assertEqual(info["cache_creation_tokens"], 20_000)
        self.assertEqual(info["cache_read_tokens"], 14_000)
        self.assertEqual(info["model_id"], "claude-opus-4-7")

    def test_1m_model_variant(self):
        _write_jsonl(self.path, [
            _assistant("claude-opus-4-7[1m]", {
                "input_tokens": 50,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 100_000,
            }),
        ])
        info = parse_transcript(str(self.path))
        self.assertEqual(info["model_id"], "claude-opus-4-7[1m]")
        self.assertEqual(context_limit(info["model_id"]), 1_000_000)

    def test_message_with_only_input_tokens(self):
        _write_jsonl(self.path, [
            _assistant("claude-opus-4-7", {"input_tokens": 1234}),
        ])
        info = parse_transcript(str(self.path))
        self.assertEqual(info["input_tokens"], 1234)
        self.assertEqual(info["cache_creation_tokens"], 0)
        self.assertEqual(info["cache_read_tokens"], 0)

    def test_corrupt_file_returns_none(self):
        self.path.write_text("this is not json\n{also not}\n")
        self.assertIsNone(parse_transcript(str(self.path)))

    def test_empty_file_returns_none(self):
        self.path.write_text("")
        self.assertIsNone(parse_transcript(str(self.path)))

    def test_only_user_messages_returns_none(self):
        _write_jsonl(self.path, [
            {"type": "user", "message": {"role": "user", "content": "hi"}},
            {"type": "user", "message": {"role": "user", "content": "again"}},
        ])
        self.assertIsNone(parse_transcript(str(self.path)))

    def test_missing_file_returns_none(self):
        self.assertIsNone(parse_transcript(str(self.path) + ".does-not-exist"))

    def test_empty_path_returns_none(self):
        self.assertIsNone(parse_transcript(""))
        self.assertIsNone(parse_transcript(None))

    def test_picks_last_assistant_message(self):
        _write_jsonl(self.path, [
            _assistant("claude-opus-4-7", {"input_tokens": 1}),
            {"type": "user", "message": {"role": "user", "content": "x"}},
            _assistant("claude-opus-4-7[1m]", {
                "input_tokens": 999,
                "cache_read_input_tokens": 500_000,
            }),
        ])
        info = parse_transcript(str(self.path))
        self.assertEqual(info["model_id"], "claude-opus-4-7[1m]")
        self.assertEqual(info["input_tokens"], 999)

    def test_skips_assistant_without_usage(self):
        _write_jsonl(self.path, [
            _assistant("claude-opus-4-7", {"input_tokens": 42}),
            {"type": "assistant", "message": {"model": "x"}},
        ])
        info = parse_transcript(str(self.path))
        self.assertEqual(info["input_tokens"], 42)


class RenderCtxSegmentTests(unittest.TestCase):
    def test_none_info_omits_segment(self):
        self.assertEqual(render_ctx_segment(None), "")

    def test_zero_used_omits_segment(self):
        info = {
            "input_tokens": 0, "cache_read_tokens": 0, "cache_creation_tokens": 0,
            "model_id": "claude-opus-4-7",
        }
        self.assertEqual(render_ctx_segment(info), "")

    def test_basic_200k_render(self):
        info = {
            "input_tokens": 1_000,
            "cache_read_tokens": 20_000,
            "cache_creation_tokens": 13_000,
            "model_id": "claude-opus-4-7",
        }
        # used = 34000 → 17% of 200k → 34k/200k
        self.assertEqual(render_ctx_segment(info), "ctx:17%(34k/200k)")

    def test_1m_model_render(self):
        info = {
            "input_tokens": 0,
            "cache_read_tokens": 100_000,
            "cache_creation_tokens": 0,
            "model_id": "claude-opus-4-7[1m]",
        }
        self.assertEqual(render_ctx_segment(info), "ctx:10%(100k/1000k)")


class RenderIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "session.jsonl"
        import statusline
        env_patcher = mock.patch.dict(os.environ, {"NO_COLOR": "1"})
        env_patcher.start()
        self.addCleanup(env_patcher.stop)
        # Don't reach out to the network during render integration.
        gat = mock.patch.object(statusline, "get_access_token",
                                return_value=(None, None))
        gat.start()
        self.addCleanup(gat.stop)

    def test_ctx_segment_appears_between_git_and_model(self):
        _write_jsonl(self.path, [
            _assistant("claude-opus-4-7", {
                "input_tokens": 1_000,
                "cache_creation_input_tokens": 13_000,
                "cache_read_input_tokens": 20_000,
            }),
        ])
        # Use a non-git directory so git segment is absent.
        non_git = Path(self.tmp.name) / "not-a-repo"
        non_git.mkdir()
        line = render({
            "model": {"display_name": "Opus 4.7"},
            "workspace": {"current_dir": str(non_git)},
            "transcript_path": str(self.path),
        })
        # cwd basename, then ctx segment, then model
        self.assertIn("ctx:17%(34k/200k)", line)
        self.assertTrue(line.endswith("Opus 4.7"))
        self.assertIn(non_git.name, line)

    def test_missing_transcript_omits_ctx_segment(self):
        line = render({
            "model": {"display_name": "Opus 4.7"},
            "workspace": {"current_dir": str(Path(self.tmp.name))},
            "transcript_path": str(self.path) + ".missing",
        })
        self.assertNotIn("ctx:", line)

    def test_corrupt_transcript_does_not_crash(self):
        self.path.write_text("garbage\n{not json")
        line = render({
            "model": {"display_name": "Opus 4.7"},
            "workspace": {"current_dir": str(Path(self.tmp.name))},
            "transcript_path": str(self.path),
        })
        self.assertNotIn("ctx:", line)


if __name__ == "__main__":
    unittest.main()
