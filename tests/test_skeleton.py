"""Tests for the skeleton statusline.py: stdin parsing + render."""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "statusline.py"


def run_statusline(stdin_text: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=stdin_text,
        capture_output=True,
        text=True,
        timeout=10,
        cwd=str(REPO_ROOT),
    )


class SkeletonRenderTests(unittest.TestCase):
    def test_renders_cwd_basename_and_model(self):
        payload = json.dumps({
            "model": {"display_name": "Opus 4.7"},
            "workspace": {"current_dir": "/tmp/foo"},
        })
        result = run_statusline(payload)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        out = result.stdout.strip()
        self.assertIn("foo", out)
        self.assertIn("Opus 4.7", out)

    def test_handles_empty_stdin_without_crash(self):
        result = run_statusline("")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        # Should produce some minimal fallback line, not crash
        self.assertTrue(result.stdout.strip() != "" or result.stdout == "\n")

    def test_handles_malformed_json_without_crash(self):
        result = run_statusline("not json at all {{{")
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    def test_handles_missing_fields_without_crash(self):
        result = run_statusline(json.dumps({}))
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    def test_script_has_python3_shebang(self):
        with open(SCRIPT, "r") as f:
            first_line = f.readline().rstrip("\n")
        self.assertEqual(first_line, "#!/usr/bin/env python3")

    def test_script_is_executable(self):
        self.assertTrue(os.access(SCRIPT, os.X_OK), "statusline.py must be executable")


if __name__ == "__main__":
    unittest.main()
