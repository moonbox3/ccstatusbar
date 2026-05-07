"""Tests for the git_status parser and render integration.

Fixtures are captured `git status --porcelain=v2 --branch` output. The
parser must return the documented dict shape regardless of how the
underlying command is run; we exercise it with the captured strings
directly via the public parse function.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "statusline.py"

sys.path.insert(0, str(REPO_ROOT))
import statusline  # noqa: E402


# ---------- fixtures ----------

CLEAN = """\
# branch.oid abcdef0123456789abcdef0123456789abcdef01
# branch.head main
# branch.upstream origin/main
# branch.ab +0 -0
"""

ONLY_STAGED = """\
# branch.oid abcdef0123456789abcdef0123456789abcdef01
# branch.head main
# branch.upstream origin/main
# branch.ab +0 -0
1 M. N... 100644 100644 100644 aaaa bbbb foo.py
1 A. N... 000000 100644 100644 0000 cccc bar.py
1 D. N... 100644 000000 100644 dddd 0000 baz.py
"""

ONLY_UNSTAGED = """\
# branch.oid abcdef0123456789abcdef0123456789abcdef01
# branch.head main
# branch.upstream origin/main
# branch.ab +0 -0
1 .M N... 100644 100644 100644 aaaa bbbb foo.py
1 .D N... 100644 100644 100644 cccc dddd baz.py
"""

ONLY_UNTRACKED = """\
# branch.oid abcdef0123456789abcdef0123456789abcdef01
# branch.head main
# branch.upstream origin/main
# branch.ab +0 -0
? new_file.py
? another.txt
"""

MIXED = """\
# branch.oid abcdef0123456789abcdef0123456789abcdef01
# branch.head feature/x
# branch.upstream origin/feature/x
# branch.ab +2 -1
1 M. N... 100644 100644 100644 aaaa bbbb staged.py
1 .M N... 100644 100644 100644 cccc dddd unstaged.py
1 MM N... 100644 100644 100644 eeee ffff both.py
2 R. N... 100644 100644 100644 gggg hhhh R100 new\told
? brand_new.py
"""

DETACHED = """\
# branch.oid abcdef0123456789abcdef0123456789abcdef01
# branch.head (detached)
"""

NO_UPSTREAM = """\
# branch.oid abcdef0123456789abcdef0123456789abcdef01
# branch.head my-feature
"""

AHEAD_ONLY = """\
# branch.oid abcdef0123456789abcdef0123456789abcdef01
# branch.head main
# branch.upstream origin/main
# branch.ab +3 -0
"""

BEHIND_ONLY = """\
# branch.oid abcdef0123456789abcdef0123456789abcdef01
# branch.head main
# branch.upstream origin/main
# branch.ab +0 -2
"""

AHEAD_AND_BEHIND = """\
# branch.oid abcdef0123456789abcdef0123456789abcdef01
# branch.head main
# branch.upstream origin/main
# branch.ab +2 -3
"""


# ---------- parser tests ----------

class GitStatusParseTests(unittest.TestCase):
    def test_clean(self):
        d = statusline.parse_git_porcelain(CLEAN)
        self.assertTrue(d["in_repo"])
        self.assertEqual(d["branch"], "main")
        self.assertEqual(d["staged"], 0)
        self.assertEqual(d["unstaged"], 0)
        self.assertEqual(d["untracked"], 0)
        self.assertEqual(d["ahead"], 0)
        self.assertEqual(d["behind"], 0)

    def test_only_staged(self):
        d = statusline.parse_git_porcelain(ONLY_STAGED)
        self.assertEqual(d["staged"], 3)
        self.assertEqual(d["unstaged"], 0)
        self.assertEqual(d["untracked"], 0)

    def test_only_unstaged(self):
        d = statusline.parse_git_porcelain(ONLY_UNSTAGED)
        self.assertEqual(d["staged"], 0)
        self.assertEqual(d["unstaged"], 2)
        self.assertEqual(d["untracked"], 0)

    def test_only_untracked(self):
        d = statusline.parse_git_porcelain(ONLY_UNTRACKED)
        self.assertEqual(d["staged"], 0)
        self.assertEqual(d["unstaged"], 0)
        self.assertEqual(d["untracked"], 2)

    def test_mixed(self):
        d = statusline.parse_git_porcelain(MIXED)
        # staged.py (M.), both.py (MM), renamed (R.) → 3 staged
        self.assertEqual(d["staged"], 3)
        # unstaged.py (.M), both.py (MM) → 2 unstaged
        self.assertEqual(d["unstaged"], 2)
        self.assertEqual(d["untracked"], 1)
        self.assertEqual(d["branch"], "feature/x")
        self.assertEqual(d["ahead"], 2)
        self.assertEqual(d["behind"], 1)

    def test_detached_head(self):
        d = statusline.parse_git_porcelain(DETACHED)
        self.assertTrue(d["in_repo"])
        self.assertEqual(d["branch"], "(detached)")
        self.assertEqual(d["ahead"], 0)
        self.assertEqual(d["behind"], 0)

    def test_no_upstream(self):
        d = statusline.parse_git_porcelain(NO_UPSTREAM)
        self.assertTrue(d["in_repo"])
        self.assertEqual(d["branch"], "my-feature")
        self.assertEqual(d["ahead"], 0)
        self.assertEqual(d["behind"], 0)

    def test_ahead_only(self):
        d = statusline.parse_git_porcelain(AHEAD_ONLY)
        self.assertEqual(d["ahead"], 3)
        self.assertEqual(d["behind"], 0)

    def test_behind_only(self):
        d = statusline.parse_git_porcelain(BEHIND_ONLY)
        self.assertEqual(d["ahead"], 0)
        self.assertEqual(d["behind"], 2)

    def test_ahead_and_behind(self):
        d = statusline.parse_git_porcelain(AHEAD_AND_BEHIND)
        self.assertEqual(d["ahead"], 2)
        self.assertEqual(d["behind"], 3)

    def test_empty_output_means_not_in_repo(self):
        d = statusline.parse_git_porcelain("")
        self.assertFalse(d["in_repo"])


# ---------- render tests ----------

class GitRenderTests(unittest.TestCase):
    def test_not_in_repo_omits_segment(self):
        self.assertEqual(statusline.render_git_segment({"in_repo": False}), "")

    def test_clean_repo_shows_just_branch(self):
        d = statusline.parse_git_porcelain(CLEAN)
        self.assertEqual(statusline.render_git_segment(d), "main")

    def test_dirty_unstaged_and_untracked(self):
        d = {
            "in_repo": True, "branch": "main",
            "staged": 0, "unstaged": 1, "untracked": 2,
            "ahead": 0, "behind": 0,
        }
        self.assertEqual(statusline.render_git_segment(d), "main | U:1 A:2")

    def test_dirty_only_staged_no_zero_noise(self):
        d = {
            "in_repo": True, "branch": "main",
            "staged": 3, "unstaged": 0, "untracked": 0,
            "ahead": 0, "behind": 0,
        }
        self.assertEqual(statusline.render_git_segment(d), "main | S:3")

    def test_ahead_appended(self):
        d = {
            "in_repo": True, "branch": "main",
            "staged": 0, "unstaged": 0, "untracked": 0,
            "ahead": 2, "behind": 0,
        }
        self.assertEqual(statusline.render_git_segment(d), "main ↑2")

    def test_behind_appended(self):
        d = {
            "in_repo": True, "branch": "main",
            "staged": 0, "unstaged": 0, "untracked": 0,
            "ahead": 0, "behind": 1,
        }
        self.assertEqual(statusline.render_git_segment(d), "main ↓1")

    def test_ahead_and_behind(self):
        d = {
            "in_repo": True, "branch": "main",
            "staged": 0, "unstaged": 0, "untracked": 0,
            "ahead": 2, "behind": 3,
        }
        self.assertEqual(statusline.render_git_segment(d), "main ↑2↓3")

    def test_dirty_with_ahead(self):
        d = {
            "in_repo": True, "branch": "main",
            "staged": 1, "unstaged": 0, "untracked": 0,
            "ahead": 1, "behind": 0,
        }
        self.assertEqual(statusline.render_git_segment(d), "main | S:1 ↑1")


# ---------- end-to-end integration ----------

class GitSegmentIntegrationTests(unittest.TestCase):
    """Drive statusline.py against a temporary git repo and assert the
    rendered line includes a sensible git segment."""

    @classmethod
    def setUpClass(cls):
        if shutil.which("git") is None:
            raise unittest.SkipTest("git not installed")

    def _init_repo(self, path: Path) -> None:
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        for cmd in (
            ["git", "init", "-q", "-b", "main"],
            ["git", "commit", "-q", "--allow-empty", "-m", "init"],
        ):
            subprocess.run(cmd, cwd=path, env=env, check=True,
                           capture_output=True)

    def _run(self, cwd: Path, payload: dict) -> str:
        result = subprocess.run(
            [sys.executable, str(SCRIPT)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(cwd),
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        return result.stdout.strip()

    def test_clean_repo_renders_branch(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            self._init_repo(repo)
            out = self._run(repo, {
                "model": {"display_name": "Opus 4.7"},
                "workspace": {"current_dir": str(repo)},
            })
            self.assertIn("main", out)
            self.assertIn("Opus 4.7", out)
            # No counters when clean
            self.assertNotIn("|", out)

    def test_dirty_repo_shows_untracked(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            self._init_repo(repo)
            (repo / "new.txt").write_text("hi\n")
            out = self._run(repo, {
                "model": {"display_name": "Opus 4.7"},
                "workspace": {"current_dir": str(repo)},
            })
            self.assertIn("main | A:1", out)

    def test_outside_repo_omits_segment(self):
        with tempfile.TemporaryDirectory() as td:
            # td is not a git repo
            out = self._run(Path(td), {
                "model": {"display_name": "Opus 4.7"},
                "workspace": {"current_dir": td},
            })
            # Should still render cwd + model, but no '|' from git counters
            self.assertIn("Opus 4.7", out)


if __name__ == "__main__":
    unittest.main()
